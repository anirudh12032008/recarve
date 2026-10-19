# recarve

notes for a class that keeps missing class. one person records the lecture, everyone wakes up to written notes hindi, english, or both in the same sentence, because that's how the lecture was.


## what it does
- record a lecture, get notes back. summary, key points
- your week, from the institute timetable
- a notice board, a wall, and doubts your section can answer 

## the tutor
a teacher in the app for the lecture nobody recorded. open the Tutor tab, pick a subject, pick a topic from the syllabus, and it teaches it: small steps, one question at a time, it grades what you answer and re-teaches what you got wrong, and it says out loud what the mini/mid/end term cares about. past-paper questions, once extracted, are the evidence; before that it says "estimate" and means it.

- one person teaches with it (whoever holds `RECARVE_TUTOR_ROLE`, default admin); the people the section trusts can read every lesson, read-only
- `claude-opus-5-5` by default, streamed, with the spend shown on the screen. set `RECARVE_TUTOR_MODEL` / `RECARVE_TUTOR_EFFORT` to change it
- `tutor/packs/<code>.json` is what it teaches from first: per topic, the gist, formulas, traps and practice questions, written by hand and by agents, checked in and corrected like code
- past papers become text with `python notes.py tutor pyq extract` (the API reads the PDFs in `library/archive/`), then `... pyq answer`, then they show up in the lessons and in the PYQ bank. or run the `pyq-extract` Claude Code skill on the machine that has the PDFs; same table either way

design notes: `docs/specs/2026-10-19-ai-tutor-design.md`

## how it works

audio → whisper → transcript → groq → markdown on disk. postgres holds who uploaded what.

the security boundary is postgres, not python. every table has row-level security and every request runs as the person making it

## screenshots
<img width="1710" height="1027" alt="Screenshot 2026-09-16 at 8 52 11 AM" src="https://github.com/user-attachments/assets/a17aef63-8394-4604-b815-fc934447ba7d" />


## stuff that broke
- ran a few claude sessions in parallel and they silently undid each other's migrations
- 100 job polls were opening 100 TCP connections and 101 postgres sessions
- people joined by invite and typed different roll number and comparing them as strings hands someone a second account

## run it
needs postgres and python 3.12+

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
createdb recarve_test && python backend/dev/migrate.py
export GROQ_API_KEY=your_key_here
export ANTHROPIC_API_KEY=your_key_here   # notes from uploads, and the tutor
python notes.py serve --port 8000
```

first person to join an empty database becomes admin, otherwise nobody could ever approve anybody.

## ai usage
ai was used to reduce the repetitive tasks and do the boring work boilerplate, the conceptual work and the directions were mine making postgres the security boundary instead of the app what the thing should even be the parts that took longest were the ones ai couldn't help with mostly working with manual labor and working out what it actually meant i made sure this isn't AI slop but an actually useful tool any class opens every morning.


## disclaimer
the notes are LLM written they're good they're not a substitute for having been there.
