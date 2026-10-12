#!/usr/bin/env python3
"""
Canvas Quiz Duplicate/Similar Question Finder
-----------------------------------------------
Parses one or more Canvas QTI/IMSCC exports (.zip, .imscc, or already-
extracted folders), pulls out every question from every quiz and bank
(Classic Quizzes and New Quizzes alike), and finds:
  - Modified questions: versions of the same question whose stem wording,
    answer choices, or answer key were tweaked. Grouped into clusters, with
    a word-level diff of each version against the most common one.
  - Exact duplicates (same stem, same choices, same answer key)
  - Same stem but unrelated choices (generic stems like "Which of the
    following is true?" -- almost certainly different questions)

Usage:
    python3 quiz_dedup.py export1.zip export2.zip ... -o report.html

Notes on the Canvas QTI export format:
  - Each quiz lives in a folder named after its resource id (g<hash>/),
    containing assessment_meta.xml (quiz settings/title) and
    <hash>.xml (the actual QTI 1.2 question data).
  - Question stem text is in presentation/material/mattext (HTML-escaped).
  - Answer choices (for choice-based types) are in
    response_lid/render_choice/response_label/material/mattext.
  - Some "Group" sections point to a question bank via <sourcebank_ref>
    whose actual questions are NOT included in a course-content export
    (Canvas only bundles the banks it decides to inline). Those are
    reported separately as "unresolved bank references" rather than
    silently skipped.
  - New Quizzes come out of a course export in the same QTI 1.2 shape as
    Classic Quizzes. A New Quiz is an <assessment> with an
    external_assignment_id attribute, and a New Quizzes item bank is an
    <objectbank> with canvas_item_bank="true". Their questions carry a
    calculator_type metadata field that Classic questions don't have.
  - A New Quiz can also use single questions out of an item bank. Each is a
    <bankentry_item sourcebank_ref="..." item_ref="..."/> in the quiz, and
    the question itself stays in the bank.
"""
import argparse
import difflib
import hashlib
import html
import json
import math
import os
import re
import sys
import tempfile
import urllib.parse
import zipfile
from collections import Counter
from datetime import date
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import numpy as np
from rapidfuzz import fuzz, process

QTI_NS = "{http://www.imsglobal.org/xsd/ims_qtiasiv1p2}"

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
IMG_ATTR_RE = re.compile(r"""\b(src|alt)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


FILEBASE = "$IMS-CC-FILEBASE$/"  # how an export refers to a file it carries under web_resources/


class ImageStore:
    """Names question images by what they contain.

    An image file found in the export is hashed. Every image with the same
    bytes gets the same name (the first file name seen for it), whatever it
    is called elsewhere, and a different image that reuses a file name gets
    "name (2)". The rest of the script compares the "[image: name]" text, so
    this alone makes identical pictures match and different pictures differ.

    Images that can't be read (a web address, or a file the export leaves
    out) keep their file name, as before."""

    def __init__(self):
        self.name_of = {}    # content hash -> name
        self.hash_of = {}    # name -> content hash
        self.read = None     # reads a path inside the export being parsed, or returns None
        self.by_content = self.by_name = self.by_address = 0

    def open(self, source):
        """Point at an export: a folder, or a zip/imscc that is read in place.
        Returns something to close when that export is done."""
        if os.path.isdir(source):
            root = os.path.realpath(source)

            def read(rel):
                full = os.path.realpath(os.path.join(root, rel))
                if full.startswith(os.path.join(root, "")) and os.path.isfile(full):
                    with open(full, "rb") as f:
                        return f.read()
                return None
            self.read = read
            return None
        zf = zipfile.ZipFile(source, "r")
        # looked up without regard to case: the path in a question and the
        # path in the archive don't always agree on it
        members = {n.lower(): n for n in zf.namelist()}
        self.read = lambda rel: zf.read(members[rel.lower()]) if rel.lower() in members else None
        return zf

    def name(self, src, fallback):
        """The name to show and compare for an image, given its src."""
        rel, local = None, src
        while local.startswith("../"):
            local = local[3:]
        for base in (FILEBASE, "$IMS_CC_FILEBASE$/"):
            if local.startswith(base):
                rel = "web_resources/" + local[len(base):]
        if rel is None and local.lower().startswith("web_resources/"):
            rel = local
        if rel is None:
            if "://" in src:
                self.by_address += 1
            else:
                self.by_name += 1
            return fallback
        data = self.read(rel) if self.read else None
        if data is None:
            self.by_name += 1
            return fallback
        self.by_content += 1
        digest = hashlib.sha256(data).hexdigest()
        if digest not in self.name_of:
            name, n = fallback, 1
            while name in self.hash_of:  # same file name, different picture
                n += 1
                name = f"{fallback} ({n})"
            self.name_of[digest], self.hash_of[name] = name, digest
        return self.name_of[digest]


IMAGES = None  # the ImageStore in use while exports are being parsed
IMAGE_NAME_RE = re.compile(r"\[image: ([^\]]*)\]")
IMAGE_WORD_RE = re.compile(r"\bimg[0-9a-f]{16}\b")  # what normalize() turns a picture into


def different_pictures(a, b):
    """True when both sides show pictures and neither side's pictures are all
    among the other's. Showing one picture more or one fewer is an edit;
    showing a different picture is a different thing."""
    return bool(a and b) and not (a <= b or b <= a)


def image_token(match):
    """Stand-in text for an <img>, so an image-only stem or choice is kept
    and two questions that differ only by their image compare as different.
    Equation images are named by their LaTeX; others by their content when
    the file is in the export (see ImageStore), otherwise by file name."""
    attrs = {k.lower(): (a or b) for k, a, b in IMG_ATTR_RE.findall(match.group(0))}
    alt = html.unescape(attrs.get("alt", "")).strip()
    if alt.lower().startswith("latex:"):
        name = alt
    else:
        path = urllib.parse.unquote(html.unescape(attrs.get("src", "")).split("?")[0]).rstrip("/")
        parts = [p for p in path.split("/") if p]
        if parts and parts[-1] in ("preview", "download") and len(parts) > 1:
            parts = parts[:-1]  # .../files/123/preview -> 123
        name = (parts[-1] if parts else "") or alt or "unnamed"
        if IMAGES is not None:
            name = IMAGES.name(path, name)
    return f" [image: {name}] "


def strip_html(text):
    """Turn an HTML fragment (already unescaped) into plain readable text."""
    if not text:
        return ""
    text = IMG_RE.sub(image_token, text)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    return text


# sign -> word, applied in this order (two-character signs first)
MATH_WORDS = (
    (r"<=|\u2264", "le"), (r">=|\u2265", "ge"), (r"!=|\u2260", "ne"),
    (r"<", "lt"), (r">", "gt"), (r"=", "eq"), (r"\+", "plus"), (r"%", "percent"),
    (r"\u00d7|(?<=[\w)])\s?\*\s?(?=[\w(])", "times"), (r"\u00f7", "div"),
    (r"(?<=\d)\s?/\s?(?=\d)", "over"),
)


def normalize(text):
    """Aggressive normalization for exact-duplicate grouping."""
    # a picture becomes one opaque word, so two file names that look alike
    # (graph1.png, graph3.png) don't make two pictures look alike
    text = IMAGE_NAME_RE.sub(
        lambda m: " img" + hashlib.sha1(m.group(1).encode("utf-8")).hexdigest()[:16] + " ", text)
    text = text.lower()
    # keep what makes two numbers different: -5 is not 5, .5 is not 5,
    # 1.5 is not 15, and 1-5 is not 15
    text = re.sub(r"(?<![\w.])[-\u2212](?=\.?\d)", " minus ", text)
    # x-5 is not x5: a minus after a letter is kept too
    text = re.sub(r"(?<=[^\W\d_])\s?[-\u2212]\s?(?=\.?\d)", " minus ", text)
    text = re.sub(r"(?<=\d)[-\u2013\u2212](?=\d)", " ", text)
    # comparison and arithmetic signs carry meaning: x < 3 is not x > 3,
    # 1/2 is not 12, 50% is not 50. They become words before punctuation goes.
    for pattern, word in MATH_WORDS:
        text = re.sub(pattern, f" {word} ", text)
    text = re.sub(r"(?<!\w)\.(?=\d)", "0_", text)
    text = re.sub(r"(?<=\d)\.(?=\d)", "_", text)
    text = re.sub(r"[^\w\s]", "", text)  # drop punctuation
    text = WS_RE.sub(" ", text).strip()
    return text


@dataclass
class Question:
    source_file: str        # which zip/export this came from
    quiz_title: str
    quiz_id: str
    item_id: str
    question_type: str
    points: str
    text: str                # plain-text question stem
    choices: list = field(default_factory=list)  # plain-text answer choices
    source_kind: str = "quiz"  # "quiz" or "bank"
    correct: list = field(default_factory=list)  # plain-text credited answers
    key_pairs: list = field(default_factory=list)  # (prompt, answer) for matching rows / dropdown blanks
    engine: str = "classic"  # "classic" or "new" (New Quizzes), from the quiz or bank it is in
    # Classic Quizzes: the export ident of the bank question this quiz question
    # was made from (assessment_question_identifierref), or "" when there is none
    bank_ref: str = ""

    @property
    def norm_choices(self):
        return frozenset(normalize(c) for c in self.choices)

    @property
    def norm_correct(self):
        return frozenset(normalize(c) for c in self.correct)

    @property
    def norm_text(self):
        return normalize(self.text)

    @property
    def choices_text(self):
        return " | ".join(self.choices)


@dataclass
class Container:
    """One quiz or bank found in an export, whether or not it stores any
    questions of its own."""
    source_file: str
    kind: str                # "quiz" or "bank"
    cid: str
    title: str
    engine: str = "classic"  # "classic" or "new" (New Quizzes)
    # question groups that pull from a bank: [(bank id, how many)]. how many
    # is None when the export gives no number, which is how a New Quiz that
    # uses every question in the bank comes out.
    draws: list = field(default_factory=list)
    # single bank questions the quiz uses by reference: [(bank id, item id)]
    picks: list = field(default_factory=list)
    picked: set = field(default_factory=set)   # variant indexes of the picks found, filled in with members
    lost_picks: int = 0                        # picks whose question is not in the export
    missing: frozenset = frozenset()           # those picks, as (bank id, item id)
    missing_banks: frozenset = frozenset()     # banks it draws from that are not in the export
    item_count: int = 0      # questions stored in it in the export, before any --exclude filter
    members: set = field(default_factory=set)  # variant indexes, filled in when quizzes and banks are compared
    published: object = None  # True/False for a quiz whose assessment_meta.xml says, None when unknown

    @property
    def size(self):
        return len(self.members)


def plural(n, word, many=None):
    """'1 bank', '2 banks', '2 quizzes'."""
    return f"{n} {word if n == 1 else (many or word + 's')}"


def kind_label(kind, engine):
    """How a quiz or bank is named in the report. New Quizzes keeps its
    questions in what Canvas calls item banks."""
    if engine == "new":
        return "new quiz" if kind == "quiz" else "item bank"
    return kind


def local(tag):
    """Strip namespace from an ElementTree tag."""
    return tag.split("}")[-1] if "}" in tag else tag


def extract_mattext(material_el):
    """Get the plain-text content of a <material><mattext>...</mattext></material>."""
    if material_el is None:
        return ""
    for child in material_el:
        if local(child.tag) == "mattext":
            return strip_html(child.text or "")
    return ""


CONTAINER_TAGS = {"assessment", "objectbank"}


def get_container_title(container):
    """Quiz-backed content (<assessment>) carries its title as an attribute.
    Standalone question banks (<objectbank>) carry it as a bank_title
    metadata field instead."""
    tag = local(container.tag)
    if tag == "assessment":
        return container.get("title") or "(untitled quiz)"
    # objectbank
    for qmf in container.iter():
        if local(qmf.tag) == "qtimetadatafield":
            label_el = entry_el = None
            for c in qmf:
                if local(c.tag) == "fieldlabel":
                    label_el = c
                elif local(c.tag) == "fieldentry":
                    entry_el = c
            if label_el is not None and label_el.text == "bank_title" and entry_el is not None:
                return entry_el.text or "(untitled bank)"
    return "(untitled bank)"


def credited_answers(item):
    """Return [(respident, value)] for every answer Canvas awards credit for:
    <varequal> entries inside a <respcondition> whose <setvar> is above zero.
    Entries wrapped in <not> (the wrong boxes of a multiple-answers question)
    are skipped. Per-answer feedback conditions have no <setvar>, so they
    drop out on their own."""
    found = []

    def walk(el, negated):
        for c in el:
            t = local(c.tag)
            if t == "not":
                walk(c, not negated)
            elif t == "varequal":
                if not negated:
                    found.append((c.get("respident", ""), (c.text or "").strip()))
            else:
                walk(c, negated)

    for rc in item.iter():
        if local(rc.tag) != "respcondition":
            continue
        awards = False
        for sv in rc:
            if local(sv.tag) == "setvar":
                if (sv.get("action") or "").lower() == "subtract":
                    continue  # a penalty, not credit
                try:
                    awards = float((sv.text or "").strip()) > 0
                except ValueError:
                    pass
        if awards:
            for cv in rc:
                if local(cv.tag) == "conditionvar":
                    walk(cv, False)
    return found


def container_engine(container):
    """"new" if a quiz or bank comes from New Quizzes, otherwise "classic".
    In a course export a New Quiz is an <assessment> carrying an
    external_assignment_id, and a New Quizzes item bank is an <objectbank>
    marked canvas_item_bank. Their questions also carry a calculator_type
    field that Classic questions lack, which covers a file whose container
    isn't marked."""
    if container.get("external_assignment_id") is not None:
        return "new"
    if (container.get("canvas_item_bank") or "").lower() == "true":
        return "new"
    for el in container.iter():
        if local(el.tag) == "fieldlabel" and el.text == "calculator_type":
            return "new"
    return "classic"


def bank_draws(container):
    """[(bank id, how many)] for each question group in a quiz that pulls its
    questions from a bank instead of holding them. This is how a New Quiz
    built from item banks looks in an export: no questions, only draws."""
    draws = []
    for sel in container.iter():
        if local(sel.tag) != "selection":
            continue
        bank_id, count = "", None
        for c in sel:
            if local(c.tag) == "sourcebank_ref":
                bank_id = (c.text or "").strip()
            elif local(c.tag) == "selection_number":
                try:
                    count = int((c.text or "").strip())
                except ValueError:
                    pass
        if bank_id:
            draws.append((bank_id, count))
    return draws


def bank_picks(container):
    """[(bank id, item id)] for single questions a quiz uses out of a bank.
    New Quizzes writes these as <bankentry_item> references; the question
    text is only in the bank."""
    return [(el.get("sourcebank_ref", "").strip(), el.get("item_ref", "").strip())
            for el in container.iter()
            if local(el.tag) == "bankentry_item" and el.get("sourcebank_ref")]


