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
   (default `admin`), `RECARVE_TUTOR_MODEL` (default `claude-opus-5-5`),
   `RECARVE_TUTOR_EFFORT` (default `medium`).

## Running locally

```bash
source .venv/bin/activate && pip install -r requirements.txt
createdb recarve_test 2>/dev/null; python backend/dev/migrate.py
python -m pytest backend/tests -q -p no:cacheprovider
set -a; . ./.env; set +a
python notes.py serve --port 8000       # then open the Tutor tab
```

## Decisions worth knowing

- Writes under `/tutor` need `RECARVE_TUTOR_ROLE` (admin by default);
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
