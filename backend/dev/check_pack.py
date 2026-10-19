#!/usr/bin/env python3
"""Check a teaching pack against the syllabus and the pack rules.

    python backend/dev/check_pack.py tutor/packs/BS1111.json

Unit and topic names must be the syllabus's own, in its order, because the
tutor joins on them; a pack with its own outline must say so. Every topic
needs six questions, two of them MCQs with four options and a valid answer
index. Exit 1 on the first rule broken, so a pack that cannot be used is
never quietly committed.
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPORTANCE = {"low", "medium", "high", "must"}
KINDS = {"short", "long", "numerical", "mcq"}


def check(path):
    d = json.loads(pathlib.Path(path).read_text())
    syl = json.loads((ROOT / "syllabus.json").read_text()).get(d["code"])
    problems = []
    if syl and not d.get("syllabus_estimated"):
        want = {u["unit"]: u["topics"] for u in syl}
        for u in d["units"]:
            if u["unit"] not in want:
                problems.append(f"unit not in syllabus: {u['unit']!r}")
                continue
            got = [t["topic"] for t in u["topics"]]
            if got != want[u["unit"]]:
                problems.append(f"{u['unit']}: topics differ: {sorted(set(got) ^ set(want[u['unit']]))}")
    elif not syl and not d.get("syllabus_estimated"):
        problems.append("no syllabus for this code and syllabus_estimated is not true")
    n = 0
    for u in d["units"]:
        for t in u["topics"]:
            n += 1
            where = f"{u['unit']} / {t['topic']}"
            if t.get("importance") not in IMPORTANCE:
                problems.append(f"{where}: importance {t.get('importance')!r}")
            for key in ("why", "gist", "key_points", "traps", "asked_as", "questions"):
                if not t.get(key):
                    problems.append(f"{where}: missing {key}")
            qs = t.get("questions") or []
            if len(qs) < 6:
                problems.append(f"{where}: {len(qs)} questions, need 6")
            mcq = 0
            for q in qs:
                if q.get("kind") not in KINDS:
                    problems.append(f"{where}: question kind {q.get('kind')!r}")
                if not q.get("q") or not q.get("a"):
                    problems.append(f"{where}: a question without q or a")
                if q.get("kind") == "mcq":
                    mcq += 1
                    opts = q.get("options") or []
                    if len(opts) != 4 or not isinstance(q.get("answer_index"), int) \
                            or not 0 <= q["answer_index"] < 4:
                        problems.append(f"{where}: bad mcq options/answer_index")
            if mcq < 2:
                problems.append(f"{where}: {mcq} mcq, need 2")
    size = pathlib.Path(path).stat().st_size // 1024
    print(f"{path}: {n} topics, {size} KB, {len(problems)} problem(s)")
    for p in problems[:40]:
        print("  " + p)
    return not problems


if __name__ == "__main__":
    sys.exit(0 if all(check(p) for p in sys.argv[1:]) else 1)