def own_items(container):
    """The <item>s of a quiz or bank, not counting any inside a quiz or bank
    nested in it (Canvas does not nest them, but a hand-made file might)."""
    for c in container:
        t = local(c.tag)
        if t == "item":
            yield c
        elif t not in CONTAINER_TAGS:
            yield from own_items(c)


def parse_quiz_xml(path, source_file):
    """Parse one QTI file (a quiz's <assessment> or a bank's <objectbank>).
    Returns (questions, containers): the questions stored in the file, and a
    Container for each quiz or bank in it, including one with no questions."""
    questions = []
    containers = []
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        print(f"  ! could not parse {path}: {e}", file=sys.stderr)
        return questions, containers

    root = tree.getroot()

    for assessment in root.iter():
        tag = local(assessment.tag)
        if tag not in CONTAINER_TAGS:
            continue
        quiz_title = get_container_title(assessment)
        quiz_id = assessment.get("ident", "")
        source_kind = "bank" if tag == "objectbank" else "quiz"
        engine = container_engine(assessment)
        containers.append(Container(source_file, source_kind, quiz_id, quiz_title, engine,
                                    bank_draws(assessment), bank_picks(assessment)))

        for item in own_items(assessment):
            if True:
                item_id = item.get("ident", "")
                item_title = item.get("title", "")

                # question_type from itemmetadata
                qtype = "unknown"
                points = ""
                bank_ref = ""
                for qmf in item.iter():
                    if local(qmf.tag) == "qtimetadatafield":
                        label_el = None
                        entry_el = None
                        for c in qmf:
                            if local(c.tag) == "fieldlabel":
                                label_el = c
                            elif local(c.tag) == "fieldentry":
                                entry_el = c
                        if label_el is not None and entry_el is not None:
                            if label_el.text == "question_type":
                                qtype = entry_el.text or "unknown"
                            elif label_el.text == "points_possible":
                                points = entry_el.text or ""
                            elif label_el.text == "assessment_question_identifierref":
                                bank_ref = (entry_el.text or "").strip()

                # question stem: first <presentation><material><mattext>
                stem_text = ""
                presentation = None
                for c in item:
                    if local(c.tag) == "presentation":
                        presentation = c
                        break
                if presentation is not None:
                    for c in presentation:
                        if local(c.tag) == "material":
                            stem_text = extract_mattext(c)
                            break

                if not stem_text:
                    stem_text = WS_RE.sub(" ", item_title).strip()  # plain text, not HTML

                # answer choices (choice-based question types)
                choices = []
                prompts = []     # left-hand side of a matching question
                label_text = {}  # response_label ident -> choice text
                label_idents = set()
                lid_text = {}    # response_lid ident -> its own prompt (matching rows, dropdown blanks)
                if presentation is not None:
                    for resp in presentation.iter():
                        rtag = local(resp.tag)
                        if rtag == "response_lid":
                            for c in resp:
                                if local(c.tag) == "material":
                                    ltext = extract_mattext(c)
                                    if ltext:
                                        lid_text[resp.get("ident", "")] = ltext
                                        # the left-hand side of a matching question is
                                        # part of what can be reworded, so compare it too
                                        if qtype == "matching_question":
                                            prompts.append(ltext)
                        elif rtag == "response_label":
                            label_idents.add(resp.get("ident", ""))
                            for c in resp:
                                if local(c.tag) == "material":
                                    ctext = extract_mattext(c)
                                    if ctext:
                                        label_text[resp.get("ident", "")] = ctext
                                        # matching repeats the right-hand options under
                                        # every row; keep one copy
                                        if qtype != "matching_question" or ctext not in choices:
                                            choices.append(ctext)

                choices = prompts + choices  # matching: all prompts, then the options

                # answer key
                correct = []
                key_pairs = []
                blank_types = ("multiple_dropdowns_question", "fill_in_multiple_blanks_question")
                for respident, value in credited_answers(item):
                    if value in label_idents and value not in label_text:
                        continue  # points at a choice with no readable text
                    # short-answer keys carry the answer text itself, not an id
                    # and it is plain text: "x < 3 and y > 5" must not lose "< 3 and y >" as a tag
                    answer = label_text.get(value, WS_RE.sub(" ", value).strip())
                    prompt = lid_text.get(respident)
                    if not prompt and qtype in blank_types and respident.startswith("response_"):
                        prompt = respident[len("response_"):]
                    entry = f"{prompt} \u2192 {answer}" if prompt else answer
                    if entry and entry not in correct:
                        correct.append(entry)
                        if prompt:
                            key_pairs.append((prompt, answer))

                if not stem_text:
                    stem_text = "[no question text]"

                containers[-1].item_count += 1
                questions.append(Question(
                    source_file=source_file,
                    quiz_title=quiz_title,
                    quiz_id=quiz_id,
                    item_id=item_id,
                    question_type=qtype,
                    points=points,
                    text=stem_text,
                    choices=choices,
                    source_kind=source_kind,
                    correct=correct,
                    key_pairs=key_pairs,
                    engine=engine,
                    bank_ref=bank_ref if source_kind == "quiz" else "",
                ))

    return questions, containers


CONTAINER_IDENT_RE = re.compile(r"""<(?:\w+:)?(?:assessment|objectbank)\b[^>]*?\sident=["']([^"']+)["']""")


