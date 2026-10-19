---
name: pyq-extract
description: Use on the machine that holds library/archive/ to read past-paper PDFs yourself and import every question (with a full-marks answer) into pyq_questions for the AI tutor.
---

# pyq-extract: past papers into the tutor, read with your own eyes

The tutor cites past-paper questions (PYQs) to say what the exam cares
about. They live in `pyq_questions`, cut out of the PDFs that
`archive_documents` shelves under `library/archive/<code>/<folder>/<file>`.
There are two routes into that table and they end in the same rows:

- `python notes.py tutor pyq extract` sends each PDF to the API.
- **This skill**: you read each PDF with the Read tool, write
  `tutor/pyq/<code>.json`, and run `python notes.py tutor pyq import`.

Use this route when there is no API key on the box, when a scan is too poor
for a one-shot extraction, or when a person wants the questions checked by
reading rather than trusted.

The import is idempotent: every question is fingerprinted on its subject,
its sitting (the paper it came from, or exam + year + section) and its text
(case and whitespace ignored), so a question imported twice lands once, and
an answer added later fills in a question that had none. The same question
set again in another year is another row on purpose: "asked in three of the
last four end terms" is what the tutor needs to say. A partial run is fine.
Stop whenever you like and run the import again later.

## 1. See what is there

```bash
python notes.py tutor pyq status          # per subject: papers, extracted, questions, answered, mapped
ls library/archive/CY1107/                # the folders the portal used
psql "${RECARVE_DB_URL:-postgresql:///recarve_test}" -Atc "
  select d.file_key, d.exam, d.year, d.set_for_section
    from archive_documents d
   where d.subject_code = 'CY1107' and d.kind = 'paper'
     and not exists (select 1 from pyq_questions q where q.document_id = d.id)
   order by d.year desc nulls last, d.file_key"
```

The `file_key` printed there (`library/archive/CY1107/End Term/End Term 2024-25.pdf`)
is what goes in each item's `path`. That is how the question gets linked to
its PDF. A path that names no archive row is refused, not imported unlinked,
so copy it exactly.

Work on papers that have no questions yet. The same End Term is often shelved
under two folders. If two files are the same paper, do it once.

## 2. Read each paper and transcribe every question

For each PDF, `Read` it (pass `pages` for anything over 10 pages) and look at
every page. Then write the questions into `tutor/pyq/<code>.json` as a list of
ImportItems, or `{"items": [...]}`:

