# Canvas Quiz Duplicate and Modified Question Finder

`quiz_dedup.py` reads Canvas course or quiz exports and reports which questions are duplicated, and which exist in several slightly different versions, across every quiz and bank in the export. It reads Classic Quizzes and New Quizzes, and compares them with each other.

It is meant for courses that have been copied forward for years and have collected many overlapping question banks. The output is a single HTML report you open in a browser.

## What it finds

- **Orphan question banks.** Which banks a quiz uses, and for each bank no quiz uses, whether every one of its questions also lives somewhere that is in use, as is or as a modified version. For questions no quiz uses, it picks one bank to keep so the other copies can go. The report opens with a written summary naming the banks in each group.
- **Duplicate quizzes and banks.** Whole quizzes or question banks that hold exactly the same questions as another, and ones that are mostly or fully contained in another. Quizzes that pull their questions from banks are listed with what they pull.
- **Modified questions.** Versions of the same question where the stem wording, the answer choices, or the answer key was changed. Versions are grouped into clusters, and each version is shown as a word-level diff against the most-used one.
- **Similar wording, different answer.** Questions that read alike but where the wording and the credited answer both differ, such as a statement and its opposite. These are usually different questions, so they are listed apart from the modified questions.
- **Exact duplicates.** Questions with the same stem, the same choices, the same question type, and the same answer key that appear in more than one place.
- **Same stem, unrelated choices.** Questions that share a generic stem such as "Which of the following is true?" but are otherwise different questions.
- **Same choices and answer, different stem.** Identical sets of three or more choices with the same credited answer, under stems too different to be linked. These may be one question with a rewritten stem, or two questions that test the same idea in different ways.
- **Repeated answer choices.** Questions that list the same choice twice.
- **Unresolved bank references.** Quizzes that draw from a question bank whose questions were not included in the export.

## Requirements