def find_quiz_xml_files(root_dir):
    """Return list of candidate QTI file paths: quiz question files (named
    various things depending on export vintage: <hash>.xml, assessment_qti.xml)
    and question-bank files under non_cc_assessments/ (named <hash>.xml.qti).
    Anything else -- course pages, module metadata, assignment settings -- is
    excluded by content sniff, not just by filename, since export layouts vary."""
    results = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for fn in filenames:
            if fn in ("assessment_meta.xml", "imsmanifest.xml"):
                continue
            if not (fn.endswith(".xml") or fn.endswith(".qti")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(2000)
                if "questestinterop" in head:
                    results.append(full)
            except OSError:
                pass
    return results


def resolve_preferred_files(xml_files):
    """A full course export often contains the SAME quiz twice: a portable
    'Common Cartridge' copy at the top level and a richer native copy under
    non_cc_assessments/, both carrying the identical question text under the
    same container ident. Parsing both would report every such quiz as
    duplicating itself. Keep one file per ident, preferring the
    non_cc_assessments copy when both exist."""
    ident_to_path = {}
    unidentified = []
    for path in xml_files:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            continue
        m = CONTAINER_IDENT_RE.search(content)
        if not m:
            unidentified.append(path)
            continue
        ident = m.group(1)
        existing = ident_to_path.get(ident)
        if existing is None:
            ident_to_path[ident] = path
        elif "non_cc_assessments" in path and "non_cc_assessments" not in existing:
            ident_to_path[ident] = path
    return list(ident_to_path.values()) + unidentified


def read_published(root_dir):
    """{quiz ident: published?} from the export's assessment_meta.xml files.
    Canvas writes a quiz's published state there as <available>, under a
    <quiz identifier="..."> root that matches the quiz's QTI ident. Quizzes
    with no such file (New Quizzes, quiz-only exports) are left out."""
    found = {}
    for dirpath, _, filenames in os.walk(root_dir):
        if "assessment_meta.xml" not in filenames:
            continue
        try:
            root = ET.parse(os.path.join(dirpath, "assessment_meta.xml")).getroot()
        except ET.ParseError:
            continue
        ident = root.get("identifier", "")
        for el in root:
            if local(el.tag) == "available" and ident:
                found[ident] = (el.text or "").strip().lower() == "true"
    return found


def load_source(path, workdir):
    """Given a zip/imscc file or a directory, return a directory containing its
    extracted contents."""
    if os.path.isdir(path):
        return path
    # a fresh directory per input: two exports with the same file name
    # (Fall/course.imscc, Spring/course.imscc) must not unpack on top of
    # each other
    extract_to = tempfile.mkdtemp(prefix="export_", dir=workdir)
    with zipfile.ZipFile(path, "r") as zf:
        # only the quiz files: a course export can hold gigabytes of video and documents
        wanted = [n for n in zf.namelist() if n.lower().endswith((".xml", ".qti"))]
        zf.extractall(extract_to, members=wanted)
    return extract_to


def export_labels(paths):
    """The name each export goes by in the report: its file name, or the full
    path when two inputs share a file name."""
    basenames = [os.path.basename(p.rstrip("/\\")) for p in paths]
    return [base if basenames.count(base) == 1 else p for p, base in zip(paths, basenames)]


def collect_questions(paths, images=None):
    """Parse every export. Returns (questions, unresolved_bank_refs,
    bank_usage, containers). `containers` lists every quiz and bank found,
    including quizzes that store no questions and only draw from banks.
    `images` is the ImageStore to name question images with; pass one to
    read its counts afterwards."""
    global IMAGES
    all_questions = []
    containers = []
    IMAGES = images if images is not None else ImageStore()
    try:
        with tempfile.TemporaryDirectory() as workdir:
            for p, label in zip(paths, export_labels(paths)):
                root_dir = load_source(p, workdir)
                # sorted, so image names (first one seen wins) don't depend on disk order
                xml_files = sorted(resolve_preferred_files(find_quiz_xml_files(root_dir)))
                archive = IMAGES.open(p)
                published = read_published(root_dir)
                try:
                    for xf in xml_files:
                        qs, found_here = parse_quiz_xml(xf, label)
                        all_questions.extend(qs)
                        for c in found_here:
                            if c.kind == "quiz":
                                c.published = published.get(c.cid)
                        containers.extend(found_here)
                finally:
                    if archive is not None:
                        archive.close()
    finally:
        IMAGES = None

    # (export label, quiz title, bank id) for every bank a quiz draws from
    in_export = {(q.source_file, q.quiz_id, q.item_id) for q in all_questions if q.source_kind == "bank"}
    for c in containers:
        c.missing = frozenset((b, i) for b, i in c.picks if (c.source_file, b, i) not in in_export)
        c.lost_picks = sum(1 for pick in c.picks if pick in c.missing)
    bank_refs = {(c.source_file, c.title, bank_id) for c in containers for bank_id, _ in c.draws}
    pick_refs = {(c.source_file, c.title, bank_id) for c in containers for bank_id, _ in c.picks}
    # a sourcebank_ref is only a real gap if that bank is nowhere in the same
    # export (e.g. under non_cc_assessments). The ones that are there tell us
    # which quizzes draw from which bank.
    found = {(c.source_file, c.cid) for c in containers}
    for c in containers:
        c.missing_banks = frozenset(b for b, _ in c.draws if (c.source_file, b) not in found)
    exports_with = {}
    for src, cid in found:
        exports_with.setdefault(cid, set()).add(src)
    # 4th field: other exports that do contain a bank with this id. It is a
    # hint, not a resolution: that copy may have been edited since.
    all_unresolved = sorted(
        r + (", ".join(sorted(exports_with.get(r[2], ()))),)
        for r in bank_refs | pick_refs if (r[0], r[2]) not in found
    )
    bank_usage = {}
    for label, quiz_title, bank_id in bank_refs:
        if (label, bank_id) in found:
            bank_usage.setdefault((label, bank_id), set()).add(quiz_title)

    return all_questions, all_unresolved, bank_usage, containers


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------
# Two questions count as versions of one another when the stem and the choices
# BOTH line up to some degree. Requiring both is what keeps generic stems
# ("Which of the following is true?") from matching each other.
LOOSE_STEM = 60       # stem floor when the choices are a strong match
CHOICES_FLOOR = 60    # choices floor when the stem is a strong match. Two
                      # unrelated sets of sentence-length choices already score
                      # 40-53 on character similarity, so the floor sits above that.
LOOSE_CHOICES = 50    # floor for calling two individual choices "the same choice, reworded"
BOTH_MARGIN = 10      # ...or both are within this many points of the threshold
STEM_ONLY_MIN = 90    # floor when choices can't help (true/false, essay, etc.)
TRUE_FALSE = frozenset({"true", "false"})
CHOICE_TYPES = frozenset({"multiple_choice_question", "multiple_answers_question", "true_false_question",
                          "matching_question", "multiple_dropdowns_question"})
STEM_BLOCK = 2000     # rows of the stem-similarity matrix computed at a time
WORD_OVERLAP = 60     # default for --word-overlap: how much of the distinctive
                      # wording two different stems must share
CONTAINER_OVERLAP = 80  # default for --container-overlap: share of a quiz's or
                        # bank's questions that must also be in another one
MIN_CONTAINER = 3     # quizzes and banks with fewer distinct questions are not compared

# the settings above that can be changed from the command line: (name, default, help)
ADVANCED_SETTINGS = (
    ("LOOSE_STEM", LOOSE_STEM, "Stem similarity accepted when the choices match strongly"),
    ("CHOICES_FLOOR", CHOICES_FLOOR, "Choice similarity accepted when the stems match strongly"),
    ("BOTH_MARGIN", BOTH_MARGIN, "Points below --threshold at which a stem and choices that both match count"),
    ("LOOSE_CHOICES", LOOSE_CHOICES, "Similarity for two single choices or matching rows to count as one reworded"),
    ("STEM_ONLY_MIN", STEM_ONLY_MIN, "Stem similarity needed for questions without choices (true/false, essay)"),
    ("MIN_CONTAINER", MIN_CONTAINER, "Smallest quiz or bank, in distinct questions, compared as a whole"),
)


@dataclass
class Variant:
    """One distinct version of a question (stem + choice set + answer key +
    question type) and every place it occurs."""
    stem: str
    choices: frozenset
    correct: frozenset
    qtype: str = ""
    occurrences: list = field(default_factory=list)

    @property
    def rep(self):
        return self.occurrences[0]

    @property
    def key_pairs(self):
        if not self.occurrences:
            return []
        return [(normalize(p), normalize(a)) for p, a in self.rep.key_pairs]


def build_variants(questions):
    variants = {}
    for q in questions:
        key = (q.norm_text, q.norm_choices, q.norm_correct, q.question_type)
        if key not in variants:
            variants[key] = Variant(*key)
        variants[key].occurrences.append(q)
    return list(variants.values())


def align_choices(a, b):
    """Pair up two lists of normalized choices one-to-one, best matches
    first. Returns [(index_in_a, index_in_b, score)]; leftovers on the longer
    side stay unpaired. No choice can be claimed twice."""
    if not a or not b:
        return []
    m = np.asarray(process.cdist(a, b, scorer=fuzz.ratio))
    # two choices that show different pictures are not the same choice reworded
    pics_a = [frozenset(IMAGE_WORD_RE.findall(x)) for x in a]
    pics_b = [frozenset(IMAGE_WORD_RE.findall(x)) for x in b]
    if any(pics_a) and any(pics_b):
        m = m.copy()
        for i, pa in enumerate(pics_a):
            for j, pb in enumerate(pics_b):
                if different_pictures(pa, pb):
                    m[i, j] = 0
    pairs, used_i, used_j = [], set(), set()
    limit = min(len(a), len(b))
    for flat in np.argsort(-m, axis=None, kind="stable"):
        i, j = divmod(int(flat), len(b))
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        pairs.append((i, j, float(m[i, j])))
        if len(pairs) == limit:
            break
    return pairs


def choice_score(a, b):
    """Order-independent similarity of two choice sets, 0-100, from a
    one-to-one pairing. A reworded choice costs a little; an added or
    dropped one counts as zero for that slot."""
    if a == b:
        return 100.0
    if not a or not b:
        return 0.0
    # sorted, because a set's order changes from run to run and the pairing
    # breaks ties by position: unsorted, the score could shift by a point
    pairs = align_choices(sorted(a), sorted(b))
    return 2.0 * sum(score for _, _, score in pairs) / (len(a) + len(b))


def version_scores(stem_score, va, vb, strict):
    """Return (stem_score, choice_score) if va and vb look like versions of
    the same question, else None."""
    ca, cb = va.choices, vb.choices
    if bool(ca) != bool(cb):
        return None
    if not ca or (ca <= TRUE_FALSE and cb <= TRUE_FALSE):
        # nothing but the stem to go on, so hold it to a higher bar
        return (stem_score, 100.0) if stem_score >= max(strict, STEM_ONLY_MIN) else None
    cs = choice_score(ca, cb)
    if ((stem_score >= strict and cs >= CHOICES_FLOOR)
            or (cs >= strict and stem_score >= LOOSE_STEM)
            or (stem_score >= strict - BOTH_MARGIN and cs >= strict - BOTH_MARGIN)):
        return (stem_score, cs)
    return None


SUFFIX_RE = re.compile(r"(?<=\w{3})(?:ing|ed|es|s|e)$")


def content_words(stem):
    """The stem's set of words, with common endings trimmed so that
    message/messages and accompany/accompanying count as the same word."""
    return {SUFFIX_RE.sub("", w) for w in stem.split()}


def word_weights(stems):
    """Weight for each word by how rare it is across all stems. Wording that
    recurs everywhere ("according to your textbook", "which of the following")
    counts for little; the words that make a question about what it is about
    count for a lot.

    Rarity can't be estimated from a handful of questions, so it is phased in
    with the size of the export: below a few hundred distinct stems the
    weights approach 1 and the measure becomes plain word overlap."""
    n = len(stems)
    trust = min(1.0, n / 300.0)
    df = Counter(w for s in stems for w in content_words(s))
    return {w: 1.0 + trust * math.log((n + 1) / (c + 1)) for w, c in df.items()}


def word_overlap(a, b, weights):
    """Share of two stems' distinctive wording that they have in common,
    0-100. Character similarity can't tell a reworded question from a
    different question built on the same sentence frame ("To persuade my
    audience that X is a ... question of"): the frame dominates the score.
    Weighting words by rarity makes the X count instead."""
    A, B = content_words(a), content_words(b)
    w = lambda words: sum(weights.get(x, 1.0) for x in words)
    shared, only_a, only_b = w(A & B), w(A - B), w(B - A)
    if shared > 0 and min(only_a, only_b) <= 0.05 * shared:
        return 100.0  # one stem is the other plus added words (a hint, a lead-in)
    total = shared + only_a + only_b
    return 100.0 * shared / total if total else 100.0


def group_lookalikes(pairs, usage):
    """Turn look-alike pairs (index pairs) into groups, most-used first.
    Each group is one question plus the questions directly paired with it,
    so a chain A-B-C-D becomes [A, B] and [C, D], not one group of four.
    A question whose look-alikes were all claimed by other groups joins the
    group of its most-used look-alike, so no pair's member is dropped."""
    near = {}
    for i, j in pairs:
        near.setdefault(i, set()).add(j)
        near.setdefault(j, set()).add(i)
    order = sorted(near, key=lambda i: (-usage[i], i))
    group_of, groups = {}, []
    for i in order:
        if i in group_of:
            continue
        members = sorted((j for j in near[i] if j not in group_of), key=lambda j: (-usage[j], j))
        if members:
            for j in [i] + members:
                group_of[j] = len(groups)
            groups.append([i] + members)
    for i in order:
        if i not in group_of:
            g = group_of[min(near[i], key=lambda j: (-usage[j], j))]
            group_of[i] = g
            groups[g].append(i)
    return groups


def find_version_clusters(variants, strict, min_overlap=WORD_OVERLAP):
    """Link variants that are versions of each other and return
    (clusters, cluster_id_per_variant, lookalike_groups).

    clusters: lists of variants, most-used first, 2+ variants each.
    lookalike_groups: same shape, for questions whose wording is close but
    whose wording AND credited answer both differ. Those are near-twins that
    ask different things (a statement and its negation, "most likely" vs
    "least likely"), so they are kept out of the clusters."""
    by_stem = {}
    for idx, v in enumerate(variants):
        by_stem.setdefault(v.stem, []).append(idx)
    stems = list(by_stem)

    parent = list(range(len(variants)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    weights = word_weights(stems)
    twin_pairs = []
    pictures = [frozenset(IMAGE_NAME_RE.findall(v.rep.text)) if v.occurrences else frozenset() for v in variants]

    def consider(i, j, stem_score):
        vi, vj = variants[i], variants[j]
        # Two stems that each show a picture, and not the same pictures, are
        # different questions however alike the wording: "What is wrong with
        # this slide?" under two slides. To the text comparison a picture is
        # one word in the stem, so it has to be ruled on here. (A picture
        # added to or dropped from a question is still an edit.)
        if different_pictures(pictures[i], pictures[j]):
            return
        reworded = vi.stem != vj.stem
        if reworded and word_overlap(vi.stem, vj.stem, weights) < min_overlap:
            return  # same frame, different content
        if not version_scores(stem_score, vi, vj, strict):
            return
        if reworded and key_differs(vi, vj):
            twin_pairs.append((i, j))
        else:
            parent[find(i)] = find(j)

    # same stem, different choices or key
    for idxs in by_stem.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                consider(idxs[a], idxs[b], 100.0)

    # different stems: vectorized stem comparison, then the more expensive
    # choice comparison only for pairs that clear the stem floor. Done in row
    # blocks so memory stays at STEM_BLOCK x N bytes however large the export is.
    # the lowest stem score any rule in version_scores() can accept
    cutoff = max(0.0, min(LOOSE_STEM, strict - BOTH_MARGIN))
    for start in range(0, len(stems), STEM_BLOCK):
        # each block against itself and everything after it: every pair once
        m = process.cdist(stems[start:start + STEM_BLOCK], stems[start:], scorer=fuzz.ratio,
                          score_cutoff=cutoff, dtype=np.uint8, workers=-1)
        for r, c in np.argwhere(m > 0):
            if c <= r:
                continue
            si, sj = start + int(r), start + int(c)
            score = float(m[r, c])
            for i in by_stem[stems[si]]:
                for j in by_stem[stems[sj]]:
                    consider(i, j, score)

    roots = [find(i) for i in range(len(variants))]
    grouped = {}
    for idx, r in enumerate(roots):
        grouped.setdefault(r, []).append(variants[idx])
    clusters = [sorted(g, key=lambda v: -len(v.occurrences))
                for g in grouped.values() if len(g) > 1]
    # Look-alike groups. A different-answer pair that clustering joined anyway
    # (through a third version) already shows in its cluster with "Answer key
    # differs", so it is skipped. "Reads alike" does not carry across a chain
    # the way "is a version of" does, so groups are not connected components:
    # each is one question plus the questions directly linked to it.
    groups = group_lookalikes([(i, j) for i, j in twin_pairs if roots[i] != roots[j]],
                              [len(v.occurrences) for v in variants])
    lookalikes = [[variants[i] for i in g] for g in groups]
    return clusters, roots, lookalikes


def pair_rows(a, b):
    """Pair the rows (matching prompts or dropdown blank names) of two
    versions one-to-one: [(index in a, index in b, score)]. Rows pair when
    they read alike; when exactly one row is left over on each side, those
    two pair as well, since in two versions of one question that is the row
    that was rewritten."""
    pairs = [(i, j, score) for i, j, score in align_choices(a, b) if score >= LOOSE_CHOICES]
    left_a = [i for i in range(len(a)) if i not in {p[0] for p in pairs}]
    left_b = [j for j in range(len(b)) if j not in {p[1] for p in pairs}]
    if len(left_a) == len(left_b) == 1:
        pairs.append((left_a[0], left_b[0], 0.0))
    return pairs


def pair_key_differs(ref_pairs, other_pairs, ref_options=(), other_options=()):
    """Key comparison for matching rows and dropdown blanks, given
    [(prompt, answer)] on each side. Prompts are paired one-to-one and so are
    the answers, so rewording either side is not a key change; a row whose
    prompt now maps to a different answer is. Added or removed rows are left
    to the choice diff.

    ref_options and other_options are each version's full list of options.
    An answer that exists word for word on the other side is that option, not
    a rewording of something else: without this, moving a row's key to an
    unused option that reads like the old answer would go unnoticed."""
    r_all, o_all = {a for _, a in ref_pairs}, {a for _, a in other_pairs}
    same_answer = {a: a for a in o_all if a in r_all or a in ref_options}
    r_ans = sorted(a for a in r_all if a not in o_all and a not in other_options)
    o_ans = sorted(a for a in o_all if a not in same_answer)
    same_answer.update({o_ans[j]: r_ans[i] for i, j, score in align_choices(r_ans, o_ans)
                        if score >= LOOSE_CHOICES})
    for i, j, _ in pair_rows([p for p, _ in ref_pairs], [p for p, _ in other_pairs]):
        if same_answer.get(other_pairs[j][1]) != ref_pairs[i][1]:
            return True
    return False


def key_differs(ref, other):
    """True if the two versions credit different answers.

    Choices are first paired one-to-one, and a pair only counts as "the same
    choice, reworded" at LOOSE_CHOICES or better. The key is unchanged only
    if the credited choices are each other's counterparts. A credited choice
    with no counterpart (newly added, or rewritten beyond recognition) counts
    as a different key."""
    if not ref.correct or not other.correct:
        return False  # key unknown on one side
    if ref.key_pairs and other.key_pairs:
        return pair_key_differs(ref.key_pairs, other.key_pairs, ref.choices, other.choices)
    if ref.correct <= ref.choices and other.correct <= other.choices:
        rc, oc = sorted(ref.choices), sorted(other.choices)
        counterpart = {oc[j]: rc[i] for i, j, score in align_choices(rc, oc)
                       if score >= LOOSE_CHOICES}
        mapped = {counterpart.get(c) for c in other.correct}
        return mapped != set(ref.correct)
    return ref.correct != other.correct


def one_per_cluster(variants, roots, idxs):
    """The most-used variant from each cluster among idxs. Versions of one
    question are already shown together under Modified questions; here they
    stand in as a single entry."""
    best = {}
    for i in idxs:
        r = roots[i]
        if r not in best or len(variants[i].occurrences) > len(variants[best[r]].occurrences):
            best[r] = i
    return [variants[i] for i in best.values()]


def find_same_stem_unrelated(variants, roots):
    """Stems shared by variants that did NOT end up in the same cluster:
    identical wording, but choices too different to be the same question."""
    by_stem = {}
    for idx, v in enumerate(variants):
        by_stem.setdefault(v.stem, []).append(idx)
    return [one_per_cluster(variants, roots, idxs) for idxs in by_stem.values()
            if len({roots[i] for i in idxs}) > 1]


def find_same_choices_other_stem(variants, roots):
    """Identical choice sets (3+ choices) with the same credited answer,
    under stems too different to have been linked: possibly one question
    with a rewritten stem. Stock choice sets reused for different questions
    mostly have different answers, so requiring the same answer drops them."""
    by_choices = {}
    for idx, v in enumerate(variants):
        if len(v.choices) >= 3:
            by_choices.setdefault((v.choices, v.correct), []).append(idx)
    return [one_per_cluster(variants, roots, idxs) for idxs in by_choices.values()
            if len({roots[i] for i in idxs}) > 1]


def find_duplicate_containers(variants, roots, min_overlap=CONTAINER_OVERLAP, containers=None):
    """Compare whole quizzes and banks by the questions they hold.

    `containers` is the list from collect_questions(). Passing it brings in
    quizzes that store no questions and only draw from banks, which is the
    usual shape of a New Quiz; without it, quizzes and banks are rebuilt
    from the questions alone.

    Returns (identical, overlaps):
      identical: groups of containers with the same contents: the same set of
                 stored questions and, for quizzes that draw from banks, the
                 same number drawn from banks holding the same questions.
      overlaps:  [(container, [(other, exact, modified, percent), ...])] for each
                 container that has at least min_overlap percent of its
                 questions in another container at least as large. `exact`
                 counts identical questions; `modified` counts questions the
                 other container has as a modified version (same cluster).
                 Only stored questions count here, not drawn ones."""
    found = {}
    for c in containers or ():
        c.members, c.picked = set(), set()
        found[(c.source_file, c.kind, c.cid)] = c
    in_bank = {}  # (export, bank id, item id) -> variant index
    for idx, v in enumerate(variants):
        for q in v.occurrences:
            key = (q.source_file, q.source_kind, q.quiz_id)
            if key not in found:
                found[key] = Container(q.source_file, q.source_kind, q.quiz_id, q.quiz_title, q.engine)
            found[key].members.add(idx)
            if q.source_kind == "bank":
                in_bank[(q.source_file, q.quiz_id, q.item_id)] = idx
    for c in found.values():
        c.picked = {in_bank[(c.source_file, b, i)] for b, i in c.picks if (c.source_file, b, i) in in_bank}
    banks = {(c.source_file, c.cid): c for c in found.values() if c.kind == "bank"}

    def contents(c):
        drawn = Counter()
        for bank_id, count in c.draws:
            bank = banks.get((c.source_file, bank_id))
            if bank is None and bank_id not in c.missing_banks:
                continue  # the bank is in the export but was excluded by a filter: not compared
            # a bank is known by its questions, so two quizzes drawing from
            # two copies of one bank (or from two exports) still match
            what = frozenset(bank.members) if bank is not None and bank.members else (c.source_file, bank_id)
            drawn[(what, count)] += 1
        # a pick that isn't in the export stays distinct, so it never matches by
        # accident. One that is there but was excluded by a filter is left out.
        lost = frozenset((c.source_file, b, i) for b, i in c.missing)
        return frozenset(c.members), frozenset(drawn.items()), frozenset(c.picked), lost

    compared = sorted((c for c in found.values() if c.size >= MIN_CONTAINER or c.draws or c.picks),
                      key=lambda c: (c.kind, c.title.lower(), c.source_file, c.cid))
    same = {}
    for c in compared:
        same.setdefault(contents(c), []).append(c)
    identical = sorted((g for g in same.values() if len(g) > 1), key=lambda g: (-g[0].size, g[0].title.lower()))

    sized = [c for c in compared if c.size >= MIN_CONTAINER]
    cluster_sets = [{roots[i] for i in c.members} for c in sized]
    signatures = [contents(c) for c in sized]
    overlaps = []
    for a, c in enumerate(sized):
        hits = []
        for b, other in enumerate(sized):
            if a == b or signatures[a] == signatures[b]:
                continue
            # compare the smaller against the larger; equal sizes are listed once
            if c.size > other.size or (c.size == other.size and a > b):
                continue
            exact = len(c.members & other.members)
            covered = sum(1 for i in c.members if roots[i] in cluster_sets[b])
            percent = 100.0 * covered / c.size
            if percent >= min_overlap:
                hits.append((other, exact, covered - exact, percent))
        if hits:
            hits.sort(key=lambda h: (-h[3], -h[1], h[0].title.lower()))
            overlaps.append((c, hits))
    overlaps.sort(key=lambda o: (-o[1][0][3], -o[0].size, o[0].title.lower()))
    return identical, overlaps


def describe_contents(c):
    """'12 questions', 'draws 10 from 2 banks', 'uses 3 bank questions', or
    a combination."""
    parts = []
    if c.size or not (c.draws or c.picks):
        parts.append(plural(c.size, "question"))
    if c.picks:
        parts.append(f"uses {plural(len(c.picks), 'bank question')}")
    if c.draws:
        whole = {b for b, n in c.draws if n is None}   # New Quizzes: every question in the bank
        counted = [(b, n) for b, n in c.draws if n is not None]
        if whole:
            parts.append(f"uses all of {plural(len(whole), 'bank')}")
        if counted:
            parts.append(f"draws {sum(n for _, n in counted)} from "
                         f"{plural(len({b for b, _ in counted}), 'bank')}")
    return " + ".join(parts)


# ---------------------------------------------------------------------------
# Bank usage and orphan check
# ---------------------------------------------------------------------------
# A bank is in use when a quiz links to it: a question group that draws from
# it, or (New Quizzes) a single question chosen from it. A Classic quiz
# question that was copied in from a bank keeps a pointer back to the bank
# question (assessment_question_identifierref); that makes the bank "copied
# only": no quiz needs it, but its questions went somewhere.
USED_LINKED, USED_COPIED, UNUSED = "linked", "copied only", "unused"
VERDICT_SAFE, VERDICT_COVERED, VERDICT_REVIEW = "safe", "covered", "review"
VERDICT_UNIQUE, VERDICT_COPY, VERDICT_EMPTY = "unique", "keep_copy", "empty"
VERDICT_LABELS = {
    VERDICT_SAFE: "Safe to delete",
    VERDICT_COVERED: "Safe to delete if the kept copies stay",
    VERDICT_REVIEW: "Review the diffs first",
    VERDICT_UNIQUE: "Has unique questions",
    VERDICT_COPY: "Keep as the one copy",
    VERDICT_EMPTY: "Empty in the export",
}
VERDICT_HELP = {
    VERDICT_SAFE: "Every question in them is in use, unchanged, somewhere else.",
    VERDICT_COVERED: ("Every question in them is in use or in a bank to keep (\u201ckeep as the one copy\u201d "
                      "or \u201chas unique questions\u201d), unchanged. They can go as long as those banks stay; "
                      "each one\u2019s card says which."),
    VERDICT_REVIEW: ("Some questions survive only as modified versions, in use or in a bank to keep, so one of "
                     "these banks may hold the wording you want."),
    VERDICT_UNIQUE: ("They hold questions found nowhere else in the export. Whether to keep them depends on "
                     "whether you want the material, not on duplication."),
    VERDICT_COPY: ("Their leftover questions exist only in other unused banks. One bank was picked to keep for "
                   "each set of copies, so the others can go."),
    VERDICT_EMPTY: "No questions in the export.",
}
VERDICT_ORDER = (VERDICT_UNIQUE, VERDICT_COPY, VERDICT_REVIEW, VERDICT_COVERED, VERDICT_SAFE, VERDICT_EMPTY)


@dataclass
class BankUsage:
    linked_by: set = field(default_factory=set)   # quiz titles with a question group drawing from the bank
    picked_by: set = field(default_factory=set)   # New Quizzes that use single questions from it
    copied_into: set = field(default_factory=set)  # Classic quizzes holding a question copied from it

    @property
    def status(self):
        if self.linked_by or self.picked_by:
            return USED_LINKED
        return USED_COPIED if self.copied_into else UNUSED


def find_bank_usage(questions, containers, skip=frozenset()):
    """How every bank in the exports is used, worked out before any --exclude
    filter so that a filtered-out quiz still counts as using its banks.

    Returns (usage, refs_seen, refs_resolved): usage maps (export, bank id)
    to a BankUsage for every bank; refs_seen is how many quiz questions carry
    a pointer back to a bank question and refs_resolved how many of those
    point at a bank question that is in the same export. Quizzes in `skip`,
    as (export, quiz id), are left out: --only-published passes the
    unpublished ones."""
    usage = {(c.source_file, c.cid): BankUsage() for c in containers if c.kind == "bank"}
    for c in containers:
        if c.kind != "quiz" or (c.source_file, c.cid) in skip:
            continue
        for bank_id, _ in c.draws:
            if (c.source_file, bank_id) in usage:
                usage[(c.source_file, bank_id)].linked_by.add(c.title)
        for bank_id, _ in c.picks:
            if (c.source_file, bank_id) in usage:
                usage[(c.source_file, bank_id)].picked_by.add(c.title)
    # export item ids are unique in a Canvas export; if two banks ever share
    # one, the copy is credited to both rather than to whichever came last
    bank_of_item = {}
    for q in questions:
        if q.source_kind == "bank":
            bank_of_item.setdefault((q.source_file, q.item_id), set()).add(q.quiz_id)
    refs_seen = refs_resolved = 0
    for q in questions:
        if not q.bank_ref or (q.source_file, q.quiz_id) in skip:
            continue
        refs_seen += 1
        banks = [b for b in bank_of_item.get((q.source_file, q.bank_ref), ()) if (q.source_file, b) in usage]
        if banks:
            refs_resolved += 1
        for bank_id in banks:
            usage[(q.source_file, bank_id)].copied_into.add(q.quiz_title)
    return usage, refs_seen, refs_resolved


@dataclass
class BankFinding:
    """One question of a bank that is not linked, and where else it lives."""
    variant: int          # index into variants
    # "exact" / "modified": in use as is / as a modified version
    # "kept_copy" / "kept_version": not in use, but a bank kept as the one copy holds it as is / as a version
    # "unused_only": only in other unlinked banks (said of a bank that is itself kept)
    # "unique": nowhere else
    coverage: str
    places: list = field(default_factory=list)  # titles of the other quizzes and banks involved


@dataclass
class BankVerdict:
    bank: Container
    status: str
    usage: BankUsage
    verdict: str = ""
    findings: list = field(default_factory=list)   # BankFinding per distinct question
    not_compared: int = 0                          # questions removed by --exclude-type
    kept_for: list = field(default_factory=list)   # BankVerdicts that rely on this kept bank
    relies_on: list = field(default_factory=list)  # kept BankVerdicts this one relies on

    def count(self, coverage):
        return sum(1 for f in self.findings if f.coverage == coverage)


def check_orphan_banks(variants, roots, containers, usage, excluded_items=None, skip=frozenset()):
    """For each bank in `containers` that no quiz links to, sort its distinct
    questions by where else they live in the same export:

      exact        the same question is in a quiz or in a linked bank
      modified     a modified version of it is (same cluster)
      unused_only  copies or versions exist, but only in other unlinked banks
      unique       nowhere else

    Only quizzes and linked banks count as "in use": two unlinked copies of
    one bank must not vouch for each other. Questions that are not in use at
    all ("at risk") then need one bank to stay. A bank holding a question no
    other bank has must stay; after those, banks are kept greedily, the one
    holding the most at-risk questions not yet kept first, until every
    at-risk question is in a kept bank. Questions of the other banks that
    were at risk become kept_copy or kept_version.

    Returns a BankVerdict for every bank, linked ones included (with no
    verdict), sorted by title. `excluded_items` maps (export, bank id) to how
    many of its questions an --exclude-type filter removed; those could not
    be checked. Quizzes in `skip`, as (export, quiz id), do not count as in
    use."""
    excluded_items = excluded_items or {}
    linked = {key for key, u in usage.items() if u.status == USED_LINKED}
    # each bank's distinct questions, worked out here so this does not depend
    # on find_duplicate_containers() having run first
    members = {}
    for idx, v in enumerate(variants):
        for q in v.occurrences:
            if q.source_kind == "bank":
                members.setdefault((q.source_file, q.quiz_id), set()).add(idx)
    for c in containers:
        if c.kind == "bank":
            c.members = members.get((c.source_file, c.cid), set())

    def place(q):
        return (q.source_file, q.source_kind, q.quiz_id)

    def skipped(q):
        return q.source_kind == "quiz" and (q.source_file, q.quiz_id) in skip

    def in_use(q):
        return q.source_kind == "quiz" or (q.source_file, q.quiz_id) in linked

    by_root = {}
    for idx, r in enumerate(roots):
        by_root.setdefault(r, []).append(idx)

    def titles(qs):
        return sorted({q.quiz_title for q in qs}, key=str.lower)

    results, candidates = [], []
    for c in sorted((c for c in containers if c.kind == "bank"),
                    key=lambda c: (c.title.lower(), c.source_file, c.cid)):
        u = usage.get((c.source_file, c.cid), BankUsage())
        result = BankVerdict(c, u.status, u, not_compared=excluded_items.get((c.source_file, c.cid), 0))
        results.append(result)
        if u.status == USED_LINKED:
            continue
        candidates.append(result)
        here = (c.source_file, "bank", c.cid)
        for idx in sorted(c.members):
            # a skipped (unpublished) quiz is treated as if it were not there
            elsewhere = [q for q in variants[idx].occurrences
                         if q.source_file == c.source_file and place(q) != here and not skipped(q)]
            versions = [q for j in by_root.get(roots[idx], ()) if j != idx
                        for q in variants[j].occurrences
                        if q.source_file == c.source_file and place(q) != here and not skipped(q)]
            if any(in_use(q) for q in elsewhere):
                finding = BankFinding(idx, "exact", titles(q for q in elsewhere if in_use(q)))
            elif any(in_use(q) for q in versions):
                finding = BankFinding(idx, "modified", titles(q for q in versions if in_use(q)))
            elif elsewhere or versions:
                finding = BankFinding(idx, "unused_only", titles(elsewhere + versions))
            else:
                finding = BankFinding(idx, "unique")
            result.findings.append(finding)

    # ---- keep one copy of every question that is not in use anywhere
    at_risk = {}  # id(result) -> {(export, root)}
    for r in candidates:
        at_risk[id(r)] = {(r.bank.source_file, roots[f.variant]) for f in r.findings
                          if f.coverage in ("unused_only", "unique")}
    holders = {}
    for r in candidates:
        for key in at_risk[id(r)]:
            holders.setdefault(key, []).append(r)
    kept, covered = [], set()
    for r in candidates:  # a bank with a question no other bank has must stay
        if any(len(holders[key]) == 1 for key in at_risk[id(r)]):
            kept.append(r)
            covered |= at_risk[id(r)]
    while True:
        best, gain = None, 0
        for r in candidates:
            new = len(at_risk[id(r)] - covered)
            # ties: the bank with more questions, then the first by title
            if new > gain or (new == gain and new and best is not None and r.bank.size > best.bank.size):
                best, gain = r, new
        if best is None:
            break
        kept.append(best)
        covered |= at_risk[id(best)]
    kept_ids = {id(r) for r in kept}
    kept_places = {(r.bank.source_file, "bank", r.bank.cid): r for r in kept}

    for r in candidates:
        if id(r) in kept_ids:
            continue
        here = (r.bank.source_file, "bank", r.bank.cid)
        for f in r.findings:
            if f.coverage not in ("unused_only", "unique"):
                continue
            exact = [kept_places[place(q)] for q in variants[f.variant].occurrences
                     if place(q) in kept_places and place(q) != here]
            version = [kept_places[place(q)] for j in by_root.get(roots[f.variant], ()) if j != f.variant
                       for q in variants[j].occurrences if place(q) in kept_places and place(q) != here]
            keepers = exact or version
            f.coverage = "kept_copy" if exact else "kept_version"
            f.places = sorted({k.bank.title for k in keepers}, key=str.lower)
            for k in keepers:
                if r not in k.kept_for:
                    k.kept_for.append(r)
                if k not in r.relies_on:
                    r.relies_on.append(k)

    for r in candidates:
        if not r.findings and not r.not_compared:
            r.verdict = VERDICT_EMPTY
        elif r.count("unique"):
            r.verdict = VERDICT_UNIQUE
        elif id(r) in kept_ids:
            r.verdict = VERDICT_COPY
        elif r.count("modified") or r.count("kept_version") or r.not_compared:
            r.verdict = VERDICT_REVIEW
        elif r.count("kept_copy"):
            r.verdict = VERDICT_COVERED
        else:
            r.verdict = VERDICT_SAFE
    return results


def find_repeated_choices(variants):
    """Questions that list the same answer choice twice. A copy with a
    repeated choice has the same choice *set* as a clean copy, so both sit in
    one variant; every occurrence is checked and only the offending copies
    are returned."""
    found = []
    for v in variants:
        # these list the same options once per row or per blank by design
        if v.qtype in ("matching_question", "multiple_dropdowns_question", "fill_in_multiple_blanks_question"):
            continue
        bad = [q for q in v.occurrences if len(q.choices) != len(q.norm_choices)]
        if bad:
            found.append(Variant(v.stem, v.choices, v.correct, v.qtype, bad))
    return found


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def esc(s):
    return html.escape(s or "")


def word_diff(old, new):
    """`new` as HTML, with words added relative to `old` in <ins> and words
    removed in <del>."""
    a, b = old.split(), new.split()
    # compare words the same way questions are compared (case and punctuation
    # ignored), so the diff only lights up differences that actually count
    ka, kb = [normalize(w) for w in a], [normalize(w) for w in b]
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(esc(" ".join(b[j1:j2])))
        else:
            if i2 > i1:
                out.append(f"<del>{esc(' '.join(a[i1:i2]))}</del>")
            if j2 > j1:
                out.append(f"<ins>{esc(' '.join(b[j1:j2]))}</ins>")
    return " ".join(out)


def render_locations(variant, ctx):
    ids = {}
    for q in variant.occurrences:
        ids.setdefault((q.source_kind, q.quiz_title, q.source_file, q.quiz_id, q.engine), []).append(q.item_id)
    items = []
    for (kind, title, src, cid, engine), item_ids in sorted(ids.items(), key=lambda kv: (kv[0][0], kv[0][1].lower())):
        n = len(item_ids)
        tip = esc("Export item id" + ("s: " if n > 1 else ": ") + ", ".join(item_ids))
        extra = f" <span class='src'>({esc(src)})</span>" if ctx["show_source"] else ""
        times = f" &times;{n}" if n > 1 else ""
        used_by = ""
        if kind == "bank":
            quizzes = sorted(ctx["bank_usage"].get((src, cid), ()), key=str.lower)
            if quizzes:
                used_by = f" <span class='src'>&mdash; drawn on by: {esc(', '.join(quizzes))}</span>"
            chosen = sorted({t for i in item_ids for t in ctx.get("picked_by", {}).get((src, cid, i), ())},
                            key=str.lower)
            if chosen:
                used_by += f" <span class='src'>&mdash; used as a question in: {esc(', '.join(chosen))}</span>"
        items.append(f"<li title='{tip}'>[{esc(kind_label(kind, engine))}] {esc(title)}{times}{extra}{used_by}</li>")
    total = len(variant.occurrences)
    label = f"Used in {total} place{'s' if total != 1 else ''}"
    open_attr = " open" if len(items) <= 4 else ""
    return f"<details class='locs'{open_attr}><summary>{label}</summary><ul>{''.join(items)}</ul></details>"


def render_choices(q, ref=None):
    """Choice list for q. With `ref`, each choice is diffed against its
    counterpart in the reference version (same one-to-one pairing the
    answer-key check uses)."""
    correct = q.norm_correct
    any_marked = False
    rows = []
    counterpart, matched = {}, set()
    if ref is not None:
        pairs = align_choices([normalize(c) for c in q.choices], [normalize(c) for c in ref.choices])
        counterpart = {i: (j, score) for i, j, score in pairs if score >= LOOSE_CHOICES}
        matched = {j for j, _ in counterpart.values()}
    for i, c in enumerate(q.choices):
        cls, body = "", esc(c)
        if ref is not None:
            if i not in counterpart:
                cls, body = "changed", f"<ins>{esc(c)}</ins>"
            elif counterpart[i][1] < 100:
                cls, body = "changed", word_diff(ref.choices[counterpart[i][0]], c)
        mark = ""
        if normalize(c) in correct:
            any_marked = True
            mark = "<span class='key' title='credited answer'>&#10003;</span> "
        rows.append(f"<li class='{cls}'>{mark}{body}</li>")
    if ref is not None:
        for j, c in enumerate(ref.choices):
            if j not in matched:
                rows.append(f"<li class='changed'><del>{esc(c)}</del></li>")
    out = f"<ul class='choices'>{''.join(rows)}</ul>" if rows else ""
    if q.correct and not any_marked:
        # matching rows, dropdown blanks and typed answers: mark the entries
        # that the reference version doesn't have, and the ones it has that this lacks
        theirs = {normalize(k) for k in ref.correct} if ref is not None and ref.correct else None
        mine = {normalize(k) for k in q.correct}
        if ref is not None and q.key_pairs and ref.key_pairs:
            # rows and blanks: show a changed answer in place, next to its prompt.
            # Prompts are paired the way pair_key_differs() pairs them, so a
            # reworded prompt is diffed in place instead of shown as a removed
            # row and an added one.
            rows = pair_rows([normalize(p) for p, _ in q.key_pairs], [normalize(p) for p, _ in ref.key_pairs])
            twin = {i: (j, score) for i, j, score in rows}
            paired = {j for j, _ in twin.values()}
            parts = []
            for i, (p, a) in enumerate(q.key_pairs):
                if i not in twin:
                    parts.append(f"<ins>{esc(p)} \u2192 {esc(a)}</ins>")
                    continue
                j, score = twin[i]
                old_p, was = ref.key_pairs[j]
                prompt = esc(p) if score >= 100 else word_diff(old_p, p)
                if normalize(was) != normalize(a):
                    parts.append(f"{prompt} \u2192 <del>{esc(was)}</del> <ins>{esc(a)}</ins>")
                else:
                    parts.append(f"{prompt} \u2192 {esc(a)}")
            parts += [f"<del>{esc(p)} \u2192 {esc(a)}</del>" for j, (p, a) in enumerate(ref.key_pairs)
                      if j not in paired]
        else:
            parts = [f"<ins>{esc(k)}</ins>" if theirs is not None and normalize(k) not in theirs else esc(k)
                     for k in q.correct]
            if theirs is not None:
                parts += [f"<del>{esc(k)}</del>" for k in ref.correct if normalize(k) not in mine]
        out += f"<div class='keyline'>Answer key: {'; '.join(parts)}</div>"
    elif not q.choices and q.question_type in CHOICE_TYPES:
        out += ("<div class='keyline unread'>No choices were found for this question in the export, "
                "so only its wording was compared.</div>")
    elif q.choices and not q.correct:
        out += "<div class='keyline unread'>Answer key could not be read, so it was not compared.</div>"
    return out


def render_variant(variant, ctx, heading="", ref=None, badges=""):
    q = variant.rep
    stem = word_diff(ref.rep.text, q.text) if ref is not None else esc(q.text)
    head = f"<div class='vhead'>{heading}{badges}</div>" if heading or badges else ""
    def as_number(p):
        try:
            return float(p)
        except ValueError:
            return float("inf")

    pts = sorted({o.points for o in variant.occurrences if o.points}, key=as_number)
    points = f" &middot; {esc(' / '.join(pts))} pts" if pts else ""
    return f"""
    <div class="qcard">
      {head}
      <div class="qmeta">{esc(q.question_type)}{points}</div>
      <div class="qtext">{stem}</div>
      {render_choices(q, ref.rep if ref is not None else None)}
      {render_locations(variant, ctx)}
    </div>
    """


def compare_versions(ref, v):
    """What differs between version v and the reference version, as data.
    Similarities are 0-100 and only given for the part that changed."""
    stem_changed, choices_changed = ref.stem != v.stem, ref.choices != v.choices
    keyed = key_differs(ref, v)
    return {
        "stem_reworded": stem_changed,
        "stem_similarity": round(fuzz.ratio(ref.stem, v.stem), 1) if stem_changed else None,
        "choices_changed": choices_changed,
        "choice_similarity": round(choice_score(ref.choices, v.choices), 1) if choices_changed else None,
        "question_type_differs": ref.qtype != v.qtype,
        "answer_key_differs": keyed,
        # key_differs() is False when either key is unknown, so this is the
        # "can't tell" case: one version has a readable key and the other doesn't
        "answer_key_unread_on_one_side": not keyed and bool(ref.correct) != bool(v.correct),
    }


def describe_change(ref, v):
    """Badges saying what differs between version v and the reference."""
    diff = compare_versions(ref, v)
    tags = []
    if diff["stem_reworded"]:
        tags.append(("Stem reworded", f"{diff['stem_similarity']:.0f}% similar", ""))
    if diff["choices_changed"]:
        tags.append(("Choices changed", f"{diff['choice_similarity']:.0f}% similar", ""))
    if diff["question_type_differs"]:
        tags.append(("Question type differs", "", ""))
    keyed = diff["answer_key_differs"]
    if keyed:
        tags.append(("Answer key differs", "", "alert"))
    elif diff["answer_key_unread_on_one_side"]:
        tags.append(("Answer key unread on one side", "", ""))
    html_tags = "".join(
        f"<span class='badge {cls}'>{esc(name)}{' &middot; ' + esc(detail) if detail else ''}</span>"
        for name, detail, cls in tags
    )
    return html_tags, keyed


def order_clusters(groups):
    """Clusters in report order: those whose versions disagree on the answer
    key first, then by how many places the question appears."""
    def rank(cluster):
        keyed = any(key_differs(cluster[0], v) for v in cluster[1:])
        return (not keyed, -sum(len(v.occurrences) for v in cluster))
    return sorted(groups, key=rank)


def build_json_report(clusters, lookalikes, exact_variants, unrelated_groups, same_choice_groups, repeated,
                      identical_containers, container_overlaps, unresolved, settings, ctx):
    """The report's findings as plain data for json.dump(): the same
    sections as the HTML report, in the order they are passed in."""
    containers = ctx["containers"]
    banks = {(c.source_file, c.cid): c for c in containers if c.kind == "bank"}

    def used_by(src, cid):
        return sorted(ctx["bank_usage"].get((src, cid), ()), key=str.lower)

    def ref(c):
        return {"export": c.source_file, "kind": kind_label(c.kind, c.engine), "title": c.title, "id": c.cid}

    def bank_title(c, bank_id):
        bank = banks.get((c.source_file, bank_id))
        if bank is None:  # excluded by a filter, or (None) not in the export at all
            return ctx.get("excluded_banks", {}).get((c.source_file, bank_id))
        return bank.title

    def container(c):
        out = ref(c)
        out["own_questions"] = c.size
        if c.kind == "bank":
            out["drawn_on_by"] = used_by(c.source_file, c.cid)
        else:
            # count is null when the quiz uses every question in the bank
            out["draws"] = [{"bank_id": b, "bank_title": bank_title(c, b), "count": n} for b, n in c.draws]
            out["picks"] = [{"bank_id": b, "bank_title": bank_title(c, b), "item_id": i} for b, i in c.picks]
            out["picks_not_found"] = c.lost_picks
            # draws from banks left out with --exclude-title are not part of any comparison
            out["published"] = c.published
            out["draws_from_excluded_banks"] = sum(1 for b, _ in c.draws
                                                   if (c.source_file, b) in ctx.get("excluded_banks", {}))
        return out

    def question(v):
        q = v.rep
        places = []
        for o in v.occurrences:
            place = {"export": o.source_file, "kind": kind_label(o.source_kind, o.engine),
                     "title": o.quiz_title, "container_id": o.quiz_id, "item_id": o.item_id,
                     "points": o.points or None}
            if o.source_kind == "bank":
                place["drawn_on_by"] = used_by(o.source_file, o.quiz_id)
                place["used_as_question_in"] = sorted(
                    ctx.get("picked_by", {}).get((o.source_file, o.quiz_id, o.item_id), ()), key=str.lower)
            places.append(place)
        return {"question_type": q.question_type, "stem": q.text, "choices": list(q.choices),
                "answer_key": list(q.correct), "locations": places}

    def cluster(group):
        first = group[0]
        versions = [dict(question(first), version="A", changes_from_a=None)]
        for n, v in enumerate(group[1:], 1):
            letter = chr(ord("A") + n) if n < 26 else f"#{n + 1}"
            versions.append(dict(question(v), version=letter, changes_from_a=compare_versions(first, v)))
        return {"answer_key_differs": any(v["changes_from_a"] and v["changes_from_a"]["answer_key_differs"]
                                          for v in versions),
                "occurrences": sum(len(v.occurrences) for v in group),
                "versions": versions}

    def bank_audit(b):
        out = ref(b.bank)
        u = b.usage
        out.update({"questions": b.bank.item_count, "distinct_questions": b.bank.size, "status": b.status,
                    "linked_by": sorted(u.linked_by, key=str.lower),
                    "questions_chosen_by": sorted(u.picked_by, key=str.lower),
                    "copied_into": sorted(u.copied_into, key=str.lower),
                    "verdict": b.verdict or None})
        if b.verdict:
            out["verdict_label"] = VERDICT_LABELS[b.verdict]
            out["counts"] = {k: b.count(k) for k in ("exact", "modified", "kept_copy", "kept_version",
                                                     "unused_only", "unique")}
            out["counts"]["not_compared"] = b.not_compared
            out["kept_as_the_one_copy_for"] = [ref(o.bank) for o in b.kept_for]
            out["relies_on_keeping"] = [ref(o.bank) for o in b.relies_on]
            out["questions_to_review"] = [
                {"coverage": f.coverage, "stem": ctx["variants"][f.variant].rep.text, "places": f.places,
                 "modified_group": ctx.get("cluster_of", {}).get(id(ctx["variants"][f.variant]))
                 if f.coverage in ("modified", "kept_version") else None}
                for f in b.findings if f.coverage not in ("exact", "kept_copy")]
        return out

    quizzes = [c for c in containers if c.kind == "quiz"]
    return {
        "generated": date.today().isoformat(),
        "classic_bank_links": {"quiz_questions_pointing_to_a_bank_question": ctx.get("refs_seen", 0),
                               "found_in_export": ctx.get("refs_resolved", 0)},
        "settings": settings,
        "summary": {
            "questions": sum(len(v.occurrences) for v in ctx["variants"]),
            "distinct_questions": len(ctx["variants"]),
            "quizzes": len(quizzes),
            "banks": len(containers) - len(quizzes),
            "new_quizzes": sum(c.engine == "new" for c in quizzes),
            "item_banks": sum(c.engine == "new" for c in containers if c.kind == "bank"),
        },
        # how the images inside questions were told apart
        "question_images": {"compared_by_content": ctx["images"].by_content,
                            "compared_by_file_name": ctx["images"].by_name,
                            "compared_by_web_address": ctx["images"].by_address},
        "bank_usage_audit": [bank_audit(b) for b in ctx.get("bank_audit", [])],
        "quizzes_and_banks": [container(c) for c in sorted(
            containers, key=lambda c: (c.kind, c.title.lower(), c.source_file, c.cid))],
        "identical_quizzes_and_banks": [[container(c) for c in group] for group in identical_containers],
        "mostly_contained": [
            {"container": ref(c),
             "also_in": [{"container": ref(other), "identical": exact, "as_modified_versions": modified,
                          "percent": round(percent, 1)} for other, exact, modified, percent in hits]}
            for c, hits in container_overlaps],
        "quizzes_with_nothing_in_export": [ref(c) for c in quizzes if not (c.item_count or c.draws or c.picks)],
        "modified_questions": [cluster(g) for g in clusters],
        "similar_wording_different_answer": [cluster(g) for g in lookalikes],
        "exact_duplicates": [question(v) for v in sorted(exact_variants, key=lambda v: -len(v.occurrences))],
        "same_stem_unrelated_choices": [[question(v) for v in g] for g in unrelated_groups],
        "same_choices_and_answer_different_stem": [[question(v) for v in g] for g in same_choice_groups],
        "repeated_answer_choices": [question(v) for v in repeated],
        "unresolved_bank_references": [
            {"export": src, "quiz": title, "bank_id": bank, "same_id_found_in": elsewhere or None}
            for src, title, bank, elsewhere in unresolved],
    }


def build_html_report(clusters, lookalikes, exact_variants, unrelated_groups, same_choice_groups, repeated,
                      identical_containers, container_overlaps, unresolved, threshold,
                      total_questions, total_quizzes, total_banks, ctx):
    def container_label(c):
        src = f" <span class='src'>({esc(c.source_file)})</span>" if ctx["show_source"] else ""
        used = ""
        if c.kind == "bank":
            quizzes = sorted(ctx["bank_usage"].get((c.source_file, c.cid), ()), key=str.lower)
            if quizzes:
                used = f" <span class='src'>&mdash; drawn on by: {esc(', '.join(quizzes))}</span>"
        return (f"[{esc(kind_label(c.kind, c.engine))}] <strong>{esc(c.title)}</strong> &middot; "
                f"{esc(describe_contents(c))}{src}{used}")

    identical_html = ""
    for group in identical_containers:
        rows = "".join(f"<li>{container_label(c)}</li>" for c in group)
        first = group[0]
        if first.picks:
            head = f"{len(group)} quizzes with the same contents, including the same questions used from banks"
        elif not first.draws:
            head = f"{len(group)} with the same {first.size} questions"
        elif first.size:
            head = (f"{len(group)} quizzes with the same {first.size} questions "
                    "that also pull the same number from the same banks")
        else:
            head = f"{len(group)} quizzes that pull the same number of questions from the same banks"
        excluded = ctx.get("excluded_banks", {})
        if any((c.source_file, b) in excluded for c in group for b, _ in c.draws):
            head += (" <span class='src'>&mdash; draws from banks excluded by --exclude-title were not "
                     "compared, so these may still differ there</span>")
        identical_html += f"""
            <div class="match-group exact">
              <h3>{head}</h3>
              <ul class="containers">{rows}</ul>
            </div>
            """
    identical_html = identical_html or "<p class='empty'>No two quizzes or banks have exactly the same contents.</p>"

    # ---- quizzes that pull questions from banks
    built_html = ""
    quizzes = sorted((c for c in ctx["containers"] if c.kind == "quiz"), key=lambda c: (c.title.lower(), c.source_file))
    source = lambda c: f" <span class='src'>({esc(c.source_file)})</span>" if ctx["show_source"] else ""
    built = [c for c in quizzes if c.draws or c.picks]
    if built:
        banks = {(c.source_file, c.cid): c for c in ctx["containers"] if c.kind == "bank"}

        def bank_name(c, bank_id):
            bank = banks.get((c.source_file, bank_id))
            if bank is None and (c.source_file, bank_id) in ctx.get("excluded_banks", {}):
                title = ctx["excluded_banks"][(c.source_file, bank_id)]
                return f"{esc(title)} <span class='src'>(excluded by --exclude-title)</span>"
            if bank is None:
                return f"a bank that is not in this export <span class='src'>({esc(bank_id)})</span>"
            return (f"[{esc(kind_label('bank', bank.engine))}] {esc(bank.title)} "
                    f"<span class='src'>({plural(bank.size, 'question')})</span>")

        rows = ""
        for c in built:
            pulls = ""
            for bank_id, n in sorted(Counter(b for b, _ in c.picks).items(), key=lambda kv: kv[0]):
                pulls += f"<li>{plural(n, 'chosen question')} from {bank_name(c, bank_id)}</li>"
            for bank_id, count in c.draws:
                amount = "all questions" if count is None else f"{count} at random"
                pulls += f"<li>{amount} from {bank_name(c, bank_id)}</li>"
            note = (f"<li class='src'>{plural(c.lost_picks, 'chosen question')} could not be found in the "
                    "export</li>" if c.lost_picks else "")
            rows += (f"<tr><td>[{esc(kind_label(c.kind, c.engine))}] <strong>{esc(c.title)}</strong>{source(c)}</td>"
                     f"<td>{c.size}</td><td><ul class='pulls'>{pulls}{note}</ul></td></tr>")
        built_html = f"""
  <h3 id="from-banks">Quizzes that use questions from banks</h3>
  <p>A quiz can use bank questions in two ways. A question group gives each student a random
  set from a bank, and a New Quiz can also use single questions chosen from an item bank.
  Either way the questions are stored in the bank and not in the quiz, so a quiz built this
  way has few or no questions of its own to compare. This is common for New Quizzes.</p>
  <table>
    <tr><th>Quiz</th><th>Own questions</th><th>From banks</th></tr>
    {rows}
  </table>
"""
    hollow = [c for c in quizzes if not (c.item_count or c.draws or c.picks)]
    if hollow:
        names = "".join(f"<li>[{esc(kind_label(c.kind, c.engine))}] <strong>{esc(c.title)}</strong>{source(c)}</li>"
                        for c in hollow)
        built_html += f"""
  <h3 id="nothing-exported">Quizzes with nothing in the export</h3>
  <p>These quizzes are listed in the export with no questions and no link to a bank, so
  there was nothing to compare. Either the quiz is empty in Canvas or its content was
  not exported.</p>
  <ul class="containers">{names}</ul>
"""

    overlap_html = ""
    for c, hits in container_overlaps:
        rows = ""
        for other, exact, modified, percent in hits:
            detail = f"{exact} identical"
            if modified:
                detail += f" + {modified} as modified versions"
            whole = " <span class='badge'>fully contained</span>" if exact + modified == c.size else ""
            rows += (f"<li>{exact + modified} of {c.size} ({percent:.0f}%) are also in "
                     f"{container_label(other)} <span class='src'>&mdash; {detail}</span>{whole}</li>")
        overlap_html += f"""
            <div class="match-group near">
              <h3>{container_label(c)}</h3>
              <ul class="containers">{rows}</ul>
            </div>
            """
    overlap_html = overlap_html or "<p class='empty'>None found at this setting.</p>"

    def render_clusters(groups, empty, anchor=None):
        rendered = []
        for number, cluster in enumerate(groups, 1):
            ref = cluster[0]
            lead = len(ref.occurrences) > max(len(v.occurrences) for v in cluster[1:])
            cards = [render_variant(ref, ctx, heading="Version A" + (
                " <span class='muted'>(most used)</span>" if lead else ""))]
            any_key = False
            for n, v in enumerate(cluster[1:], 1):
                badges, keyed = describe_change(ref, v)
                any_key = any_key or keyed
                letter = chr(ord("A") + n) if n < 26 else f"#{n + 1}"
                cards.append(render_variant(v, ctx, heading=f"Version {letter} ", ref=ref, badges=badges))
            total = sum(len(v.occurrences) for v in cluster)
            ident = f' id="{anchor}-{number}"' if anchor else ""
            rendered.append((any_key, total, f"""
            <div class="match-group {'warn' if any_key else 'near'}"{ident}>
              <h3>{f"Group {number} &middot; " if anchor else ""}{len(cluster)} versions &middot; {total} occurrences{' &middot; answer key differs' if any_key else ''}</h3>
              <div class="cards">{''.join(cards)}</div>
            </div>
            """))
        return ("".join(r[2] for r in rendered) or f"<p class='empty'>{empty}</p>",
                sum(1 for r in rendered if r[0]))

    modified_html, key_clusters = render_clusters(clusters, "No modified questions found at these settings.",
                                                  anchor="modified")
    lookalike_html, _ = render_clusters(lookalikes, "None found.")

    # ---- exact duplicates
    exact_html = ""
    for v in sorted(exact_variants, key=lambda v: -len(v.occurrences)):
        exact_html += f"""
            <div class="match-group exact">
              <div class="cards">{render_variant(v, ctx)}</div>
            </div>
            """
    exact_html = exact_html or "<p class='empty'>No exact duplicates found.</p>"

    # ---- same stem, unrelated choices
    unrelated_html = ""
    for group in unrelated_groups:
        cards = "".join(render_variant(v, ctx) for v in group)
        unrelated_html += f"""
            <div class="match-group plain">
              <div class="cards">{cards}</div>
            </div>
            """
    unrelated_html = unrelated_html or "<p class='empty'>None found.</p>"

    def plain_groups(groups):
        out = ""
        for group in groups:
            cards = "".join(render_variant(v, ctx) for v in group)
            out += f"""
            <div class="match-group plain">
              <div class="cards">{cards}</div>
            </div>
            """
        return out or "<p class='empty'>None found.</p>"

    same_choice_html = plain_groups(same_choice_groups)
    repeated_html = plain_groups([[v] for v in repeated])

    unresolved_html = ""
    if unresolved:
        rows = "".join(
            f"<tr><td>{esc(src)}</td><td>{esc(title)}</td><td>{esc(bank)}</td><td>{esc(elsewhere)}</td></tr>"
            for src, title, bank, elsewhere in unresolved
        )
        unresolved_html = f"""
        <h2 id="unresolved">Unresolved question bank references <a class="top" href="#contents">Contents &uarr;</a></h2>
        <p>These quizzes pull questions from a question bank via a "Group" section,
        but the bank's actual question content was not included in this export
        (Canvas only inlines banks it decides to bundle), so those questions
        could not be checked. If another export you passed contains a bank with the
        same id, it is named in the last column; that copy was compared, but it may
        not match what this course uses.</p>
        <table>
          <tr><th>Export</th><th>Quiz</th><th>Bank reference id</th><th>Same id found in</th></tr>
          {rows}
        </table>
        """

    # ---- bank usage and orphan check
    audit = ctx.get("bank_audit", [])
    variants = ctx["variants"]

    def short(text, limit=180):
        text = WS_RE.sub(" ", text).strip()
        return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"

    def names(titles):
        return esc(", ".join(sorted(titles, key=str.lower)))

    # quiz titles whose every quiz of that name is unpublished
    quiz_states = {}
    for c in ctx["containers"]:
        if c.kind == "quiz":
            quiz_states.setdefault((c.source_file, c.title), set()).add(c.published)
    unpublished_titles = {key for key, states in quiz_states.items() if states == {False}}

    def quiz_names(titles, src):
        return ", ".join(esc(t) + (" <span class='src'>(unpublished)</span>" if (src, t) in unpublished_titles else "")
                         for t in sorted(titles, key=str.lower))

    def used_by_cell(u, src):
        parts = []
        if u.linked_by:
            parts.append(f"group in: {quiz_names(u.linked_by, src)}")
        if u.picked_by:
            parts.append(f"questions chosen in: {quiz_names(u.picked_by, src)}")
        if u.copied_into:
            parts.append(f"copied into: {quiz_names(u.copied_into, src)}")
        return "<br>".join(parts)

    audit_rows = ""
    audit_groups = {v: [] for v in VERDICT_ORDER}
    anchor_of = {id(b): n for n, b in enumerate(audit, 1)}
    first_question = {}  # (export, bank id) -> text of the first question found in it
    for v in variants:
        for q in v.occurrences:
            if q.source_kind == "bank":
                first_question.setdefault((q.source_file, q.quiz_id), q.text)
    title_count = Counter((b.bank.source_file, b.bank.title.lower()) for b in audit)

    def same_name_tag(b):
        """'#4' for a bank that shares its title with another, so the two can be told apart."""
        return f"#{anchor_of[id(b)]}" if title_count[(b.bank.source_file, b.bank.title.lower())] > 1 else ""

    def tag_span(b):
        tag = same_name_tag(b)
        return f" <span class='src'>{tag}</span>" if tag else ""

    def bank_link(b):
        tag = same_name_tag(b)
        return (f"<a href='#bank-{anchor_of[id(b)]}'>\u201c{esc(b.bank.title)}\u201d"
                f"{f' <span class=src>{tag}</span>' if tag else ''}</a>")

    def bank_links(bs, limit=None):
        bs = sorted(bs, key=lambda b: (b.bank.title.lower(), anchor_of[id(b)]))
        shown = ", ".join(bank_link(b) for b in bs[:limit])
        return shown + (f" and {len(bs) - limit} more" if limit and len(bs) > limit else "")

    # same-named banks are the hard case, so those rows also show their first question and a number
    for n, b in enumerate(audit, 1):
        c = b.bank
        verdict = (f"<a href='#bank-{n}'><span class='verdict {b.verdict}'>{VERDICT_LABELS[b.verdict]}</span></a>"
                   if b.verdict else "")
        first = first_question.get((c.source_file, c.cid), "")
        hint = (f"<br><span class='src'>first question: {esc(short(first, 70))}</span>"
                if first and title_count[(c.source_file, c.title.lower())] > 1 else "")
        audit_rows += (f"<tr><td title='export id {esc(c.cid)}'>[{esc(kind_label('bank', c.engine))}] "
                       f"<strong>{esc(c.title)}</strong>{tag_span(b)}"
                       f"{source(c)}{hint}</td>"
                       f"<td>{c.item_count}</td><td>{esc(b.status)}</td><td>{used_by_cell(b.usage, c.source_file)}</td>"
                       f"<td>{verdict}</td></tr>")
        if not b.verdict:
            continue
        counts = []
        for coverage, label in (("exact", "in use as is"), ("modified", "in use as a modified version"),
                                ("kept_copy", "in a bank to keep, as is"),
                                ("kept_version", "in a bank to keep, as a modified version"),
                                ("unused_only", "also in other unused banks"), ("unique", "nowhere else")):
            if b.count(coverage):
                counts.append(f"{b.count(coverage)} {label}")
        if b.not_compared:
            counts.append(f"{b.not_compared} not compared (excluded by --exclude-type)")
        lines = ""
        for f in b.findings:
            if f.coverage in ("exact", "kept_copy"):
                continue
            stem = esc(short(variants[f.variant].rep.text))
            group = ctx["cluster_of"].get(id(variants[f.variant]))
            link = f" &middot; <a href='#modified-{group}'>group {group}</a>" if group else ""
            if f.coverage == "modified":
                where = f"a modified version is in use in {names(f.places)}{link}"
            elif f.coverage == "kept_version":
                where = f"a modified version is in a bank to keep: {names(f.places)}{link}"
            elif f.coverage == "unused_only":
                where = f"kept here; also in unused banks: {names(f.places)}"
            else:
                where = "found nowhere else in the export"
            lines += f"<li>{stem} <span class='src'>&mdash; {where}</span></li>"
        in_use = {t for f in b.findings if f.coverage == "exact" for t in f.places}
        detail = ""
        if lines:
            detail += f"<details open><summary>Questions to look at</summary><ul class='containers'>{lines}</ul></details>"
        if in_use:
            detail += (f"<details><summary>Where the exact copies are in use</summary>"
                       f"<p class='src'>{names(in_use)}</p></details>")
        copied = (f"<p class='src'>Questions were copied from it into: {names(b.usage.copied_into)}</p>"
                  if b.usage.copied_into else "")
        if b.kept_for:
            copied += f"<p class='src'>Kept as the one copy for: {bank_links(b.kept_for)}</p>"
        if b.relies_on:
            copied += f"<p class='src'>Relies on keeping: {bank_links(b.relies_on)}</p>"
        tone = {VERDICT_UNIQUE: "warn", VERDICT_COPY: "warn", VERDICT_REVIEW: "near"}.get(b.verdict, "plain")
        audit_groups[b.verdict].append(f"""
            <div class="match-group {tone}" id="bank-{n}">
              <h3>[{esc(kind_label('bank', c.engine))}] <strong>{esc(c.title)}</strong>{tag_span(b)}{source(c)} &middot;
                {plural(c.size, 'distinct question')} &middot; {esc(b.status)}
                <span class='verdict {b.verdict}'>{VERDICT_LABELS[b.verdict]}</span></h3>
              <p class='src'>{'; '.join(counts) or 'No questions in the export.'}</p>
              {copied}{detail}
            </div>
            """)
    # ---- written summary of the verdicts
    by_verdict = {v: [b for b in audit if b.verdict == v] for v in VERDICT_ORDER}

    def unique_list(banks):
        """Banks with unique questions, most first, with same-pattern names
        (chapter_1..., chapter_2...) folded together."""
        number = r"(?<![A-Za-z])(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|" \
                 r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)(?![A-Za-z])"
        def pattern(title):
            """'chapter_N-…' for 'chapter_4-_language': the words before the first
            number, the number as N, and the punctuation after it. None when no
            word comes first, so '101 final' and '2018 summer' are not a family."""
            m = re.search(number, title, re.I)
            if not m or len(re.findall(r"[A-Za-z]", title[:m.start()])) < 3:
                return None
            after = re.match(r"[^A-Za-z0-9]*", title[m.end():]).group(0)
            return title[:m.start()] + "N" + after.rstrip()

        families = {}
        for b in banks:
            key = pattern(b.bank.title)
            families.setdefault(key.lower() if key else id(b), []).append(b)
        entries = []
        for key, group in families.items():
            counts = sorted(b.count("unique") for b in group)
            if isinstance(key, str) and len(group) >= 3:
                each = (f"{counts[0]} each" if counts[0] == counts[-1] else f"{counts[0]}\u2013{counts[-1]} each")
                all_unique = all(b.count("unique") == b.bank.size for b in group)
                entries.append((sum(counts), f"the {len(group)} \u201c{esc(pattern(group[0].bank.title))}\u2026\u201d "
                                              f"banks: {each}{', all unique' if all_unique else ''}"))
            else:
                for b in group:
                    entries.append((b.count("unique"), f"{bank_link(b)}: {b.count('unique')}"
                                                       f"{' (all unique)' if b.count('unique') == b.bank.size else ''}"))
        entries.sort(key=lambda e: -e[0])
        shown = "".join(f"<li>{text}</li>" for _, text in entries[:8])
        if len(entries) > 8:
            shown += f"<li>and {len(entries) - 8} more</li>"
        return f"<ul>{shown}</ul>"

    summary_items = ""
    for v in VERDICT_ORDER:
        banks = by_verdict[v]
        if not banks:
            continue
        head = (f"<a href='#verdict-{v}'><strong>{len(banks)}</strong> "
                f"<span class='verdict {v}'>{VERDICT_LABELS[v]}</span></a> {esc(VERDICT_HELP[v])}")
        extra = ""
        if v == VERDICT_UNIQUE:
            total = sum(b.count("unique") for b in banks)
            head += f" {plural(total, 'question')} in total."
            extra = unique_list(banks)
        elif v == VERDICT_COPY:
            examples = sorted(banks, key=lambda b: (-len(b.kept_for), b.bank.title.lower()))[:8]
            extra = "<ul>" + "".join(
                (f"<li>Keep {bank_link(b)}. {plural(len(b.kept_for), 'bank relies', 'banks rely')} on it for "
                 f"questions found nowhere else in use: {bank_links(b.kept_for, 4)}.</li>" if b.kept_for else
                 f"<li>Keep {bank_link(b)}. It holds questions that are otherwise only in other banks "
                 f"kept as the one copy.</li>")
                for b in examples)
            if len(banks) > len(examples):
                extra += f"<li>and {len(banks) - len(examples)} more</li>"
            extra += "</ul>"
        elif v in (VERDICT_SAFE, VERDICT_COVERED, VERDICT_REVIEW):
            extra = f"<p class='src'>{bank_links(banks, 6)}</p>"
        summary_items += f"<li>{head}{extra}</li>"
    audit_summary = f"<ul class='audit-summary'>{summary_items}</ul>" if summary_items else ""

    published_note = (
        "Only published quizzes count here (--only-published): a bank used only by unpublished quizzes "
        "shows as unused, and deleting it breaks those quizzes." if ctx.get("only_published") else
        "Unpublished quizzes count as using their banks and holding their questions; run with "
        "--only-published to leave them out.")
    refs_note = ""
    if ctx.get("refs_seen"):
        rest = ctx["refs_seen"] - ctx["refs_resolved"]
        refs_note = (f"<p>Classic quiz questions that point back to the bank question they were made from: "
                     f"{ctx['refs_seen']}, of which {ctx['refs_resolved']} found that question in this export."
                     + (f" The other {rest} point at banks that are not in the export, often Canvas's hidden "
                        "bank for questions written directly in a quiz." if rest else "") + "</p>")
    elif ctx.get("classic_quiz_items"):
        refs_note = ("<p><strong>This export does not record which bank questions were copied into quizzes</strong>, "
                     "so no bank is shown as &ldquo;copied only&rdquo;. The verdicts are not affected: a question "
                     "copied into a quiz is still found there as an in-use copy.</p>")
    audit_detail = ""
    for v in VERDICT_ORDER:
        if audit_groups[v]:
            audit_detail += (f'<h3 class="verdict-head" id="verdict-{v}">{VERDICT_LABELS[v]} ({len(audit_groups[v])})</h3>'
                             + "".join(audit_groups[v]))
    audit_html = ""
    if audit:
        audit_html = f"""
  <h2 id="bank-audit">Bank usage and orphan check <a class="top" href="#contents">Contents &uarr;</a></h2>
  <p>A bank is <strong>linked</strong> when a quiz has a question group that draws from it, or a New Quiz uses
  single questions from it. Deleting a linked bank breaks those quizzes. A bank is <strong>copied only</strong>
  when no quiz links to it but Classic quiz questions were copied from it; the quizzes hold their own copies.
  Otherwise it is <strong>unused</strong>. Usage counts every quiz in the export, including ones left out of the
  comparison with --exclude-title.</p>
  <p>Each bank that is not linked gets a verdict from where its questions also live in the same export.
  Only quizzes and linked banks count as in use, so two unused copies of one bank do not vouch for each other.
  For questions that are not in use anywhere, one bank is picked to keep, so the other copies can go: first
  every bank holding a question no other bank has, then the bank holding the most of the rest, and so on.</p>
  {audit_summary}
  <p>A quiz in another course can also draw from a bank, and the export cannot show that.
  {published_note}</p>
  {refs_note}
  <table>
    <tr><th>Bank</th><th>Questions</th><th>Status</th><th>Used by</th><th>Verdict</th></tr>
    {audit_rows}
  </table>
  {audit_detail}
"""

    bank_summary = ""
    if audit:
        linked_n = sum(b.status == USED_LINKED for b in audit)
        bank_summary = (f"{linked_n} of {plural(len(audit), 'bank')} linked to a quiz; the rest: "
                        + ", ".join(f"{VERDICT_LABELS[v].lower()} {len(audit_groups[v])}"
                                    for v in VERDICT_ORDER if audit_groups[v]) + ".")

    def toc_item(anchor, title, count, sub=""):
        shown = "" if count == "" else f" <span class='src'>{count}</span>"
        return f"<li><a href='#{anchor}'>{title}</a>{shown}{sub}</li>"

    sub = toc_item("identical", "Exactly the same contents", len(identical_containers))
    sub += toc_item("mostly-contained", "Mostly contained in another", len(container_overlaps))
    if built:
        sub += toc_item("from-banks", "Quizzes that use questions from banks", len(built))
    if hollow:
        sub += toc_item("nothing-exported", "Quizzes with nothing in the export", len(hollow))
    toc = ""
    if audit:
        verdict_sub = "".join(toc_item(f"verdict-{v}", VERDICT_LABELS[v], len(audit_groups[v]))
                              for v in VERDICT_ORDER if audit_groups[v])
        toc += toc_item("bank-audit", "Bank usage and orphan check", "",
                        f"<ul>{verdict_sub}</ul>" if verdict_sub else "")
    toc += toc_item("quizzes-and-banks", "Duplicate quizzes and banks", "", f"<ul>{sub}</ul>")
    toc += toc_item("modified", "Modified questions", len(clusters))
    toc += toc_item("similar-wording", "Similar wording, different answer", len(lookalikes))
    toc += toc_item("exact", "Exact duplicates", len(exact_variants))
    toc += toc_item("same-stem", "Same stem, unrelated choices", len(unrelated_groups))
    toc += toc_item("same-choices", "Same choices and answer, different stem", len(same_choice_groups))
    toc += toc_item("repeated", "Repeated answer choices", len(repeated))
    if unresolved:
        toc += toc_item("unresolved", "Unresolved question bank references", len(unresolved))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Canvas Quiz Duplicate Report</title>
<style>
  :root {{
    --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --border: #e0e0e0;
    --exact-bg: #fff4f4; --exact-border: #e57373;
    --near-bg: #fff9e6; --near-border: #e0c34a;
    --warn-bg: #fff0e0; --warn-border: #e08a3d;
    --card-bg: #fafafa;
    --ins-bg: #d4f5d4; --ins-fg: #14501a; --del-bg: #fbdada; --del-fg: #8a1f1f;
    --alert: #c62828; --key: #2e7d32; --review-fg: #8a6400;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1a1a1a; --fg: #eee; --muted: #aaa; --border: #333;
      --exact-bg: #2a1a1a; --exact-border: #a54a4a;
      --near-bg: #2a260f; --near-border: #a5904a;
      --warn-bg: #2a1f0f; --warn-border: #a5723d;
      --card-bg: #232323;
      --ins-bg: #1e4023; --ins-fg: #b9f0bf; --del-bg: #4a2020; --del-fg: #f5b5b5;
      --alert: #ef6c6c; --key: #7fd087; --review-fg: #e0c34a;
    }}
  }}
  body {{ background: var(--bg); color: var(--fg); font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 24px 16px 80px; line-height: 1.5; }}
  h1 {{ font-size: 1.5rem; }}
  h2 {{ margin-top: 2.5rem; border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
  h3 {{ font-size: 0.95rem; margin: 0 0 8px; }}
  .top {{ float: right; font-size: 0.75rem; font-weight: 400; margin-top: 0.6em; }}
  .toc {{ border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; font-size: 0.9rem; }}
  .toc ul {{ margin: 6px 0 0; padding-left: 20px; }}
  a {{ color: inherit; }}
  h2, h3[id] {{ scroll-margin-top: 12px; }}
  .summary {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1.5rem; }}
  .match-group {{ border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; border: 1px solid var(--border); }}
  .match-group.exact {{ background: var(--exact-bg); border-color: var(--exact-border); }}
  .match-group.near {{ background: var(--near-bg); border-color: var(--near-border); }}
  .match-group.warn {{ background: var(--warn-bg); border-color: var(--warn-border); }}
  .cards {{ display: flex; flex-direction: column; gap: 8px; }}
  .qcard {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; }}
  .qmeta {{ font-size: 0.75rem; color: var(--muted); margin-bottom: 4px; }}
  .qtext {{ font-weight: 500; }}
  .vhead {{ font-size: 0.85rem; font-weight: 600; margin-bottom: 2px; }}
  .muted, .src {{ color: var(--muted); font-weight: 400; }}
  .badge {{ display: inline-block; font-size: 0.72rem; font-weight: 500; border: 1px solid var(--border); border-radius: 10px; padding: 0 8px; margin-left: 6px; color: var(--muted); }}
  .badge.alert {{ color: var(--alert); border-color: var(--alert); }}
  .choices {{ margin: 6px 0 0; padding-left: 20px; font-size: 0.9rem; color: var(--muted); }}
  .choices li.changed {{ color: var(--fg); }}
  .key {{ color: var(--key); font-weight: 700; }}
  .keyline {{ font-size: 0.85rem; color: var(--muted); margin-top: 4px; }}
  .keyline.unread {{ font-style: italic; }}
  ins {{ background: var(--ins-bg); color: var(--ins-fg); text-decoration: none; border-radius: 3px; padding: 0 2px; }}
  del {{ background: var(--del-bg); color: var(--del-fg); border-radius: 3px; padding: 0 2px; }}
  .containers {{ margin: 4px 0 0; padding-left: 20px; font-size: 0.9rem; }}
  .match-group h3 strong, .containers strong {{ font-weight: 600; }}
  .pulls {{ margin: 0; padding-left: 18px; }}
  td {{ vertical-align: top; }}
  .locs {{ font-size: 0.8rem; color: var(--muted); margin-top: 6px; }}
  .locs summary {{ cursor: pointer; }}
  .locs ul {{ margin: 4px 0 0; padding-left: 20px; }}
  .empty {{ color: var(--muted); font-style: italic; }}
  .verdict {{ display: inline-block; font-size: 0.75rem; font-weight: 600; border-radius: 10px; padding: 0 8px; border: 1px solid var(--border); white-space: nowrap; }}
  .verdict.safe {{ color: var(--key); border-color: var(--key); }}
  .verdict.review {{ color: var(--review-fg); border-color: var(--review-fg); }}
  .verdict.unique, .verdict.keep_copy {{ color: var(--alert); border-color: var(--alert); }}
  .verdict.covered {{ color: var(--key); border-style: dashed; border-color: var(--key); }}
  .audit-summary > li {{ margin-bottom: 8px; }}
  .audit-summary a {{ text-decoration: none; }}
  .audit-summary ul {{ margin: 4px 0 0; padding-left: 20px; font-size: 0.9rem; }}
  .audit-summary p {{ margin: 2px 0 0; }}
  .verdict.empty {{ color: var(--muted); }}
  .verdict-head {{ margin-top: 1.6rem; }}
  details summary {{ cursor: pointer; font-size: 0.85rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }}
