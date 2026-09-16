# recarve

notes for a class that keeps missing class. one person records the lecture, everyone wakes up to written notes — hindi, english, or both in the same sentence, because that's how the lecture was.

live at [notes.workwithani.tech](https://notes.workwithani.tech)

## what it does
- record a lecture, get notes back. summary, key points, a hindi/english terms table and practice questions
- your week, from the institute timetable
- attendance, with the 75%-per-subject rule worked out for you
- a notice board, a wall, and doubts your section can answer (or an AI if nobody's awake)
- clubs, fests, past papers

## how it works
one python file. `notes.py`, ~14k lines — CLI, server, and the whole UI as HTML/CSS/JS inside python strings. no build step, no framework.

audio → whisper → transcript → groq → markdown on disk. postgres holds who uploaded what.

the security boundary is postgres, not python. every table has row-level security and every request runs as the person making it, so "section A can't read section B" is the database's job and not mine. 970 tests.

your institute email is the door — the digits before the @ are your scholar number, which gets looked up in the registrar's list, so your section is never something you typed.

## stuff that broke
- ran a few claude sessions in parallel and they silently undid each other's migrations. turns out `git stash` is shared across worktrees
- 100 job polls were opening 100 TCP connections and 101 postgres sessions. keep-alive and a 10s cache on the auth lookup took it to 1 and 0
- the scheme calls it Environmental Science, students call it EVS, the timetable PDF uses a third name. most of the schema complexity is this
- people joined by invite and typed `I60` when the institute writes `26I060`. same seat, and comparing them as strings hands someone a second account

## run it
needs postgres and python 3.12+

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
createdb recarve_test && python backend/dev/migrate.py
export GROQ_API_KEY=your_key_here
python notes.py serve --port 8000
```

first person to join an empty database becomes admin, otherwise nobody could ever approve anybody.

## ai usage
ai was used to reduce the repetitive tasks and do the boring work — boilerplate, test scaffolding, debugging, a lot of tedious SQL. the conceptual work and the directions were mine: the schema, making postgres the security boundary instead of the app, what the thing should even be. the parts that took longest were the ones ai couldn't help with, mostly reading the institute's timetable PDF and working out what it actually meant. i made sure this isn't AI slop but an actually useful tool my class opens every morning.

the app itself uses ai too — groq writes the notes and answers doubts.

## disclaimer
the notes are LLM-written. they're good, they're not a substitute for having been there.

---
made by anirudh sahu, a first-year who kept falling asleep in class.
[linkedin](https://www.linkedin.com/in/anirudh-sahu-4b245327b/) · [instagram](https://www.instagram.com/anirudh_sahu_12/)
