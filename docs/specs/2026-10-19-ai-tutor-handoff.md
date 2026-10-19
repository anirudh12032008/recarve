# Handoff: the AI tutor, continuing on the Mac

Branch `claude/recarve-ai-tutor-pyq-p3iygq`, draft PR #1. Read
`docs/specs/2026-10-19-ai-tutor-design.md` first; it is the contract every
file below was built against.

## What is on the branch and verified

| Piece | File(s) | Verified how |
|---|---|---|
| Schema | `supabase/migrations/0056_the_tutor.sql` | `migrate.py` applies; policy, grant and matrix tests pass |
| Hooks | `notes.py` (`/tutor` → `tutor.handle`, `__TUTOR_JS__`/`__TUTOR_CSS__`, `TUTOR_ROLE`, `tutor` CLI, prices) | full suite |
| Backend | `tutor.py`, `backend/tests/test_tutor.py` | 28 tests against a stubbed `model_stream`; real server, real DB |
| Screen | `web/tutor.js`, `web/tutor.css`, PAGE edits in `notes.py` | `test_page.py` green; driven in headless Chromium against a mock server (phone + desktop, light + dark) |
| PYQ pipeline | `tutor_pyq.py`, `backend/tests/test_tutor_pyq.py`, `.claude/skills/pyq-extract/SKILL.md` | 20 tests with a fake client and a tiny real PDF |
| Packs | `tutor/packs/{MC1101,EE1108,ME1109,CY1110,BS1111,HS1112,CY1107-b}.json` | `backend/dev/check_pack.py` clean on each |

Full suite on the cloud box: 1032 passed, 1 deselected (a worker network
test that cannot bind in that sandbox).

## Done on the Mac, 2026-10-19 (second session)

- Baseline: 1031 passed, 2 failed, both in `test_worker.py`
  (`test_a_blip_while_reporting_is_not_a_lecture_failure`,
  `test_jobs_says_when_the_worker_was_last_heard_from`). Both are timing
  flakes that fail on master too and pass in isolation; nothing on this
  branch touches the worker.
- `tutor/packs/CY1107.json` is whole (51 topics, checker clean); the halves
  are gone.
- The old API key was dead (401 on the Mac and on the VM). Anirudh put a
  new one in the Mac's `.env` and it was copied to `~/app/.env` on the VM
  (the old file is `~/app/.env.pre-tutor-key`); both verified with a
  one-token call.
- Real lessons, read as a student: Cell theory on Standard (Sonnet, 264
  words, importance as an estimate, the Hooke trap, one check; the MCQ
  graded on tap; "Quiz me" gave one 3-mark written check; a half answer
  was marked "partly right, about 1.5 out of 3" with the missing years
  named; the meter went 0 to 2 on evidence; 4 cents for the lot) and
  Hardness on Advanced in Hinglish (Opus, romanised Hindi with every term
  in English, no Devanagari, the estimate said out loud, one check, 22 s,
  9 cents).