</style>
</head>
<body>
  <h1>Canvas Quiz Duplicate / Modified Question Report</h1>
  <p class="summary">
    {total_questions} questions parsed across {plural(total_quizzes, "quiz", "quizzes")} and {plural(total_banks, "bank")}{ctx.get("engine_note", "")}.{ctx.get("draw_note", "")}
    {bank_summary}
    {len(identical_containers)} sets of identical quizzes or banks; {len(container_overlaps)} quizzes or banks mostly contained in another.
    {len(clusters)} modified-question clusters ({key_clusters} with a differing answer key).
    {len(lookalikes)} groups of similar wording with a different answer.
    {len(exact_variants)} questions with exact duplicates.
    {len(unrelated_groups)} stems shared by unrelated questions.
    {len(same_choice_groups)} choice sets with the same answer under different stems.
    {len(repeated)} questions with a repeated choice.
    Threshold {threshold:g}, word overlap {ctx["min_overlap"]:g}. Generated {date.today().isoformat()}.{ctx.get("excluded_note", "")}{ctx.get("image_note", "")}
  </p>

  <nav class="toc" id="contents">
    <strong>Contents</strong>
    <ul>{toc}</ul>
  </nav>

{audit_html}
  <h2 id="quizzes-and-banks">Duplicate quizzes and banks <a class="top" href="#contents">Contents &uarr;</a></h2>
  <p>Whole quizzes and banks compared by the questions they hold. Question counts are
  distinct questions, so a question repeated inside one bank counts once. Quizzes and banks
  with fewer than {MIN_CONTAINER} distinct questions are left out, unless they use questions from a bank.</p>
  <h3 id="identical">Exactly the same contents</h3>
  <p>The same stored questions and, for quizzes that pull from banks, the same number pulled
  from banks holding the same questions and the same single questions used from banks.</p>
  {identical_html}
  <h3 id="mostly-contained">Mostly contained in another</h3>
  <p>Each heading is a quiz or bank with at least {ctx["container_overlap"]:g}% of its questions also in a
  quiz or bank that is the same size or larger. A modified version of a question counts as a match.
  Only questions stored in the quiz or bank count here, not ones a quiz uses from a bank.</p>
  {overlap_html}
{built_html}
  <h2 id="modified">Modified questions <a class="top" href="#contents">Contents &uarr;</a></h2>
  <p>Each group is one question that exists in more than one version. Version A is the
  most-used version, or the first one found when there is a tie; every other version is shown as a diff against it:
  <ins>added</ins> and <del>removed</del> wording, in the stem and in each choice.
  <span class="key">&#10003;</span> marks the credited answer. Groups where the versions
  disagree on the answer key are listed first: there the question is worded the same
  and only the choices or the key changed.</p>
  {modified_html}

  <h2 id="similar-wording">Similar wording, different answer <a class="top" href="#contents">Contents &uarr;</a></h2>
  <p>Questions whose wording is close but where the wording and the credited answer both
  differ. Most are different questions that happen to read alike (a statement and its
  opposite, "most likely" and "least likely"). Occasionally one is a question that was
  reworded and had its key changed at the same time.</p>
  {lookalike_html}

  <h2 id="exact">Exact duplicates <a class="top" href="#contents">Contents &uarr;</a></h2>
  <p>Same stem, same choices, same question type, and same answer key, in more than one place.
  Where a card says the answer key could not be read, the key was not part of the comparison.</p>
  {exact_html}

  <h2 id="same-stem">Same stem, unrelated choices <a class="top" href="#contents">Contents &uarr;</a></h2>
  <p>Identical stem wording, but the choices are too different to be the same question
  (typically generic stems). Listed so nothing is silently dropped.</p>
  {unrelated_html}

  <h2 id="same-choices">Same choices and answer, different stem <a class="top" href="#contents">Contents &uarr;</a></h2>
  <p>Identical sets of three or more choices with the same credited answer, under stems too
  different to be linked above. These may be one question with a rewritten stem, or two
  questions that test the same idea in different ways.</p>
  {same_choice_html}

  <h2 id="repeated">Repeated answer choices <a class="top" href="#contents">Contents &uarr;</a></h2>
  <p>Questions that list the same choice more than once.</p>
  {repeated_html}

  {unresolved_html}
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Find duplicate and modified questions across Canvas quiz exports.")
    ap.add_argument("sources", nargs="+", help="One or more .zip/.imscc exports or extracted folders")
    ap.add_argument("-o", "--output", default="quiz_duplicate_report.html", help="Output HTML report path")
    ap.add_argument("--json", metavar="PATH",
                    help="Also write the findings to PATH as JSON, with the same sections as the report.")
    ap.add_argument("-t", "--threshold", type=float, default=85.0,
                    help="How similar the stem or the choices must be to call two questions "
                         "versions of each other, 0-100 (default 85). Lower finds heavier "
                         "rewrites but more false matches; below about 60 expect mostly noise.")
    ap.add_argument("--word-overlap", type=float, default=WORD_OVERLAP,
                    help="When two stems are worded differently, how much of their distinctive "
                         "wording they must share to be the same question, 0-100 (default "
                         f"{WORD_OVERLAP}). Raise it if questions about different things are being "
                         "grouped; lower it (around 40) to also catch heavy rewrites.")
    ap.add_argument("--container-overlap", type=float, default=CONTAINER_OVERLAP,
                    help="List a quiz or bank as mostly contained in another when at least this "
                         f"percent of its questions are also there (default {CONTAINER_OVERLAP}).")
    ap.add_argument("--exclude-type", action="append", default=[], metavar="TYPE",
                    help="Skip a question type. Repeatable. Canvas types: multiple_choice_question, "
                         "true_false_question, multiple_answers_question, matching_question, "
                         "short_answer_question, fill_in_multiple_blanks_question, "
                         "multiple_dropdowns_question, numerical_question, calculated_question, "
                         "essay_question, file_upload_question, text_only_question.")
    ap.add_argument("--only-published", action="store_true",
                    help="In the bank orphan check, count only published quizzes as using a bank or "
                         "holding a question. Banks used only by unpublished quizzes then show as unused; "
                         "deleting them breaks those quizzes. Needs a course export (published state is "
                         "read from assessment_meta.xml).")
    ap.add_argument("--exclude-title", action="append", default=[], metavar="TEXT",
                    help="Skip quizzes and banks whose title contains TEXT (case-insensitive). Repeatable.")
    tuning = ap.add_argument_group(
        "advanced matching settings",
        "The fixed rules behind --threshold. The defaults were tuned on real course exports; "
        "see 'How matching works' in the README before changing them.")
    for name, default, text in ADVANCED_SETTINGS:
        tuning.add_argument(f"--{name.lower().replace('_', '-')}", type=float, default=default, metavar="N",
                            help=f"{text} (default {default:g})")
    args = ap.parse_args()
    for name in ("threshold", "word_overlap", "container_overlap") + tuple(
            n.lower() for n, _, _ in ADVANCED_SETTINGS if n != "MIN_CONTAINER"):
        if not 0 <= getattr(args, name) <= 100:
            ap.error(f"--{name.replace('_', '-')} must be between 0 and 100")
    if args.min_container < 1:
        ap.error("--min-container must be at least 1")
    # the matching functions read these module settings when they run
    for name, _, _ in ADVANCED_SETTINGS:
        value = getattr(args, name.lower())
        globals()[name] = int(value) if name == "MIN_CONTAINER" else value

    print("Parsing exports...")
    images = ImageStore()
    questions, unresolved, bank_usage, containers = collect_questions(args.sources, images)
    # worked out before the filters: a quiz left out of the comparison still uses its banks
    known = [c for c in containers if c.kind == "quiz" and c.published is not None]
    unpublished = {(c.source_file, c.cid) for c in known if not c.published}
    if known:
        print(f"  Published state read for {len(known)} of "
              f"{plural(sum(c.kind == 'quiz' for c in containers), 'quiz', 'quizzes')}: "
              f"{len(unpublished)} unpublished")
    skip = frozenset(unpublished) if args.only_published else frozenset()
    usage, refs_seen, refs_resolved = find_bank_usage(questions, containers, skip)
    bank_items = Counter((q.source_file, q.quiz_id) for q in questions if q.source_kind == "bank")
    image_note = ""
    if images.by_content or images.by_name or images.by_address:
        image_note = (f" Images in questions: {images.by_content} compared by content, "
                      f"{images.by_name} by file name (file not in the export), "
                      f"{images.by_address} by web address.")
        print(f" {image_note}")
    excluded_note = ""
    excluded_banks = {}
    if args.exclude_type or args.exclude_title:
        types = {t.lower() for t in args.exclude_type}
        titles = [t.lower() for t in args.exclude_title]
        kept = [q for q in questions
                if q.question_type.lower() not in types
                and not any(t in q.quiz_title.lower() for t in titles)]
        excluded_note = f" {len(questions) - len(kept)} questions excluded by filters."
        print(f" {excluded_note.strip()}")
        questions = kept
        if titles:
            hidden = lambda title: any(t in title.lower() for t in titles)
            excluded_banks = {(c.source_file, c.cid): c.title for c in containers
                              if c.kind == "bank" and hidden(c.title)}
            containers = [c for c in containers if not hidden(c.title)]
            bank_usage = {k: {t for t in v if not hidden(t)} for k, v in bank_usage.items()}
            unresolved = [u for u in unresolved if not hidden(u[1])]
    kept_items = Counter((q.source_file, q.quiz_id) for q in questions if q.source_kind == "bank")
    excluded_items = {key: n - kept_items.get(key, 0) for key, n in bank_items.items() if n > kept_items.get(key, 0)}
    quizzes = [c for c in containers if c.kind == "quiz"]
    banks = [c for c in containers if c.kind == "bank"]
    print(f"  {len(questions)} questions found across {plural(len(quizzes), 'quiz', 'quizzes')} "
          f"and {plural(len(banks), 'bank')}")
    new_quizzes = sum(c.engine == "new" for c in quizzes)
    item_banks = sum(c.engine == "new" for c in banks)
    engine_note = ""
    if new_quizzes or item_banks:
        engine_note = (f" (from New Quizzes: {plural(new_quizzes, 'quiz', 'quizzes')} and "
                       f"{plural(item_banks, 'item bank')})")
        print(f" {engine_note}")
    draw_only = sum(1 for c in quizzes if (c.draws or c.picks) and not c.item_count)
    hollow = sum(1 for c in quizzes if not (c.item_count or c.draws or c.picks))
    draw_note = ""
    if draw_only:
        draw_note = f" Quizzes with no questions of their own, taking everything from banks: {draw_only}."
        print(f" {draw_note}")
    if hollow:
        draw_note += f" Quizzes with nothing in the export: {hollow}."
        print(f"  Quizzes with nothing in the export: {hollow}")
    picked_by = {}
    for c in quizzes:
        for bank_id, item_id in c.picks:
            picked_by.setdefault((c.source_file, bank_id, item_id), set()).add(c.title)
    if unresolved:
        print(f"  {len(unresolved)} question-bank references with no included content")

    no_choices = sum(1 for q in questions if not q.choices and q.question_type in CHOICE_TYPES)
    if no_choices:
        print(f"  {no_choices} choice-type questions have no choices in the export (noted on their cards)")

    variants = build_variants(questions)
    exact_variants = [v for v in variants if len(v.occurrences) > 1]
    print(f"  {len(variants)} distinct questions; {len(exact_variants)} of them appear more than once")

    print(f"Finding modified questions (threshold {args.threshold}, word overlap {args.word_overlap})...")
    clusters, roots, lookalikes = find_version_clusters(variants, args.threshold, args.word_overlap)
    unrelated_groups = find_same_stem_unrelated(variants, roots)
    print(f"  {len(clusters)} clusters of modified questions")
    print(f"  {len(lookalikes)} groups of similar wording with a different answer")
    same_choice_groups = find_same_choices_other_stem(variants, roots)
    repeated = find_repeated_choices(variants)
    print(f"  {len(unrelated_groups)} stems shared by unrelated questions")
    print(f"  {len(same_choice_groups)} choice sets with the same answer under different stems")
    print(f"  {len(repeated)} questions with a repeated choice")
    identical_containers, container_overlaps = find_duplicate_containers(
        variants, roots, args.container_overlap, containers)
    print(f"  {len(identical_containers)} sets of identical quizzes or banks")
    print(f"  {len(container_overlaps)} quizzes or banks mostly contained in another")
    bank_audit = check_orphan_banks(variants, roots, containers, usage, excluded_items, skip)
    statuses = Counter(b.status for b in bank_audit)
    verdicts = Counter(b.verdict for b in bank_audit if b.verdict)
    print(f"  Banks: {statuses[USED_LINKED]} linked, {statuses[USED_COPIED]} copied only, "
          f"{statuses[UNUSED]} unused")
    if refs_seen:
        print(f"  {refs_resolved} of {refs_seen} quiz questions that point back to a bank question "
              "found their bank question in the export")
    if verdicts:
        print("  Banks not linked: " + ", ".join(f"{VERDICT_LABELS[v].lower()} {verdicts[v]}"
                                                 for v in VERDICT_ORDER if verdicts[v]))

    # one ordering for both the HTML report and the JSON file
    clusters, lookalikes = order_clusters(clusters), order_clusters(lookalikes)
    # modified-question group number of each version, for links from the bank check
    cluster_of = {id(v): n for n, group in enumerate(clusters, 1) for v in group}
    classic_quiz_items = sum(1 for q in questions if q.source_kind == "quiz" and q.engine == "classic")

    ctx = {"show_source": len({q.source_file for q in questions}) > 1, "bank_usage": bank_usage,
           "excluded_note": excluded_note, "min_overlap": args.word_overlap,
           "container_overlap": args.container_overlap, "containers": containers,
           "engine_note": engine_note, "draw_note": draw_note, "picked_by": picked_by,
           "images": images, "image_note": image_note, "excluded_banks": excluded_banks,
           "variants": variants, "bank_audit": bank_audit, "refs_seen": refs_seen,
           "refs_resolved": refs_resolved, "cluster_of": cluster_of, "only_published": args.only_published,
           "classic_quiz_items": classic_quiz_items}
    report = build_html_report(clusters, lookalikes, exact_variants, unrelated_groups, same_choice_groups, repeated,
                               identical_containers, container_overlaps, unresolved, args.threshold,
                               len(questions), len(quizzes), len(banks), ctx)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Report written to {args.output}")
    if args.json:
        # `export` is the name used for that source everywhere else in the file
        settings = {"sources": [{"path": p, "export": label}
                                for p, label in zip(args.sources, export_labels(args.sources))],
                    "threshold": args.threshold,
                    "word_overlap": args.word_overlap, "container_overlap": args.container_overlap,
                    "exclude_type": list(args.exclude_type), "exclude_title": list(args.exclude_title),
                    "only_published": args.only_published,
                    **{n.lower(): getattr(args, n.lower()) for n, _, _ in ADVANCED_SETTINGS}}
        data = build_json_report(clusters, lookalikes, exact_variants, unrelated_groups, same_choice_groups,
                                 repeated, identical_containers, container_overlaps, unresolved, settings, ctx)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"JSON written to {args.json}")


if __name__ == "__main__":
    main()