- Python 3.7 or later
- [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) and [numpy](https://numpy.org/)

```
pip install rapidfuzz numpy
```

The action plan (`--plan`) also needs [openpyxl](https://openpyxl.readthedocs.io/): `pip install openpyxl`.

## Getting an export from Canvas

1. In the course, go to **Settings**, then **Export Course Content**.
2. Choose **Course** to include every quiz and question bank, or **Quiz** to export selected quizzes.
3. Download the resulting `.imscc` or `.zip` file.

A full course export is the better choice, because it includes the question banks. In the course export this was tested with, New Quizzes and their item banks were in the same file.

## Usage

```
python3 quiz_dedup.py export.imscc -o report.html
```

You can pass several exports at once to compare across courses:

```
python3 quiz_dedup.py fall.imscc spring.imscc -o report.html
```

| Option | Default | Meaning |
| --- | --- | --- |
| `sources` | (required) | One or more `.imscc` or `.zip` exports, or folders of already-extracted exports |
| `-o`, `--output` | `quiz_duplicate_report.html` | Where to write the report |
| `--json PATH` | (none) | Also write the findings to `PATH` as JSON |
| `--plan PATH` | (none) | Also write an action plan to `PATH`, an `.xlsx` file (see *Action plan*) |
| `--compare A B` | (none) | Instead of the report, compare two quizzes or banks side by side (see *Comparing two quizzes or banks*) |
| `--compare-output PATH` | `comparison.html` | Where `--compare` writes its page |
| `-t`, `--threshold` | `85` | How similar (0 to 100) the stem or the choices must be for two questions to count as versions of each other |
| `--word-overlap` | `60` | When two stems are worded differently, how much of their distinctive wording (0 to 100) they must share to count as the same question |
| `--container-overlap` | `80` | List a quiz or bank as mostly contained in another when at least this percent of its questions are also there |
| `--exclude-type TYPE` | (none) | Skip a question type, such as `essay_question`. Can be repeated. |
| `--exclude-title TEXT` | (none) | Skip quizzes and banks whose title contains `TEXT` (case-insensitive). Can be repeated. |
| `--only-published` | off | In the bank orphan check, count only published quizzes as using a bank or holding a question |

The fixed rules behind the threshold (see *How matching works*) can also be changed, with `--loose-stem`, `--choices-floor`, `--both-margin`, `--loose-choices`, `--stem-only-min` and `--min-container`. `--help` lists their defaults. They were tuned on real exports, so change them one at a time and compare reports.

Lower the threshold to catch heavier rewrites; raise it if unrelated questions are being grouped together. Below about 60, expect mostly false matches.

If questions about different things are being grouped, raise `--word-overlap` (try 70). If reworded questions you know about are missing, lower it (try 40). At 0 the check is off. At 100, differently worded stems only match when one is the other plus added words.

Questions removed with `--exclude-type` or `--exclude-title` are dropped before anything is counted, so the totals in the report cover only what was compared. A quiz whose questions were all excluded is not listed as empty, and a bank question a quiz uses is not reported as missing because it was excluded. A bank removed with `--exclude-title` is shown by name as excluded where a quiz draws from it, and what a quiz takes from excluded banks or excluded questions is left out when quizzes are compared with each other. The report header says how many were excluded. `--help` lists the Canvas question type names.

## Reading the report

The report opens with a summary and a table of contents. Each entry links to its section and shows how many groups the section holds, and every section heading has a link back to the contents.

In the **Modified questions** section, each group is one question:

- **Version A** is the version used in the most places.
- Every other version is shown as a diff against Version A, with added wording highlighted in green and removed wording in red, in both the stem and the choices.
- A check mark shows the credited answer.
- Badges say what changed: *Stem reworded*, *Choices changed*, *Question type differs*, or *Answer key differs*.
- Each version lists the quizzes and banks it appears in. Hover over a location to see the export's item ids for those copies. For a bank, the list also names the quizzes that draw on it through a question group.
- Locations are labelled `[quiz]` and `[bank]` for Classic Quizzes, and `[new quiz]` and `[item bank]` for New Quizzes.

Groups where the versions disagree on the answer key are listed first, since one of the versions may be wrong.

### Filtering by quiz or bank title

The box at the top of the report filters it by title as you type. Type `language` and every section shows only the items that involve a quiz or bank with "language" in its title. Matching ignores capital letters. The box stays at the top of the page as you scroll.

The filter only changes what is shown. Every question was still compared with every other, so a Language bank's questions still turn up next to their copies in banks with other names, such as "Final 2019". That is usually what you want when consolidating banks.

What counts as an item:

| Section | An item | The titles it matches on |
|---|---|---|
| Bank table | one bank | the bank's title |
| Quiz coverage | one quiz | the quiz's title |
| Bank verdict lists | one bank | the bank's title |
| Quizzes that use questions from banks | one quiz | the quiz and the banks it draws from |
| Identical, mostly contained | one group | every quiz and bank in the group |
| Question lists (modified, exact, and the rest) | one group or question | every quiz and bank the question is in |

By default an item is shown when **any** of its titles matches. Tick **every quiz and bank in an item must match** to show only items that stay entirely within matching quizzes and banks. For example, duplicates among your Language banks that appear nowhere else.

While a filter is on, the counts in the contents read "2 of 4", and a section with no match says so. The verdict summary at the top of the bank section is hidden, because it describes every bank. The filter is saved in the page address (`report.html?q=language`), so a filtered view can be bookmarked or sent as a link alongside the file.

## JSON output

```
python3 quiz_dedup.py export.imscc -o report.html --json report.json
```

The JSON file holds the same findings as the HTML report, in the same order, for use in a spreadsheet or another script. Its top-level keys are:

| Key | Contents |
| --- | --- |
| `generated`, `settings`, `summary`, `question_images` | Run date, the options used, overall counts, and how question images were compared |
| `bank_usage_audit`, `classic_bank_links` | Each bank's usage status, orphan verdict, how many of its questions are also stored elsewhere (`also_stored_elsewhere`), the kept banks it relies on or is kept for, and how many quiz questions point back to a bank question |
| `quiz_coverage` | For each quiz with questions of its own: the bank with the same questions (if any), the fewest banks covering it (`covered_by`), counts of questions in a linked bank, only in banks no quiz links to, only reworded in a bank, or in no bank, and the questions in the last three groups |
| `quizzes_and_banks` | Every quiz and bank, with what each quiz draws or uses from banks |
| `identical_quizzes_and_banks`, `mostly_contained`, `quizzes_with_nothing_in_export` | The whole-quiz and whole-bank findings |
| `modified_questions`, `similar_wording_different_answer` | Clusters of versions |
| `exact_duplicates`, `same_stem_unrelated_choices`, `same_choices_and_answer_different_stem`, `repeated_answer_choices` | The other question lists |
| `unresolved_bank_references` | Bank references whose bank is not in the export |

Each question has its type, stem, choices, answer key, and a list of locations with the quiz or bank title and the export's item id. In a cluster, version A is the most-used version (the first one found on a tie), and every other version has a `changes_from_a` entry saying whether the stem, choices, question type, or answer key differ and how similar they are. A draw with `"count": null` means the quiz uses every question in that bank.

The word-level diffs are only in the HTML report.

## Bank usage and orphan check

The first section of the report answers one question for every bank: can it be deleted without losing anything?

**Usage.** Each bank gets one of three statuses, the same ones the [Unused Classic Quiz Bank Checker](https://github.com/jccalhoun/Canvas-Unused-Classic-Quiz-Bank-Checker) reports from the Canvas API:

- **linked**: a quiz has a question group that draws from the bank, or a New Quiz uses single questions from it. Deleting the bank breaks those quizzes, so linked banks get no verdict.
- **copied only**: no quiz links to the bank, but Classic quiz questions were copied from it. The export marks each such quiz question with the bank question it came from (`assessment_question_identifierref`). The quizzes hold their own copies, so they do not need the bank.
- **unused**: neither.

Usage counts every quiz in the export, including quizzes left out of the comparison with `--exclude-title`: excluding a quiz from the comparison does not stop it using its banks. Unpublished quizzes count too, unless you pass `--only-published`.

**Verdict.** For a bank that is not linked, each of its questions is first sorted by where else it is found in the same export:

| Found | Meaning |
|---|---|
| in use as is | the same question is in a quiz or in a linked bank |
| in use as a modified version | a version from the same *Modified questions* group is in a quiz or a linked bank |
| also in other unused banks | copies or versions exist, but only in banks no quiz uses |
| nowhere else | the question is unique to this bank |

Only quizzes and linked banks count as in use. Otherwise two unused copies of one bank would each make the other look safe, and deleting both would lose the questions.

**Keeping one copy.** Questions in the last two rows need one bank to stay. Every bank holding a question found nowhere else is kept. After that, the bank holding the most of the remaining questions is kept, then the next, until each of those questions is in a kept bank. Ties go to the larger bank, then the first by title. The other banks' questions are then counted as *in a bank to keep*, as is or as a modified version.

| Verdict | When |
|---|---|
| **Has unique questions** | it holds questions found nowhere else. Whether to keep it depends on whether you want the material, not on duplication |
| **Keep as the one copy** | its leftover questions exist only in other unused banks, and this bank was picked to hold them |
| **Review the diffs first** | some questions survive only as modified versions, in use or in a bank to keep; or some were removed by `--exclude-type` and not checked |
| **Safe to delete if the kept copies stay** | every question is in use or in a bank to keep, unchanged |
| **Safe to delete** | every question is in use, unchanged, somewhere else |
| **Empty in the export** | the bank has no questions in the export |

**Bank table.** One row per bank, with its question count in the export (and, when it differs, the distinct questions compared, since a bank can repeat a question and `--exclude-type` drops some), status, the quizzes that use it, and its verdict. The **Also stored elsewhere** column counts the bank's distinct questions that are also stored in another quiz or bank, as is or only as a modified version. For example, `3 of 5 (2 as is, 1 modified)`. This covers linked banks too, so it shows how much a bank shares with others even when a quiz still uses it. A quiz drawing from the bank does not count as "elsewhere", and questions removed with `--exclude-type` or `--exclude-title` are not counted.

The section opens with a summary of each verdict and the banks in it. Banks with unique questions are listed by how many they hold, with numbered families such as `chapter_1-...`, `chapter_2-...` folded into one line. Each *keep as the one copy* example names the banks that rely on it. When several banks share a title, each gets a number (`#12`) used everywhere in the section, and the table also shows its first question.

Each review item links to its group in *Modified questions*, where the word-level diff shows what changed.

The statuses can be checked against the Unused Classic Quiz Bank Checker: its *Used?* column should read `LINKED`, `COPIED ONLY` or `no` for the same banks. Match banks by title and question count, since the export does not carry Canvas's bank ids.

## Quiz coverage

The second section answers one question for every quiz that has questions of its own: which banks already hold them? Each quiz gets a sentence or two:

- **Has the same questions as the bank X.** The quiz's questions could be replaced with a question group that picks all of them from that bank.
- **Every question is in the bank X**, which also has some other questions.
- **Every question is in a bank, spread over N banks**, with how many each bank adds. The fewest banks that cover the quiz are picked, preferring a bank a quiz already links to, then the smallest.
- How many questions are in a bank **only as a reworded version**, and how many are in **no bank**.

A **Keep X** note appears when the bank check calls a bank safe to delete only because this quiz holds copies of its questions, and no other quiz does. The quiz and the bank vouch for each other, so if you change the quiz, keep the bank.

Each card can be expanded to show the reworded questions (each linked to its group in *Modified questions*, where the wording diff is), the questions in no bank, and which banks hold each question.

The heading of each card gives the question count. Distinct questions are shown too when the quiz repeats a question. Quizzes that share a title are numbered (`#29`) and show their first question, as same-named banks do. A question Canvas could not export is written with the type `Error` and only its text. The report says so wherever it shows one. Quizzes that draw all their questions from banks are not listed. Questions removed with `--exclude-type` are not counted. Cards are sorted by title and work with the title filter.

## Comparing two quizzes or banks

Under each match in *Mostly contained in another*, **Compare side by side** opens a comparison of the two:

- a count of the questions that are the same, reworded, only in A, and only in B
- each reworded question in two columns, A's version on the left and B's on the right, with B's added and removed wording marked against A's and badges saying what changed
- the questions only one side has, in their own column
- the questions that are the same, as a list you can expand

Use it to decide which of two banks to keep and edit, and which to delete. In the report, a list longer than 25 questions is counted rather than shown, to keep the report small.

To compare any two quizzes or banks, with every list shown in full:

```
python3 quiz_dedup.py export.imscc --compare "Chapter 3 Listening pool" "OLD Chapter 3 listening pool"
```

Name each one by its title or a unique part of it. When a name matches more than one quiz or bank, the script lists them with their export ids, and you can pass an id instead. The comparison is written to `comparison.html` (change it with `--compare-output`), and no report is written in this mode. Only questions stored in each quiz or bank are compared, not ones a quiz draws from a bank.

## Action plan

```
python3 quiz_dedup.py export.imscc -o report.html --plan plan.xlsx
```

The report answers "what is duplicated where". The action plan answers "what do I do next" when the goal is to get quiz questions into banks. It is a spreadsheet you can open in Excel or Google Sheets, with one row per decision and a Done column to tick:

- **Quizzes**: one row per quiz that has questions of its own, in the order to work through them.
  - **Step 1, swap for a question group**: the quiz has the same questions as a bank. Delete its questions and add a question group that picks all of them from that bank.
  - **Step 2, inside a bigger bank**: every question is in one bank that has more. Draw at random from it, or copy the questions into a bank of their own.
  - **Step 3, spread over banks**: copy the questions into one new bank and use one question group.
  - **Step 4, put questions in a bank first**: some questions are in no bank, or only as a reworded version. A quiz whose missing questions are all essay, numerical and similar types is flagged as possibly an assignment you would rather leave alone.

  The **Keep** column names banks the quiz depends on: the bank check calls them safe to delete only because this quiz has the only other copy of some of their questions.
- **Banks**: the banks the bank check calls safe to delete, with why, and whether to go ahead. **yes** means delete it. **wait** means a quiz you have not changed yet holds the only other copy of some of its questions. **no** means the plan has a quiz draw from it, so it will be in use.
- **Questions**: every question from a step 4 quiz that is in no bank, or only reworded in one, with its type, choices and answer, ready to add to a bank.

Quizzes and banks that share a title carry the same `#` number as in the report. Nothing is changed in Canvas: the plan is a checklist for doing the work by hand. After a round of changes, export the course again and rerun. Finished work drops off and the plan gets shorter.

## Duplicate quizzes and banks

The first section of the report compares whole quizzes and question banks by the questions they hold.

- **Exactly the same contents** lists quizzes and banks whose sets of questions are identical. Order does not matter, and a question repeated inside one bank counts once. Two quizzes that take questions from banks count as the same when they also draw the same number from banks holding the same questions and use the same single bank questions.
- **Mostly contained in another** lists each quiz or bank that has at least 80% (`--container-overlap`) of its questions in another one of the same size or larger. It shows how many are identical and how many are there as a modified version, and marks the ones that are fully contained. Only questions stored in the quiz or bank count here.
- **Quizzes that use questions from banks** lists each quiz that takes questions from a bank: how many questions of its own it has, how many it draws at random from which bank, and how many single questions it uses from which bank. A New Quiz that uses every question in a bank has no number in the export and is shown as "all questions".
- **Quizzes with nothing in the export** lists quizzes that came through with no questions and no link to a bank. Either the quiz is empty in Canvas or its content was not exported.

Bank entries name the quizzes that draw on them through a question group, which helps when deciding which copy to keep.

Questions a quiz pulls from a bank are stored in the bank, so they are compared as part of the bank. Quizzes and banks with fewer than three distinct questions are not compared, unless they pull from a bank.

A quiz fully contained in a bank is often intentional, since copying questions from a bank into a quiz leaves a copy in both.

## New Quizzes

A course export writes New Quizzes in the same format as Classic Quizzes, so their questions go through the same comparison. A question that exists in a Classic bank and in a New Quizzes item bank is reported as one question in two places.

The script recognizes a New Quiz and an item bank by markers Canvas puts on them in the export, and labels them `[new quiz]` and `[item bank]`.

A New Quiz built from item banks has no questions of its own in the export, only the list of banks it pulls from. It appears in the *Quizzes that use questions from banks* table and in the "drawn on by" notes on its banks.

A New Quiz can also use single questions chosen from an item bank, including a bank from outside the course. Those questions stay in the bank and are not counted a second time; the bank's copy is marked "used as a question in" with the quiz's name. In the export this was tested with, Canvas included the outside item bank in the file.

## How matching works

Question text is compared after lowercasing and removing HTML, punctuation, and extra whitespace. The order of answer choices is ignored.

Two questions are treated as versions of the same question when one of these is true:

- the stems are at least 85% similar and the choices are at least 60% similar
- the choices are at least 85% similar and the stems are at least 60% similar
- the stems and the choices are both at least 75% similar

The 85 is the `--threshold` value, and the 75 is always 10 points below it, so raising the threshold tightens that rule too. The 60s are `--loose-stem` and `--choices-floor`, the 10-point gap is `--both-margin`, and the 90 below is `--stem-only-min`.

Questions where the choices cannot help (true/false, essay, and other types without choices) are compared on the stem alone and must be at least 90% similar.

Requiring the stem and the choices to both line up is what keeps generic stems from matching each other.

Two further checks apply when the stems are not worded identically:

- **Word overlap.** Many questions are built on a shared sentence frame, such as "To persuade my audience that ... is a specific purpose statement for a persuasive speech on a question of". Their characters are mostly the same even when they are about different things. So the stems must also share at least 60% (`--word-overlap`) of their distinctive wording, where each word counts more the rarer it is across the whole export. A stem that is another stem plus added words, such as an appended hint, always passes.
- **Answer key.** If the wording changed and the credited answer changed with it, the two are treated as different questions and listed under *Similar wording, different answer*. Each group there is one question plus the questions that directly read like it; groups are not chained together.

Images are compared by content when the image file is in the export: the file is hashed, so the same picture under two file names counts as the same, and two different pictures that share a file name do not. The report shows an image as `[image: name]`, using the first file name seen for that picture, and adds `(2)` when a different picture reuses a name. Equation images are compared by their LaTeX. An image at a web address is compared by its site and path, so `graph.png` on two different sites is two pictures; the query string is ignored, since Canvas adds per-link tokens to the same file, and a Canvas file's `/preview` and `/download` links count as one. An image file the export leaves out is compared by its name. The report's summary says how many images fell into each group. A question whose stem is only an image is kept. Two questions whose stems each show a picture, and not the same pictures, are treated as different questions however alike their wording is, so "What is wrong with this slide?" under two different slides is not grouped. The same goes for answer choices: two choices that show different pictures are never treated as the same choice reworded. A picture added to or removed from a question, including one of several, still counts as an edit.

Choices are paired one-to-one between two versions. The answer key counts as unchanged only when the credited choices are each other's counterparts, so rewording the correct choice is reported as a choice change, while crediting a different or newly added choice is reported as *Answer key differs*. Matching questions work the same way on their rows: rewording a prompt or an answer is a choice change, and a prompt that maps to a different answer is a key change. Rows are paired by how alike their prompts read; when exactly one row on each side is left unpaired, those two are paired too, so a row whose prompt was rewritten past recognition still has its answer compared. In the report, a changed row shows its old answer struck out and its new answer highlighted, next to the prompt.

## Limitations

- **New Quizzes question types.** New Quizzes support was built from one course export, which held multiple choice, true/false, multiple answers, and essay questions. Types that only New Quizzes has (categorization, ordering, hot spot, formula, stimulus) were not in it, so they may be missing from an export or misread.
- **Item banks can hold more than Canvas shows.** In one tested export, an item bank that showed 21 questions in Canvas came through with 27. The export does not mark the extra six, so they are compared like any other question. If a bank's count in the report is higher than in Canvas, that is the likely reason.
- **Course exports only for New Quizzes.** A quiz exported on its own from inside New Quizzes has not been tested.
- **Banks missing from the export.** Canvas does not always include every question bank in an export. Quizzes that point to a missing bank are listed in the report, but those questions cannot be checked.
- **Answer keys.** The answer key is read for multiple choice, multiple answers, true/false, short answer, and matching questions. For other types, or when the key cannot be read, the key is not compared, and the question's card says so.
- **Stem similarity is a whole number.** Stems are scored 0 to 100 in whole points, so a fractional `--threshold` such as 85.5 acts like 86 for stems.
- **Formatting and punctuation are ignored.** Two questions that differ only in formatting or in punctuation are treated as identical. Numbers keep what distinguishes them, so `1.5`, `15`, `-5`, `.5` and `1-5` all differ, and so do `x-5` and `x5`. Comparison and arithmetic signs are kept as words, so `x < 3` and `x > 3`, `1/2` and `12`, and `50%` and `50` differ too (`<`, `>`, `<=`, `>=`, `=`, `!=` and their symbols, `+`, `%`, `×`, `÷`, and `*` or `/` between two terms). A slash between words, as in *and/or*, is still dropped.
- **Numerical ranges are not read.** For a numerical question whose answer is a range, the key is not compared.
- **Different pictures are never grouped.** If a question was reworded and its picture was replaced at the same time, the two versions are reported as separate questions. When they share the same choices and answer, they appear under *Same choices and answer, different stem*.
- **Images missing from the export are compared by name.** For those, the same picture under a different file name looks like an edit, and a replaced picture that kept its file name does not. Keep the image files in the export to avoid this. Pictures that look alike but are not the same file, such as a resized copy, count as different.
- **Item ids are export ids.** They identify a question inside the export file and are not the ids Canvas shows in its own pages.
- **Usage in other courses is not visible.** A Classic quiz group can draw from a bank in another course, so a bank shown here as unused may still be used from elsewhere.
- **Unpublished quizzes count as in use unless you say otherwise.** A course export records whether each Classic quiz is published, and the report marks unpublished quizzes in the *Used by* column. By default they still count, since deleting a bank they draw from breaks them. With `--only-published` they are treated as absent from the orphan check: they neither use a bank nor hold a question. New Quizzes, and quizzes in a quiz-only export, have no published state in the export and always count.
- **Same-named identical banks cannot be told apart in the export.** If one copy is linked and another is not, find the linked one in Canvas from the quiz's question group before deleting.
- **Bank usage is per bank, not per question.** A quiz group draws a random set from a bank, so "drawn on by" means the quiz can pull any question in that bank.
- **No choices found.** If a multiple choice, true/false, or matching question has no readable choices in the export, its card says so and only its wording is compared. This usually means the question itself is empty.
- **Clusters can chain.** If A resembles B and B resembles C, all three are grouped even when A and C differ a lot. The diffs make this visible.
- **Small edits can change meaning.** Two questions that differ by one phrase and have the same credited answer ("external obstacles" and "internal obstacles" in two essay prompts, or the same question about two different examples) look exactly like a question with one phrase reworded. Some of these will appear as modified questions, and no setting removes them without also removing real rewordings.
- **Word overlap needs a large export.** Word rarity is estimated from the export itself. With fewer than a few hundred questions it fades into plain word overlap.
- **No version history.** The export does not say which version of a question is newer. Bank and quiz names are the only clue.

The report is a set of candidates for you to review; the script does not change anything in Canvas.

## Tests

```
python3 -m unittest -v
```

The tests build small synthetic exports in the format Canvas uses for Classic Quizzes and New Quizzes. They cover the matching and answer-key logic, not every quirk of a real export.