- Two tiers (Standard on Sonnet 5.5 medium, Advanced on Opus 5.5 high with
  the unit's PYQs and the cross-subject learner notes), a third segmented
  control on the subject page.
- **Deployed 2026-10-19**: migration 0056 applied to live (backup
  `~/backups/db-2026-10-19.sql.gz` first), `notes.py` (md5 4d7a6219),
  `syllabus.json`, `tutor.py`, `tutor_pyq.py`, `web/tutor.{js,css}` and
  `tutor/` copied to `~/app`, service restarted, home 200, `/tutor` 403 for
  anonymous. Rollback: `~/app/notes.py.pre-70327a6` + restart (the tutor
  tables can stay). Extraction of the 296 papers is running on the VM from
  a copy of the branch at `~/app-tutor` (`pyq.log` there), `--max-cost 40`
  each for extract and answer.
- Everything the screen does without the model was driven for real instead,
  through `backend/dev/fake_tutor.py` (the server with `tutor.model_stream`
  swapped for a canned teacher) on a fresh `recarve_tutor` database with an
  admin and a trusted account: streamed turns with KaTeX, the MCQ card
  grading on tap and `/tutor/mark` moving attempts, a short-answer card
  going round trip, the meter following `<state>`, spend summing per lesson,
  Hinglish and depth saved through `/tutor/settings`, every quick action, a
  trusted reader on the phone over the LAN URL seeing the lesson read-only
  with no dock, `POST /tutor/start` refused for that reader (403).
- `tutor/pyq/EE1108.json`: one real paper (Section I mid term, Oct 2025)
  transcribed by eye and imported. With it the EE1108 chips read "from 10
  past-paper questions", the subject page shows "4 PYQs, 2 marks" style
  counts, `<pyq/>` cards render inside a lesson with the answer under
  `Answer`, and the bank page filters and offers "Teach me this".
- One fix from that: a `<pyq/>` whose id the archive does not have used to
  sit on "Fetching this question from the archive…" for ever; it now says
  "This past-paper question is not in the archive." once the lesson is in
  hand (`web/tutor.js`, `tuSegNode`).
- Migration 0056 rehearsed on a restore of live (116 profiles): applies
  clean, five tables with RLS, 19 policies.
- Known, left alone: a lesson whose first turn failed (bad key, model error)
  stays as an open session with 0 turns and shows in Recent; "Send again"
  on it works, so it is kept for the retry.

## Deploying, when the key works

The server's `~/app` has `web/` and `supabase/` already but none of the
tutor files. Besides `notes.py` and `syllabus.json` this deploy copies
`tutor.py`, `tutor_pyq.py`, `web/tutor.js`, `web/tutor.css`, `tutor/`
(packs, `exam-scheme.json`, `pyq/`), then
`psql -d recarve -f supabase/migrations/0056_the_tutor.sql`, then the key
into `~/app/.env`, then `sudo systemctl restart recarve`. Back up first
(`bash ~/backups/backup.sh`) and md5-check live against the commit you
think is there, as the state memory says. Run `notes.py tutor pyq import
tutor/pyq/EE1108.json` there too; the extraction and answering go on the
server because that is where the 295 PDFs are.

## What has NOT been verified

1. **No real call to the model has been made.** Every test stubs
   `tutor.model_stream` / the extraction client. The first thing to do on
   the Mac is open a lesson with a real `ANTHROPIC_API_KEY` and read it as
   a student would: does it teach in small steps, does the `<check>` card
   appear and grade, does the `<state>` move the meter, is the spend line
   sane. Then try Hinglish, then "Quiz me", then a short-answer check.
2. **The screen has only met the mock.** Run the real server and click
   through Home → subject → Teach → answer → back, on the phone too
   (the LAN URL `serve` prints).
3. **Nothing has read a real past paper.** The PDFs live in
   `library/archive/` on the server, not in the repo.

## Left to do, in order

1. **Chemistry units 1-3.** `tutor/packs/CY1107-a.json` is PARTIAL: all 7
   Lubricants topics and the first 8 Fuels topics are written and pass the
   rules; still missing are the last three Fuels topics ("Types of cracking",
   "Knocking, octane and cetane number", "Numericals on calorific value and
   ultimate analysis") and the whole Water unit (12 topics, names in
   `syllabus.json`). Write those into the `-a` file in syllabus order (the
   writer's prepared worked figures: hardness sample temporary 40 / permanent
   30 / total 70 ppm; EDTA 250 total, 150 permanent, 100 temporary ppm;
   lime-soda 222 mg/L lime and 318 mg/L soda, i.e. 2.22 kg and 3.18 kg per
   10,000 L; zeolite regeneration 213.7 ppm NaCl; ion exchange 1460 g HCl;
   Dulong for C 84%, H 5.5%, O 4%, S 1.5%: HCV 8545.8 and LCV 8255.2
   kcal/kg), run the checker, then merge:
   ```bash
   python - <<'EOF'
   import json
   a = json.load(open("tutor/packs/CY1107-a.json"))
   b = json.load(open("tutor/packs/CY1107-b.json"))
   a["units"] += b["units"]
   a["exam_notes"] = a["exam_notes"].rstrip() + " " + b["exam_notes"]
   json.dump(a, open("tutor/packs/CY1107.json", "w"), indent=1, ensure_ascii=False)
   EOF
   python backend/dev/check_pack.py tutor/packs/CY1107.json && git rm -q tutor/packs/CY1107-a.json tutor/packs/CY1107-b.json
   ```
   `tutor.py` only reads `<code>.json`, so until the merge the chemistry
   pack is invisible to the tutor.
2. **Run it for real** (see above) and fix what the first real lesson
   shows. Likely candidates: the tutor writing the check question in prose
   as well as in the card; `<state>` JSON with unescaped LaTeX (parse_turn
   repairs the common cases); turn length.
3. **Extract the past papers** on the machine with `library/archive/`:
   ```bash
   python notes.py tutor pyq status
   python notes.py tutor pyq extract --code CY1107 --limit 3     # look at the rows before the rest
   python notes.py tutor pyq extract --max-cost 40
   python notes.py tutor pyq answer --max-cost 40
   ```
   or the `pyq-extract` skill with Claude Code, one subagent per paper.
   Importance chips switch from "estimate" to "from N past-paper questions"
   on their own once rows exist.
4. **Deploy**: `python backend/dev/migrate.py` is dev-only; apply
   `0056_the_tutor.sql` to the live database the way the earlier ones were.
   `.env` needs `ANTHROPIC_API_KEY`; optional `RECARVE_TUTOR_ROLE`
   (default `admin`), `RECARVE_TUTOR_MODEL` / `RECARVE_TUTOR_EFFORT` for the
   Standard tier (defaults `claude-sonnet-5-5`, `medium`) and
   `RECARVE_TUTOR_MODEL_ADVANCED` / `RECARVE_TUTOR_EFFORT_ADVANCED` for
   Advanced (defaults `claude-opus-5-5`, `medium`).

## Running locally

```bash
source .venv/bin/activate && pip install -r requirements.txt
createdb recarve_test 2>/dev/null; python backend/dev/migrate.py
python -m pytest backend/tests -q -p no:cacheprovider
set -a; . ./.env; set +a
python notes.py serve --port 8000       # then open the Tutor tab
```

## Decisions worth knowing

- Writes under `/tutor` need `RECARVE_TUTOR_ROLE` (admin by default; live has `trusted` since 2026-10-19, so Anirudh's trusted login can teach);
  reads need `trusted`; the DB additionally lets an owner read their own
  rows whatever their rung.
- Trusted readers see the progress of whoever the tutor taught most
  recently (`?who=` picks another), read-only, no dock.
- A PYQ's fingerprint includes its sitting, so the same question in two
  years is two rows: that is what "asked in 3 of the last 4 end terms"
  needs. The ledger `library/.pyq-read.json` remembers papers that yielded
  nothing so they are not paid for again.
- Importance precedence: past-paper counts → the tutor's own `<state>` flag
  → the pack's estimate → none. The chip says which.
- Drawer: "Tutor" is a row under Classes, not a fifth section (the page
  tests count sections). The bottom bar has the fifth button.
- Spend is computed from `PRICES` plus cache rates (write 1.25×, read
  0.1×); Opus 5.5 cache reads are actually cheaper, so the readout
  slightly over-reports, by design.

## Past papers, checked on 2026-10-19 (extraction still running)

Checked from the Mac over ssh while `pyq extract --max-cost 40` was still
going on the VM (`~/app-tutor/pyq.log`); the `answer` pass had not started.

| subject | papers | extracted | questions | mapped to a topic |
|---|---|---|---|---|
| BS1111 | 18 | 17 | 145 | 137 |
| CE1103 | 35 | 35 | 220 | 152 |
| CS1105 | 22 | 22 | 216 | 214 |
| CY1107 | 30 | 30 | 353 | 343 |
| CY1110 | 21 | 20 | 271 | 248 |
| EE1108 | 35 | 34 | 250 | 245 |
| HS1106 | 21 | 6 | 43 | 42 |
| MC1101, ME1104, ME1109, PY1102, SA1141 | 114 | 0 | 0 | 0 |
| **total** | 296 | 164 | 1498 | 1381 |

Spend so far $4.18 for 164 papers, so the whole archive should come in
near $8, far under the cap; the answer pass is the bigger bill.

What the tutor derives from it, on the live database: CY1107 has 44 of 51
topics on past-paper importance (7 still estimates), BS1111 20 of 24,
EE1108 21 of 24; MC1101 still all estimates because its papers are not
reached yet. The plan reads "Cell organelles: 14 past-paper questions
(3 end, 3 mid, 8 mini), not started yet"; "Network theorems: 40".

The rows: a 15-row sample across BS1111 was faithful (question text, qno,
OR-choices, the MCQ options kept inline). No question appears twice within
one paper; the same question across years is kept as separate rows on
purpose (Lami's theorem four times, beam classification four times).
Problems to fix, none blocking:

1. **Section totals read as marks** on a few papers. CE1103 mid 2025 Q1 is
   stored with 30 marks and CE1103 mid 2024 Q1-3 with 35: the extractor
   took the section total printed beside a group of parts. Papers whose
   parts sum past the paper (CE1103 mids summing to 100, CY1107/CY1110
   2021-23 end terms summing to 90-102) deserve a look; the pre-2023 end
   terms may genuinely have been 70-100 mark papers under the old scheme.
   A cheap guard in `tutor_pyq.py`: when a paper's parts sum to more than
   twice its exam's total, null the marks on the parts above 10.
2. **117 unmapped rows (8%)**, two kinds: CE1103 shear force and bending
   moment (57 rows with no unit at all) and CY1110 biogeochemical cycles
   (21), which the papers ask every year but `syllabus.json` has no topic
   for; and a handful placed on a unit but no topic (CY1107 Materials 7,
   EE1108 Electrical Machines 4). The first kind is a syllabus gap, not an
   extraction bug: add the topics to `syllabus.json` and the packs, then
   re-run `extract` (the fingerprint makes it idempotent) or update the
   rows' unit/topic by hand.
3. **20 CY1110 mini-test rows have no year** because their
   `archive_documents` row has none ("Mini Test" folder without a year in
   the title). They count towards importance but not towards "asked in N
   of the last M"; set `year` on those documents and the rows inherit it
   on the next import.
4. 186 rows (12%) have null marks, nearly all MCQ parts of end-term Q1
   where the paper prints one total for the block. Expected; the chip
   counts questions, not marks, when marks are missing.

## Past papers, finished on 2026-10-19 (afternoon)

The API run was stopped at 171 papers ($4.18) because the same work runs
on the Claude Code plan: Sonnet agents read the other 125 scans with the
Read tool and wrote ImportItems, Opus agents wrote full-marks answers for
the 1556 API-extracted questions (every number computed in Python first),
and Sonnet agents located the printed figure for the 217 questions that
need one. All of it is live.

| what | count |
|---|---|
| papers extracted | 295 of 296 (the last is a note page, not a paper) |
| questions in the bank | 2382 |
| answered | 2382 (the last 24, ME1104 drawing constructions plus one MC1101 and one PY1102, were written by an in-session agent from the figure crops; the assumed dimensions are stated inside those answers) |
| with a figure crop | 217 (CE1103 112, EE1108 66, ME1104 29, CS1105 5, HS1106 2, CY1107 2, CY1110 1) |

- `tutor/pyq/<code>.json` is now an export of the whole bank from the live
  database, questions and answers, in ImportItem shape: importing it on a
  fresh database rebuilds the bank. Regenerate it the same way after the
  next extraction rather than editing by hand.
- Figures: migration `0057_pyq_figures.sql` adds `pyq_questions.figure`, a
  path under the library like `file_key`; the crops live at
  `~/app/library/pyq-figures/<question id>.png` on the server (14 MB, in
  the nightly library tarball, not in the repo). The crop boxes were read
  off 110-dpi page renders by agents and cut with `pdftoppm -x -y -W -H`;
  four were eyeballed and are right, the rest were not individually
  checked. The bank card and the lesson card show the image under the
  question (`.tu-fig`). The deploy now also copies `library/pyq-figures/`.
- Importance on the live site reads from past papers for every subject
  with a pack; MC1101 and the others without packs rely on the bank alone.
- The PDFs stay on the server as the archive and as the link on each card;
  the bank no longer needs them to be read.

## Practice replaces the free-form lesson (2026-10-19, live)

Anirudh's feedback after a real lesson: too much theory, PYQ answers shown
before he could try, a click after every answer, no view of what is left, and
about $1 a topic. The spec is `docs/specs/2026-10-19-tutor-practice.md`.

- Teach opens `#tutor/<CODE>/p/<unit n>/<topic n>`: one practice session per
  topic, question first, MCQs marked in the browser, written answers graded
  by `POST /tutor/answer`, doubts on `POST /tutor/doubt`, Show solution free.
- The course is static: `tutor/course/<CODE>/<unit n>.json`, 49 units, 891
  subtopics, 5149 questions, 2141 of them past papers referenced by qkey
  (all resolve against the live bank). Written by in-session Sonnet agents,
  one per unit; `backend/dev/check_course.py` must pass on any edit.
- Progress per subtopic is `tutor_progress` (migration 0058, applied live
  after a pg_dump to `~/backups/db-2026-10-19-pre0058.sql.gz`).
- Models: practice Standard `claude-haiku-5-5`, Advanced `claude-sonnet-5-5`;
  the older lessons' Advanced also moved off Opus to Sonnet.
- Rollback: `~/app/{notes.py,tutor.py,web/tutor.js,web/tutor.css}.pre-0057479`
  and restart; the new table and column can stay.
- Agents flagged facts they could not check and a few bank answers that look
  wrong (a cone-section ellipse, transformer data in kW). Their marking points
  say which reading they took. Worth a human pass per subject over time.
