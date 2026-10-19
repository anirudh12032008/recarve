#!/usr/bin/env python3
"""Check one curriculum file against the syllabus and the rules in
docs/specs/2026-10-19-tutor-practice.md.

    .venv/bin/python backend/dev/check_course.py tutor/course/CY1107/4.json

Prints every problem, then a count of the unit's past-paper questions that no
subtopic placed. Exit 1 on any problem, so a file the screen cannot use is
never committed. Placing every PYQ is reported, not enforced: a question the
bank put under this unit by mistake should be left out, and said so.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "dev"))
from course_pyqs import unit_pyqs  # noqa: E402

KINDS = {"mcq", "short", "long", "numerical"}
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
SMILES = re.compile(r"^[A-Za-z0-9@+\-\[\]()=#$/\\%.:*>~]+$")


def words(s):
    return len((s or "").split())


def check(path):
    path = pathlib.Path(path)
    d = json.loads(path.read_text())
    code, unit = d.get("code"), d.get("unit")
    syl = json.loads((ROOT / "syllabus.json").read_text()).get(code) or []
    n = int(path.stem)
    p = []
    if n < 1 or n > len(syl):
        return [f"no unit {n} in syllabus for {code}"], 0
    want = syl[n - 1]
    if unit != want["unit"]:
        p.append(f"unit is {unit!r}, syllabus unit {n} is {want['unit']!r}")
    got = [t.get("topic") for t in d.get("topics") or []]
    if got != want["topics"]:
        p.append(f"topics must be exactly, in order: {want['topics']}")
    pyqs = {r["qkey"] for r in unit_pyqs(code, want["unit"])}
    placed = set()
    for t in d.get("topics") or []:
        where = t.get("topic")
        subs = t.get("subtopics") or []
        if not 2 <= len(subs) <= 5:
            p.append(f"{where}: {len(subs)} subtopics, want 2-5")
        sids, qids = set(), set()
        for s in subs:
            at = f"{where} / {s.get('id')}"
            if not ID.match(str(s.get("id") or "")) or s["id"] in sids:
                p.append(f"{at}: id must be unique lowercase-with-hyphens")
            sids.add(s.get("id"))
            if not s.get("title"):
                p.append(f"{at}: no title")
            if not 15 <= words(s.get("theory_md")) <= 170:
                p.append(f"{at}: theory_md is {words(s.get('theory_md'))} words, want up to 150")
            if s.get("basics_md") and words(s["basics_md"]) > 120:
                p.append(f"{at}: basics_md is {words(s['basics_md'])} words, want up to 100")
            kp = s.get("key_points") or []
            if not 2 <= len(kp) <= 6:
                p.append(f"{at}: {len(kp)} key_points, want 3-5")
            for f in s.get("figures") or []:
                sm = f.get("smiles")
                if not sm or not SMILES.match(sm) or not f.get("caption"):
                    p.append(f"{at}: figure needs a SMILES string and a caption: {f}")
            own = mcq = written = 0
            for q in s.get("questions") or []:
                qat = f"{at} / {q.get('id')}"
                if not ID.match(str(q.get("id") or "")) or q["id"] in qids:
                    p.append(f"{qat}: question id must be unique in the topic")
                qids.add(q.get("id"))
                if q.get("level") not in (1, 2, 3):
                    p.append(f"{qat}: level must be 1, 2 or 3")
                if q.get("pyq"):
                    if q["pyq"] not in pyqs:
                        p.append(f"{qat}: qkey {q['pyq']} is not a past-paper question of this unit")
                    placed.add(q["pyq"])
                    if not q.get("points"):
                        p.append(f"{qat}: a pyq question needs its marking points")
                    continue
                own += 1
                kind = q.get("kind")
                if kind not in KINDS:
                    p.append(f"{qat}: kind must be one of {sorted(KINDS)}")
                    continue
                if not q.get("q"):
                    p.append(f"{qat}: no question")
                if kind == "mcq":
                    mcq += 1
                    ops = q.get("options") or []
                    if len(ops) != 4 or not isinstance(q.get("answer_index"), int) \
                            or not 0 <= q["answer_index"] < 4 or not q.get("why"):
                        p.append(f"{qat}: an mcq needs 4 options, answer_index 0-3 and why")
                else:
                    written += 1
                    if not q.get("model_answer_md") or not q.get("points") or not q.get("marks"):
                        p.append(f"{qat}: a written question needs marks, model_answer_md and points")
            if own < 3 or mcq < 1 or written < 1:
                p.append(f"{at}: {own} own questions ({mcq} mcq, {written} written); "
                         "want 3-6 with at least one of each")
    blob = path.read_text()
    if "—" in blob:
        p.append(f"{blob.count(chr(0x2014))} em-dashes; use a colon, comma or full stop")
    return p, len(pyqs - placed)


def main():
    bad = False
    for f in sys.argv[1:]:
        problems, unplaced = check(f)
        print(f"{f}: " + ("OK" if not problems else f"{len(problems)} problems"))
        for x in problems:
            print("  -", x)
        print(f"  {unplaced} of the unit's past-paper questions not placed in any subtopic")
        bad = bad or bool(problems)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
