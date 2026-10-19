# Teaching packs

One JSON file per subject, `<code>.json`. The tutor reads the entry for the
topic it is teaching into its prompt, so the student gets the same gist,
formulas and traps every time, and the practice questions give it (and the
screen) something to ask before the API is ever called.

They are written by people and agents, not generated at request time, and
they are checked in. Correct them like code.

## Shape

See `docs/specs/2026-10-19-ai-tutor-design.md` §1 "The teaching pack". In
short:

```json
{
  "code": "BS1111", "name": "Biology for Engineers",
  "written": "2026-10-19",
  "source": "agent-written from the MANIT 2025-26 syllabus and the standard first-year texts; importance is an ESTIMATE until past papers are imported",
  "exam_notes": "2-3 sentences on how this paper is usually set",
  "syllabus_estimated": false,
  "units": [{
    "unit": "Cell Biology",
    "topics": [{
      "topic": "Cell theory",
      "importance": "high",
      "why": "one line: why the exam cares",
      "gist": "120-250 words of markdown a good teacher would say first; maths as $...$",
      "key_points": ["5-10 things the student must be able to reproduce"],
      "formulas": ["$F = ma$ — Newton's second law"],
      "traps": ["what the marker penalises, what students confuse"],
      "asked_as": "how questions on this topic are typically phrased, with marks",
      "questions": [
        {"q": "...", "a": "full-marks answer in markdown", "marks": 5, "kind": "short"},
        {"q": "...", "options": ["A", "B", "C", "D"], "answer_index": 1, "a": "why B", "marks": 1, "kind": "mcq"}
      ]
    }]
  }]
}
```

## Rules

- `unit` and `topic` are the EXACT strings from `syllabus.json`, in the same
  order. The tutor joins on them. If `syllabus.json` has nothing for the code,
  the pack supplies its own units and sets `"syllabus_estimated": true`.
- `importance` is `low | medium | high | must`. It is an estimate from how
  Indian first-year engineering papers are normally set, and the screen says
  so until real past-paper counts replace it. Do not dress an estimate up as
  a fact about a particular year's paper.
- Six to eight questions per topic, at least two `mcq`. `kind` is `short`
  (2-3 marks), `long` (5-10, derivation or essay), `numerical`, or `mcq`.
  Answers are the answers an examiner would give full marks to, not hints.
- Technical terms stay in English. Maths is LaTeX: `$...$` inline,
  `$$...$$` display. Chemistry formulas too: `$\mathrm{CaCO_3}$`.
- Say the textbook-standard thing (H.K. Dass / Grewal for maths, Jain & Jain
  for chemistry, R.K. Rajput for BEEE and mechanics, NCERT XI-XII for
  biology). Where textbooks differ on a convention, name both.
- Valid JSON. `python -c "import json,sys; json.load(open(sys.argv[1]))" tutor/packs/X.json`.
