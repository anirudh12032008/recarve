# Tutor v2: question-first practice on a written curriculum

Anirudh's feedback on the first real lessons (Types of polymerization, Advanced):
too much text, too much theory beyond what the papers ask, PYQ answers shown
before he could try, a click needed after every answer, no sense of what is
left in a topic, and about $1 a topic. This replaces the free-form lesson with
a practice loop that runs on content written once, ahead of time, and calls the
API only to grade a written answer or answer a doubt.

Measured on live, the 8-turn Advanced lesson: $1.11. Each turn sent ~30k
tokens of system prompt (persona 7k chars, exam scheme 4k, pack topic 7k, and
the whole unit's past papers with answers), and the 5-minute prompt cache
expired between answers on five of the eight turns, so the 30k was re-written
at 1.25x the Opus input price each time.

## What a lesson is now

One topic = one session, always. Clicking a topic opens its session; there is
no "new lesson". The screen has three parts:

- **Sidebar**: the topic's subtopics, each with a status dot
  (grey not started, green clear, yellow minor gaps, red struggling) and the
  score. When every subtopic is green or yellow, the topic is done and the
  screen moves to the next topic in the unit, then the next unit.
- **The card**: one subtopic at a time. Question first by default. MCQs are
  graded in the browser for free. Written answers go to the grader.
- **Doubts**: a box under the card, always there. "Why does this happen?",
  "do this again, I did not get it". Answered by the API with only this
  subtopic's notes as context.

### The loop, per subtopic

1. Show a question. Buttons: answer it, or "I don't know".
2. Grade it.
   - Right: the model answer's key points and only the points the student
     missed. No theory. Then on.
   - Wrong or "I don't know": the theory (short), the model answer, "Any
     doubts?" with the doubt box, and Continue.
3. Continue moves on by itself: the next question in this subtopic if it is
   not yet clear, else the next subtopic, else the next topic. After a right
   answer the next card comes without a click.

A subtopic is **green** after a right answer at level 2 or above with no
wrong answer before it, or two right in a row. **Red** after two wrong
answers with no right one since. **Yellow** otherwise once tried. A wrong
answer at level 1, or two red subtopics in the topic, switches the topic to
*basics*: each subtopic's `basics_md` is shown before its theory and level 1
questions come first. A right first answer at level 2 skips level 1 questions
in the following subtopics (the fast-learner path; the default).

### The theory / questions slider

Per subject, three positions, saved in the profile next to language and tier:

- **Questions**: question first; theory only after a wrong answer. Default.
- **Mixed**: the subtopic's key points first (three to five lines), then the
  question.
- **Theory**: the theory first, then the question.

### Pace and coverage

A unit should take a student who already studied it one to two hours. A topic
is two to four subtopics; a fast student answers one level-2 question per
subtopic and moves on. Finishing a topic means having been asked something on
every subtopic, and the subtopics together cover every concept and every
numerical type the topic's past papers ask, and what could reasonably be
asked. A topic is never "done" with a gap left in it.

### Show solution

Every question has **Show solution** next to Answer and I don't know: it
shows the stored model answer, nothing more, with no model call. It counts the
question as seen and moves nothing on the subtopic's status.

## What is static and what is not

Static, written once by agents and checked in (`tutor/course/`): subtopics,
theory, basics, key points, model answers, marking points, MCQs, structure
drawings, and which past-paper questions belong to which subtopic. Past-paper
text, answers and figure crops stay in `pyq_questions`; the curriculum points
at them.

Live API, the only two calls left:

- **Grade** (`POST /tutor/grade`): the question, its marking points and model
  answer, the student's answer. Returns JSON: score, points hit, points
  missed, a one-line verdict, a one-line note for memory. No history.
- **Doubt** (`POST /tutor/doubt`, streamed): the subtopic's theory and key
  points, the question on screen if any, the last six doubt messages, the
  student's weak notes in this topic. Short answers, PYQ level.

Standard runs both on `claude-haiku-5-5`, Advanced on `claude-sonnet-5-5`; Opus is not used by the tutor at all (the older lessons' Advanced tier moves to Sonnet too)
(env-overridable as before). A grade is about 1.5k tokens in and 300 out:
well under a cent on either. A topic of practice is a few cents.

## Memory

`tutor_progress` holds one row per student, subject, unit, topic, subtopic:
status, right, wrong, the last score, the level reached, and up to three
short notes from the grader ("forgets that condensation can eliminate HCl").
The topic's `tutor_mastery` level is derived from its subtopics (all green 5,
no red and half green 4, any yellow 3, any red 2, started 1), and its note
lists the red and yellow subtopics, so the subject page, the meters and the
older lesson prompt keep working.

## The curriculum format

One file per unit: `tutor/course/<CODE>/<unit number>.json`, unit number
1-based in `syllabus.json` order. One agent writes one file.

```json
{
  "code": "CY1107",
  "unit": "Materials",
  "written": "2026-10-19",
  "by": "claude-sonnet-5-5, in a Claude Code session",
  "topics": [{
    "topic": "Types of polymerization",
    "subtopics": [{
      "id": "functionality",
      "title": "Monomers and functionality",
      "theory_md": "At most 150 words, at the depth the past papers ask.",
      "basics_md": "At most 100 words: the idea underneath, from zero.",
      "key_points": ["three to five lines an answer must contain"],
      "figures": [
        {"smiles": "C=C", "caption": "Ethylene"},
        {"smiles": "C=C>>CC", "caption": "a reaction: reactants >> products"}
      ],
      "questions": [
        {"id": "q1", "kind": "mcq", "level": 1, "q": "...",
         "options": ["A", "B", "C", "D"], "answer_index": 2, "why": "one line"},
        {"id": "q2", "kind": "short", "level": 2, "marks": 3, "q": "...",
         "model_answer_md": "the full-marks answer",
         "points": ["each marking point, one line"]},
        {"id": "q3", "pyq": "<qkey>", "level": 2,
         "points": ["marking points for this past-paper question"]}
      ]
    }]
  }]
}
```

Rules for writers:

- `unit` and `topic` are the exact strings from `syllabus.json`, every topic
  of the unit, in order.
- Two to five subtopics per topic; each is one thing a question can test.
  Subtopic ids are unique in the topic, lowercase-with-hyphens.
- Three to six of your own questions per subtopic, at least one MCQ and one
  written (`short`, `long` or `numerical`), levels 1-3 (1 recall, 2 what the
  paper asks, 3 the hard paper question). Every past-paper question of the
  topic goes into the subtopic it tests as a `pyq` question with marking
  points; the bank has its text, answer and figure. Question ids are unique
  in the topic.
- `qkey` comes from `backend/dev/course_pyqs.py`: the first 16 hex of
  sha256 of the code and the normalised question text.
- Theory is **PYQ level**: what earns the marks, nothing more. A reaction,
  not its mechanism, unless a paper asks for the mechanism. Initiators,
  catalysts, conditions are named, not explained.
- Structures and reactions are SMILES in `figures`, drawn in the page by
  smiles-drawer as real skeletal formulas: no image files, no licensing,
  nothing fetched at request time. Only SMILES you are sure of.
- `$...$` LaTeX for maths, `$\mathrm{H_2SO_4}$` for formulae. No em-dashes.
- Every number computed before it is written.
- `backend/dev/check_course.py <file>` passes.

The existing packs (`tutor/packs/`) are the starting material where they
exist: split into subtopics and trimmed to PYQ level. Five subjects have no
pack (PY1102, CE1103, ME1104, CS1105, HS1106); their agents write from the
syllabus, the PYQ bank and the standard texts, and may search the web.

## Generation

49 units, one Sonnet agent each, in the Claude Code session (the plan, not
API credit). Pilot first: CY1107 unit 3 by one agent, reviewed against this
spec, then the rest in two waves.

## What is kept

Old lessons stay readable in the history. `/tutor/start` and `/tutor/say`
stay for them and their tests, but Teach opens practice. A topic with no
curriculum file yet runs practice on its pack questions (one subtopic per
topic), so nothing breaks while the agents work.
