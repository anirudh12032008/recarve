# The AI tutor

A teacher that lives in recarve and teaches the whole syllabus, subject by
subject, topic by topic, for the student who has no recording of the lecture.
It teaches, asks, listens to the answer, adjusts, and says out loud what the
exam cares about. Past papers (PYQs) are the evidence for "what the exam
cares about"; where they have not been extracted yet, an estimate is shown
and labelled as one.

One person uses it. Everybody else the section trusts may read what it
taught, read-only. The API spend is deliberate and allowed: this is the one
feature in the app where the best model is the right model.

## 1. Shapes on disk and in the database

### Files

| Path | What |
|---|---|
| `tutor.py` | Everything server-side: prompts, DB access, the streaming call, the study plan, PYQ import. `notes.py` only delegates. |
| `tutor_pyq.py` | The PYQ pipeline: extract questions from archive PDFs with the API, answer them, aggregate importance. CLI `notes.py tutor pyq ...`. |
| `web/tutor.js` | The whole Tutor screen. Spliced into `PAGE`'s single `<script>` at `__TUTOR_JS__`, just before `seedHistory();`. Plain ES2020, no modules, no build. |
| `web/tutor.css` | Its styles. Spliced into the SECOND `<style>` (Tailwind's) at `__TUTOR_CSS__`. The page must keep exactly two `<style>` blocks and one `<script>` block; tests count them. |
| `tutor/packs/<code>.json` | Hand-written (by agents) teaching packs, one per subject: per topic, the gist, formulas, traps, practice questions with answers, an importance estimate. Read at request time, cached by mtime. |
| `tutor/exam-scheme.json` | The institute's marks split (mini 10, mid 20, end 40, attendance 10, internal 30) and the books the seniors recommend, for the prompts. |
| `supabase/migrations/0056_the_tutor.sql` | Tables and policies below. |
| `backend/tests/test_tutor.py` | DB policy tests + server tests with a stubbed model. |
| `.claude/skills/pyq-extract/SKILL.md` | The Claude Code agent that reads the archive PDFs on the machine that has them and imports the questions. |

### Tables (migration 0056)

All per-person tables key on `owner_id uuid references profiles(id) on delete cascade`.

```
tutor_sessions
  id uuid pk default gen_random_uuid()
  owner_id uuid not null
  subject_code text not null references subjects(code)
  unit text, topic text              -- null topic = whole-subject session ("ask")
  mode text not null check (mode in ('teach','quiz','pyq','revise','ask'))
  lang text not null default 'english' check (lang in ('english','hinglish'))
  depth text not null default 'exam' check (depth in ('basics','exam','deep'))
  title text not null
  model text
  status text not null default 'open' check (status in ('open','done'))
  turns int not null default 0
  cost_usd numeric(10,6) not null default 0
  created_at, updated_at timestamptz not null default now()

tutor_messages
  id bigserial pk
  session_id uuid not null references tutor_sessions on delete cascade
  role text not null check (role in ('student','tutor'))
  kind text not null default 'say'   -- say|lesson|check|answer|action|pyq|summary
  body text not null                 -- markdown; tutor turns keep the raw model text incl. tags
  meta jsonb not null default '{}'   -- parsed <state>, checks, pyq ids, action name
  input_tokens int, output_tokens int, cache_read_tokens int, cache_write_tokens int
  cost_usd numeric(10,6) not null default 0
  created_at timestamptz not null default now()
  index (session_id, id)

tutor_mastery
  owner_id, subject_code, unit, topic   pk
  level int not null default 0 check (level between 0 and 5)
  attempts int not null default 0, correct int not null default 0
  importance text check (importance in ('low','medium','high','must'))  -- the tutor's own flag
  importance_why text
  note text                           -- tutor's one-line picture of this topic for this student
  last_seen timestamptz

tutor_profile
  owner_id, subject_code  pk
  lang, depth, tier (same checks as sessions)
  notes text                          -- the tutor's running picture of the learner (updated by <state>)
  updated_at

pyq_questions
  id uuid pk
  subject_code text not null references subjects(code)
  document_id uuid references archive_documents(id) on delete set null
  exam text check (exam in ('mini','mid','end')), year int, set_for_section text
  qno text, marks numeric(5,1)
  unit text, topic text               -- mapped onto syllabus.json names, or null
  question_md text not null, answer_md text
  answer_model text, extracted_by text
  fingerprint text not null unique    -- sha256(subject_code + normalised question)[:32]; makes import re-runnable
  created_at
  index (subject_code, unit, topic), index (subject_code, year desc, exam)
```

Policies (every table: RLS on, grants to authenticated):

- `tutor_sessions`, `tutor_messages`, `tutor_mastery`, `tutor_profile`: **select** `is_trusted()`; **insert/update/delete** `owner_id = auth.uid()` (messages: the session's owner). Trusted people read, the owner writes.
- `pyq_questions`: **select** `is_approved()`; **write** `is_admin()`.

### The teaching pack (`tutor/packs/<code>.json`)

```json
{
  "code": "BS1111", "name": "Biology for Engineers",
  "written": "2026-10-19", "source": "agent-written from the MANIT 2025-26 syllabus; importance is an ESTIMATE until PYQs are imported",
  "exam_notes": "2-3 sentences on how this paper is usually set",
  "units": [{
    "unit": "Cell Biology",                       // EXACT unit name from syllabus.json
    "topics": [{
      "topic": "Cell theory",                     // EXACT topic name from syllabus.json
      "importance": "high",                       // low|medium|high|must
      "why": "one line: why the exam cares",
      "gist": "120-250 words of markdown that a good teacher would say first. LaTeX with $...$.",
      "key_points": ["5-10 bullets a student must be able to reproduce"],
      "formulas": ["$...$ with a 5-word label", "..."],   // [] if none
      "traps": ["common mistakes / what the marker penalises"],
      "asked_as": "how questions on this topic are typically phrased (e.g. 'Derive ... 5 marks', 'Differentiate between ... 2 marks')",
      "questions": [{
        "q": "markdown", "a": "markdown answer, full marks", "marks": 5,
        "kind": "short|long|numerical|mcq",
        "options": ["A","B","C","D"], "answer_index": 1      // mcq only
      }]
    }]
  }]
}
```

Six to eight questions per topic, at least two of them MCQ (for instant checks without the API). A pack may add a `"syllabus_estimated": true` and its own units when `syllabus.json` has none for the code (HS1112).

## 2. HTTP

Everything lives under `/tutor`. `notes.py` routes any `/tutor` or `/tutor/...` to `tutor.handle(handler, args)` for both GET and POST. Role gate (in `ROLE_REQUIRED`): every `/tutor...` **GET** needs `trusted`; every **POST** needs `RECARVE_TUTOR_ROLE` (env, default `admin`). Visitors are refused on POST by the existing gate. JSON errors are `{"error": "..."}`.

### GET

| Path | Returns |
|---|---|
| `/tutor` | `{owner: bool (may this person write), model, role_needed, subjects: [SubjectView], recent: [SessionRow], plan: [PlanItem], spend: {total, today, month}}` |
| `/tutor/subject?code=X` | `SubjectView` for one subject + `sessions: [SessionRow]` + `pyq_summary: {count, by_unit: {unit: n}, years: [...]}` |
| `/tutor/session?id=UUID` | `{session: SessionRow, messages: [MessageRow], pyqs: {id: PyqRow} (every PYQ any message cites), owner: bool}` |
| `/tutor/pyq?code=X[&unit=&topic=&year=&exam=]` | `{questions: [PyqRow], counts: {unit: {topic: n}}}` |

```
SubjectView = {code, name, syllabus_estimated: bool, progress: 0..1, importance_source: 'pyq'|'estimate'|'none',
               units: [{unit, progress, topics: [{topic, level, attempts, correct, last_seen,
                        importance: 'low'|'medium'|'high'|'must'|null, importance_source, why, pyq_count, pyq_marks,
                        has_pack: bool, sessions: n}]}]}
SessionRow  = {id, code, unit, topic, mode, lang, depth, tier, title, status, turns, cost_usd, created_at, updated_at, last_line}
MessageRow  = {id, role, kind, body, meta, cost_usd, created_at}
PlanItem    = {code, unit, topic, reason, score, suggested_mode}
PyqRow      = {id, code, exam, year, section, qno, marks, unit, topic, question_md, answer_md, title (archive doc title), path (archive file path or null)}
```

### POST (owner only)

| Path | Body | Response |
|---|---|---|
| `/tutor/start` | `{code, unit?, topic?, mode, lang?, depth?}` | **stream** (below). Creates the session and streams the tutor's first turn. |
| `/tutor/say` | `{session_id, body, kind?: 'say'|'answer'|'action', action?: 'deeper'|'example'|'simpler'|'quiz'|'pyq'|'summary'|'next'}` | **stream** |
| `/tutor/mark` | `{session_id, check_id, picked, correct: bool}` | `{mastery: TopicMastery}` — an MCQ graded on the client; updates attempts/correct and nudges level. |
| `/tutor/mastery` | `{code, unit, topic, level}` | `{mastery}` — the student overrides ("I know this"). |
| `/tutor/settings` | `{code, lang?, depth?}` | `{profile}` |
| `/tutor/session/close` | `{id}` | `{ok}` — status done. |
| `/tutor/session/delete` | `{id}` | `{ok}` |
| `/tutor/pyq/import` | `{items: [ImportItem]}` (admin) | `{inserted, skipped, mapped}` — same shape the CLI import takes. |

```
ImportItem = {code, document_title?, path?, exam?, year?, section?, qno?, marks?, unit?, topic?, question_md, answer_md?, extracted_by?}
```

### The stream

`Content-Type: application/x-ndjson`, `Transfer-Encoding: chunked`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`. One JSON object per line:

```
{"t":"session","session":SessionRow}          -- first line of /tutor/start only
{"t":"delta","text":"..."}                     -- repeated; raw model text, tags included
{"t":"done","message":MessageRow,"mastery":[TopicMastery],"session":SessionRow,"spend":{...}}
{"t":"error","error":"..."}
```

The client renders deltas as they arrive and parses the tags itself; the server parses the finished text once and stores it.

## 3. The tutor's language (tags in the model's output)

The tutor writes markdown (KaTeX maths with `$...$`/`$$...$$`). Three tags, each on its own line(s):

```
<check id="c1" kind="mcq">
{"q": "Which organelle ...?", "options": ["Nucleus","Mitochondrion","Ribosome","Golgi"], "answer": 1, "why": "..."}
</check>

<check id="c2" kind="short">
{"q": "In two lines, state Euler's theorem for homogeneous functions.", "marks": 2}
</check>

<pyq id="UUID"/>                       -- show this past-paper question as a card where the tag sits

<state>{"level": 3, "importance": "high", "why": "asked in 4 of 6 end terms", "note": "confuses mitosis phases", "profile": "prefers worked examples; weak on terminology", "next": "Cell growth and division", "done": false}</state>
```

Rules the prompt states and the server enforces:
- At most one `<state>` per turn, last thing in the turn. Every field optional. `level` is the tutor's current estimate of the student's mastery of THIS topic, 0 (never seen) to 5 (exam-ready). `profile` replaces `tutor_profile.notes`. `done: true` means the topic is finished for now.
- The client hides `<state>`, renders `<check>` as a card (MCQ: tap an option, instant grade, "Explain" sends the choice to the tutor as kind `answer`; short: a textarea whose text goes to `/tutor/say` as kind `answer`), renders `<pyq>` from the `pyqs` map.
- The server stores the raw text and `meta = {state, checks: [...], pyqs: [...]}`.

## 4. The call

Two tiers, chosen per subject (saved on `tutor_profile.tier`) and per lesson (`tutor_sessions.tier`). **Standard** runs `claude-sonnet-5-5` at effort `medium` (`RECARVE_TUTOR_MODEL` / `RECARVE_TUTOR_EFFORT` override) with the topic's pack and the topic's PYQs. **Advanced** runs `claude-opus-5-5` at effort `high` (`RECARVE_TUTOR_MODEL_ADVANCED` / `RECARVE_TUTOR_EFFORT_ADVANCED`) with the whole unit's PYQs and the tutor's profile notes from the student's other subjects, and its tier line in the learner block asks for a diagnostic opener, anticipated misconceptions and worked examples. `/tutor` returns `tiers: {standard: {model, effort, price_in}, advanced: {...}}` so the screen can say what each costs. Both: streaming, adaptive thinking (omit the parameter), `output_config.effort` per tier, `max_tokens` 16000, `betas=["server-side-fallback-2026-07-01"], fallbacks="default"` via `client.beta.messages.stream`, falling back to `client.messages.stream` on `TypeError`/`BadRequestError` the way `make_notes` does. `stop_reason == "refusal"` becomes a readable error line.

System prompt, in this order, with `cache_control` on the last stable block:
1. Persona and rules (stable).
2. The exam scheme and the subject's full syllabus (stable per subject).
3. The pack entry for this topic, and the PYQs mapped to this topic (ids, exam, year, marks, question, answer), capped at ~40 questions / 60k characters, newest first.
4. The learner: `tutor_profile.notes`, the mastery row for this topic and the unit's other topics, lang, depth, tier, mode; on advanced, the profile notes from every other subject too (how they learn carries over).

Then the session's messages, student/tutor alternating, tutor turns verbatim (tags included, so the model sees what it asked).

Cost per message from `PRICES` in notes.py plus cache rates: cache write 1.25× input, cache read 0.1× input. Stored on `tutor_messages.cost_usd`, summed onto the session, shown in the UI.

Persona, in short: a patient, exact teacher for a MANIT first-year engineer. Teach in small steps, one idea at a time, check understanding before moving on. Say explicitly what is important for the mini/mid/end term and why (cite PYQs when given, say "estimate" when not). Ask one question at a time. Grade honestly; when the student is wrong, say what was wrong and re-teach that piece. Hinglish when asked, technical terms in English always. Never invent a PYQ; only cite ids given. End each teaching turn with either a `<check>` or a one-line question.

## 5. The study plan

`plan(subject_views)` ranks `(code, unit, topic)` by `w_importance × (1 − level/5) × recency` where importance weights are must 4 / high 3 / medium 2 / low 1 / unknown 1.5, and recency is 1 for never seen, 0.6 if seen in the last 2 days, 0.8 otherwise. Top 8 across subjects on `/tutor`, per subject on `/tutor/subject`. Reason strings are human: "4 end-term questions, you are at level 1".

Importance for a topic: if any PYQs are mapped to the topic, derive it from count and marks across the subject (top quartile → must, then high, medium, low) and mark `source: 'pyq'`; else the tutor's own flag from `tutor_mastery.importance` (`source: 'tutor'`); else the pack's estimate (`source: 'estimate'`); else null.

## 6. The screen (`#tutor`)

A fifth top-level tab, "Tutor", with its own icon, a bottom-bar button, and a tour step. In the drawer it is a row inside Classes, after Past papers, not a fifth section: the page tests count the drawer's sections (four for a student, five for an admin) and a tab that is part of studying belongs under Classes anyway. Routes:

- `#tutor` — Home: "Next up" (the plan, tap to start), subject cards (progress ring, importance source, spend), recent sessions.
- `#tutor/CY1107` — Subject: the syllabus as a tree; each topic row shows a mastery dot (0–5), an importance chip (must/high/medium/low, with its source on hover/tap), PYQ count; actions Teach / Quiz / PYQs; "Continue" on the last open session; lang + depth segmented controls (saved via `/tutor/settings`).
- `#tutor/CY1107/s/UUID` — Lesson: the chat. Streamed markdown with KaTeX, check cards, PYQ cards, a quick-action row (Go deeper · Example · Simpler · Quiz me · Show PYQs · Summarise · Next topic), a composer (`askBox`-style textarea), the mastery dot for the topic live in the header, spend for this session. Read-only (no composer, no actions) when `owner` is false, with a quiet line "You are reading <name>'s lesson".
- `#tutor/CY1107/pyq` — the PYQ bank: filter by unit/topic/year/exam, each question a card with the answer under a `<details>`; "Teach me this" starts a `pyq` session on its topic.

House style: `.rows/.row`, `.sect`, `.seg`, `.blank`, `.msg/.said/.meta`, `.askbox`, `mdSafe` + `typeset` for every model string, `busy/busyDone/oops` for feedback, 44px taps, the token scale. Write controls carry class `w` so `body.visitor` hides them. State lives in module variables (the list pane is rebuilt on every `render()`); drafts in localStorage.

## 7. PYQs

`tutor_pyq.py`, CLI `notes.py tutor pyq extract [--code X] [--model M] [--limit N]`, `... pyq answer`, `... pyq import FILE.json`, `... pyq status`. Runs on the machine that has `library/archive/...` (the server). For each `archive_documents` row with `kind='paper'` and no questions yet: send the PDF as a document block with the subject's syllabus and ask for strict JSON `[{qno, marks, question_md, unit, topic}]`; insert with the fingerprint; then `answer` fills `answer_md` for rows without one, in batches, with the pack entry as context. Both are resumable and idempotent. `status` prints counts per subject.

The Claude Code route to the same table is `.claude/skills/pyq-extract/SKILL.md`: an agent reads the PDFs with its own eyes (Read tool), writes `tutor/pyq/<code>.json` in the `ImportItem` shape, and runs `notes.py tutor pyq import`. Either route ends in the same rows; the tutor does not care which.