| field | what |
|---|---|
| `code` | the subject code, e.g. `CY1107` |
| `path` | the paper's `file_key`, exactly as `archive_documents` has it |
| `exam` | `mini`, `mid` or `end`, from the folder/filename (or leave it out; the paper's row is inherited) |
| `year` | the opening year as an int: "2024-25" is `2024` (or leave it out to inherit) |
| `section` | one letter A-J when the paper was set for one section ("Sec B") |
| `qno` | as printed: `"1(a)"`, `"2(b)(ii)"`, `"3(a) OR"` |
| `marks` | the number printed for that part; `null` if none is printed |
| `unit`, `topic` | copied EXACTLY from `syllabus.json` for that code (`python -c "import json; print(json.dumps(json.load(open('syllabus.json'))['CY1107'], indent=1))"`); `null` when nothing fits |
| `question_md` | the question, faithfully, in Markdown, maths in LaTeX `$...$` / `$$...$$` |
| `answer_md` | your full-marks model answer (below) |

Transcription rules:

- **Every** sub-question is its own item. 1(a), 1(b) and 1(c) are three items.
- **Keep OR-choices.** Both alternatives are items (`"2(a)"` and `"2(a) OR"`).
  The OR side is still a question the exam asked.
- When a part leans on a shared stem ("A coal sample has the following
  analysis ..."), repeat the data inside each part so it stands alone.
- Chemical formulas and every equation in LaTeX: `$H_2SO_4$`, `$\frac{dy}{dx}$`.
  Tables as Markdown tables. A figure: one bracketed line,
  `[Figure: truss ABC, 3 m spans, 10 kN at C]`.
- Text you cannot read is `[illegible]`. Do not guess the word.
- Leave out the header, the instructions and the roll-number boxes.

The don'ts:

- **Never invent a question that is not on the page.** Not to fill a unit,
  not because "it is usually asked". The tutor tells a student "this was asked
  in 2023". That has to be true.
- Do not tidy a question into a better one. Keep its wording and its numbers,
  typos included where they change nothing. Fix nothing silently.
- Do not merge two parts into one item or drop the one you think is easy.
- Do not make up marks. `null` is an honest value.
- Do not put a unit/topic name that is not in `syllabus.json`. The importer
  maps near-misses onto the real names, but `null` beats a wrong guess.

## 3. Write the answers

`answer_md` is what an engineering first-year examiner would accept for full
marks, sized to the marks:

- 1-2 marks: 3-4 lines. The definition, the fact or the formula, exactly.
- 3-5 marks: the key points in order, the formula with symbols defined, a
  short example, or a labelled diagram described in words.
- 7-10 marks: headings or numbered steps, the full derivation with every step,
  the result in bold, units throughout.
- Numericals: given, to find, formula, substitution, result with units.
- Use `tutor/packs/<code>.json` for that topic when it exists, for notation
  and for the traps the marker penalises.

Leave `answer_md` out if you are unsure. `notes.py tutor pyq answer` fills
the gaps later, and an answer you import afterwards fills a question that was
imported without one.

## 4. Fan out

One subagent per paper. Give each one the paper's `file_key`, the subject's
syllabus (unit/topic names), the rules above, and the file to write:
`tutor/pyq/<code>/<paper-stem>.json`. Then merge them into
`tutor/pyq/<code>.json`, or import each file on its own. Order does not
matter, and neither do duplicates.

## 5. Import and check

```bash
python notes.py tutor pyq import tutor/pyq/CY1107.json --by "claude-code"
python notes.py tutor pyq status
```

The import prints inserted / already there / placed on a topic / refused,
and one line per refused item (`item 7 (3(b)): exam 'final' is not one of
mini, mid, end`). It exits non-zero when anything was refused. Fix those
items and run it again. Everything that went in already is skipped.

## Example `tutor/pyq/CY1107.json`

```json
[
  {
    "code": "CY1107",
    "path": "library/archive/CY1107/End Term Previous Year Questions/End Term 2024-25.pdf",
    "exam": "end", "year": 2024,
    "qno": "1(a)", "marks": 2,
    "unit": "Fuels", "topic": "Knocking, octane and cetane number",
    "question_md": "Define octane number.",
    "answer_md": "**Octane number** is the percentage by volume of iso-octane in a mixture of iso-octane and n-heptane that has the same knocking tendency as the fuel under test, in a standard engine. A higher octane number means less knocking."
  },
  {
    "code": "CY1107",
    "path": "library/archive/CY1107/End Term Previous Year Questions/End Term 2024-25.pdf",
    "qno": "3(b)", "marks": 5,
    "unit": "Water", "topic": "Numericals on water purification",
    "question_md": "Calculate the temporary and permanent hardness of a water sample containing $Mg(HCO_3)_2 = 7.3$ mg/L, $Ca(HCO_3)_2 = 16.2$ mg/L, $MgCl_2 = 9.5$ mg/L and $CaSO_4 = 13.6$ mg/L."
  },
  {
    "code": "CY1107",
    "path": "library/archive/CY1107/End Term Previous Year Questions/End Term 2024-25.pdf",
    "qno": "3(b) OR", "marks": 5,
    "unit": "Water", "topic": "Zeolite process",
    "question_md": "Explain the zeolite process of water softening with reactions. Give one advantage and one limitation. [Figure: zeolite softener, hard water in at the top, soft water out at the bottom]"
  }
]
```

(In JSON, a LaTeX backslash is written twice: `"$\\frac{a}{b}$"`.)
