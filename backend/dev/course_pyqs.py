#!/usr/bin/env python3
"""The past-paper questions of one unit, with the qkey a curriculum file
uses to place each one in a subtopic.

    .venv/bin/python backend/dev/course_pyqs.py CY1107 3

Reads the repo's export of the bank (tutor/pyq/<CODE>.json), so it needs no
database. One block per question: qkey, sitting, marks, the topic the
extractor gave it, and the question; the answer follows when --answers is
given. Questions set in several sittings are listed once with every sitting.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tutor  # noqa: E402


def unit_pyqs(code, unit):
    path = ROOT / "tutor" / "pyq" / f"{code}.json"
    items = json.loads(path.read_text())["items"] if path.exists() else []
    out = {}
    for it in items:
        if it.get("unit") != unit:
            continue
        k = tutor.qkey(code, it["question_md"])
        row = out.setdefault(k, {"qkey": k, "topic": it.get("topic"), "marks": it.get("marks"),
                                 "question_md": it["question_md"], "answer_md": it.get("answer_md"),
                                 "sittings": []})
        row["sittings"].append(f"{it.get('exam') or '?'} {it.get('year') or '?'}"
                               + (f" Q{it['qno']}" if it.get("qno") else ""))
    return list(out.values())


def main():
    code, n = sys.argv[1].upper(), int(sys.argv[2])
    units = json.loads((ROOT / "syllabus.json").read_text())[code]
    unit = units[n - 1]["unit"]
    rows = unit_pyqs(code, unit)
    print(f"# {code} unit {n}: {unit} -- {len(rows)} distinct past-paper questions\n")
    for r in rows:
        print(f"qkey {r['qkey']} | {', '.join(r['sittings'])} | {r['marks']} marks | topic: {r['topic']}")
        print(r["question_md"].strip())
        if "--answers" in sys.argv and r["answer_md"]:
            print("ANSWER:", r["answer_md"].strip())
        print()


if __name__ == "__main__":
    main()
