# recarve

notes for a class that keeps missing class. one person records the lecture, everyone wakes up to written notes hindi, english, or both in the same sentence, because that's how the lecture was.


## what it does
- record a lecture, get notes back. summary, key points
- your week, from the institute timetable
- a notice board, a wall, and doubts your section can answer 

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
python notes.py serve --port 8000
```

first person to join an empty database becomes admin, otherwise nobody could ever approve anybody.

## ai usage
ai was used to reduce the repetitive tasks and do the boring work boilerplate, the conceptual work and the directions were mine making postgres the security boundary instead of the app what the thing should even be the parts that took longest were the ones ai couldn't help with mostly working with manual labor and working out what it actually meant i made sure this isn't AI slop but an actually useful tool any class opens every morning.


## disclaimer
the notes are LLM written they're good they're not a substitute for having been there.
