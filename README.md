# recarve

notes for a class that keeps missing class. one person records the lecture, everyone wakes up to written notes — in hindi, english, or both in the same sentence, because that's how the lecture was.

live at [notes.workwithani.tech](https://notes.workwithani.tech) for the 2026-27 first year.

## what it does

- record a lecture, get notes. transcription then an LLM writes it up — summary, key points, a hindi/english terms table, and practice questions with answers
- **your week** — the institute timetable per section, with labs split by batch
- **attendance** — tracks the 75%-per-subject rule and tells you how many you can still miss
- **the board** — notices from your class rep
- **the wall** — lost something, found something, or ask. confessions too
- **doubts** — ask your section. if nobody's awake, an AI answers
- **campus** — clubs, fests, and where things actually are
- **past papers** — previous years' question papers, sorted

## how it works

one python file. `notes.py`, ~14k lines, and that's the whole thing — CLI, HTTP server, and the entire UI as HTML/CSS/JS inside python strings. no build step, no framework, no node_modules. you run it and it serves.

```
audio -> whisper -> transcript -> LLM -> markdown notes -> disk
                                              |
                                          postgres (who uploaded it, how the class voted)
```

the security boundary is postgres, not python. every table has row-level security and every request runs as the person making it, so "section A cannot read section B" is enforced by the database and not by me remembering to write an if-statement. 14 tables are scoped that way. 973 tests.

## signing in

your institute email is the door. google says it verified the address, the address is in the institute's workspace domain, and the eleven digits before the @ are your scholar number — that gets looked up in the registrar's published list, and your section is the registrar's answer instead of something you typed.

## tech stack

- python, stdlib `http.server` (yes, really)
- postgres + row-level security
- whisper for transcription
- groq for the notes and the doubt answers
- google oauth
- azure vm + cloudflare tunnel

## running locally

needs postgres and python 3.12+.

```bash
git clone <repo-url>
cd recarve
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

createdb recarve_test
python backend/dev/migrate.py          # rebuilds the schema from scratch

export GROQ_API_KEY=your_key_here
python notes.py serve --port 8000
```

open http://localhost:8000. the first person to join an empty database becomes its admin, because otherwise nobody could ever approve anybody.

running the tests:

```bash
python -m pytest backend/tests -q
```

## stuff that broke

- **parallel agents ate two migrations.** i had a few claude sessions running in worktrees and they silently undid each other's SQL. `git stash` turned out to be shared across worktrees, which is how it happened. now i diff branch pairs and rebuild a fresh database before trusting anything
- **100 job polls opened 100 TCP connections and 101 postgres sessions.** keep-alive, a proper timeout, a bigger backlog and a 10s cache on the auth lookup took that to 1 and 0
- **the scheme calls it Environmental Science, students call it EVS.** one subject, two curriculums, and the timetable PDF uses a third name. most of the schema complexity is this problem
- **labs run in two batches** at different hours, so one grid per section can't represent a section's own afternoon. both slots go in and the student deletes the one that isn't theirs
- **the section list arrived a year late.** people had already joined by invite and typed their roll number as `I60` when the institute writes `26I060`. both name the same seat, and a door comparing them as strings hands someone a second account and orphans the first

## limitations

- one institute, one intake year. the timetable and the section list are typed off the institute's own PDFs
- transcription quality depends on where you put your phone
- the notes are LLM-written. they're good, they're not a substitute for having been there

## ai disclosure

ai was used to reduce the repetitive work — boilerplate, test scaffolding, debugging, and a lot of SQL that was more tedious than hard. the conceptual work and the direction are mine: the schema design, the decision to make postgres the security boundary rather than the app, what the product should even be. the parts that took the longest were the ones ai couldn't help with — reading the institute's timetable PDF and working out what it actually meant, and getting the section/curriculum model right after it was wrong twice. i made sure this isn't ai slop but something my class actually opens every morning.

the app itself also uses ai — groq writes the notes and answers doubts.

## made by

anirudh sahu, a first-year who kept falling asleep in class.
[linkedin](https://www.linkedin.com/in/anirudh-sahu-4b245327b/) · [instagram](https://www.instagram.com/anirudh_sahu_12/)
