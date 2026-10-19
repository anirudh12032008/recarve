#!/usr/bin/env python3
"""Serve recarve with the tutor's model replaced by a canned teacher.

    python backend/dev/fake_tutor.py --library /path/to/library serve --port 8000

Same server, same database, same screen; only tutor.model_stream is swapped,
so the Tutor tab can be clicked through end to end (check cards, the mastery
meter, Hinglish, past-paper cards, the read-only view) without an API key
and without a cent of spend. The teacher always talks about cell theory
whatever the topic; it is there to exercise the screen, not to teach. Every
argument after the script name goes to notes.py unchanged.
"""
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402
import tutor  # noqa: E402

MCQ = ('<check id="c%d" kind="mcq">\n{"q": "Which statement is part of the cell theory?", '
       '"options": ["Cells arise only from pre-existing cells", "Viruses are cells", '
       '"Only plants are made of cells", "Mitochondria are cells"], "answer": 0, '
       '"why": "Virchow added *omnis cellula e cellula* in 1855: every cell comes from a cell."}\n</check>\n')
SHORT = ('<check id="c%d" kind="short">\n{"q": "In two lines, state the three postulates of the cell theory.", "marks": 2}\n</check>\n')

def turn(system, messages):
    hing = "Hinglish" in system[3]["text"]
    used = [int(m) for s in messages if s["role"] == "assistant"
            for m in re.findall(r'<check id="c(\d+)"', s["content"])]
    n = max(used or [0]) + 1
    last = messages[-1]["content"]
    low = last.lower()
    if low.startswith('for "') or "quiz me" in low or "quiz" in low[:40]:
        body = ("Theek hai, " if hing else "Good. ") + ("yeh wala try karo." if hing else "Try this one.") + "\n\n" + MCQ % n
        state = '{"level": 2, "note": "answering checks", "done": false}'
    elif len(messages) > 1 and messages[-2]["role"] == "assistant" and "kind=\"short\"" in messages[-2]["content"]:
        body = ("**Right**, all three are there" if hing is False else "**Sahi**, teeno points aa gaye")
        body += ": every organism is made of cells, the cell is the basic unit of life, and cells come from cells.\n\nA 2-mark answer needs exactly those three lines, with **Schleiden, Schwann and Virchow** named.\n\n" + MCQ % n
        state = '{"level": 3, "importance": "medium", "why": "estimate: a 2-3 mark definition in most papers", "note": "states postulates correctly", "profile": "learns well from short checks", "next": "Cell organelles: structure and function", "done": false}'
    elif "deeper" in low or "example" in low or "simpler" in low or "summar" in low or "next topic" in low or "pyq" in low:
        body = ("Chalo, " if hing else "Sure. ") + "one step more.\n\nThe cell theory has three postulates. The third one, from **Virchow** (1855), is the one students forget: $\\text{omnis cellula e cellula}$.\n\n" + SHORT % n
        state = '{"level": 2, "done": false}'
    else:
        intro = ("Dekho, cell theory ka matlab simple hai: " if hing
                 else "Let us start from the one idea everything else rests on: ")
        body = (intro + "**every living thing is built from cells**.\n\n"
                "Three names, three dates:\n\n1. **Schleiden** (1838): all plants are made of cells.\n"
                "2. **Schwann** (1839): all animals too, so all organisms.\n"
                "3. **Virchow** (1855): cells arise only from pre-existing cells.\n\n"
                "A cell is the smallest unit that is alive on its own. Water inside it is about "
                "$70\\%$ by mass, and the surface-to-volume ratio $\\dfrac{A}{V} = \\dfrac{3}{r}$ "
                "for a sphere is why cells stay small.\n\n"
                "$$\\text{organism} \\rightarrow \\text{organ} \\rightarrow \\text{tissue} \\rightarrow \\text{cell}$$\n\n"
                "**Exam craft**: this is a 2 to 3 mark definition question; probably important (my estimate; "
                "no past papers on this have been extracted yet).\n\n" + MCQ % n)
        state = '{"level": 1, "importance": "medium", "why": "estimate: 2-3 mark definition in most papers", "note": "first lesson", "profile": "new; find out how they learn", "next": "Cell organelles: structure and function", "done": false}'
    ids = re.findall(r"- id ([0-9a-f-]{36}) \|", system[2]["text"])
    if ids and "quiz" not in low:
        body += "\n\nThis exact thing was asked in a past paper; look at how the marks are split:\n\n<pyq id=\"" + ids[0] + "\"/>\n"
    return body + "\n<state>" + state + "</state>"

def fake_stream(system, messages, model, effort):
    text = turn(system, messages)
    for i in range(0, len(text), 37):
        yield text[i:i + 37]
        time.sleep(0.03)
    return {"input_tokens": 9000, "output_tokens": 600, "cache_read_tokens": 7000,
            "cache_write_tokens": 2000, "stop_reason": "end_turn", "model": model}

tutor.model_stream = fake_stream
if __name__ == "__main__":
    sys.argv = ["notes.py"] + sys.argv[1:]
    notes.main()
