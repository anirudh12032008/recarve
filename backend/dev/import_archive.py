#!/usr/bin/env python3
"""Shelve the seniors' portal into archive_documents.

    python backend/dev/import_archive.py ~/Downloads

Reads the per-subject zips pulled out of the NITBFreshers portal -- one per
subject, named `MT__Engineering-Graphics.zip`, whose members keep the portal's
own folder names (`Notes/Vector Calculus.pdf`). Those folder names are the
whole point of keeping them: they are the only record of what each file IS,
and this script's job is to turn 31 of them into four columns.

Re-runnable. The unique index on (subject_code, file_key) means a second pass
over the same zips inserts nothing, which matters because the portal shelves
the same End Term under two folders that mean the same thing.
"""

import os
import pathlib
import re
import sys
import zipfile

import psycopg

DB_URL = os.environ.get("RECARVE_DB_URL", "postgresql:///recarve_test")
ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = "nitbfreshers portal"

# Their subject names to ours, checked against the institute scheme page
# rather than guessed from the folder name.
#
# "Mathematics 2" is deliberately absent. It is a second-semester course and
# recarve's SUBJECTS is first-semester only, so there is no code to file it
# under. Inventing MC1201 would put a course in the database that may not
# exist under that number, and subject_code is a foreign key precisely so that
# cannot happen quietly. Those files are reported as parked, not dropped.
SUBJECTS = {
    "Mathematics 1": "MC1101",
    "Physics Theory": "PY1102",
    "Engineering Mechanics": "CE1103",
    "Engineering Graphics": "ME1104",
    "Computer Programming": "CS1105",
    "Communication Skills": "HS1106",
    "Engineering Chemistry": "CY1107",
    "Basic Electrical and Electronics Engg": "EE1108",
    "Manufacturing Sciences": "ME1109",
    "Environmental Sciences": "CY1110",
    "Biology for Engineers": "BS1111",
    "Life Skill Management": "SA1141",
}


def squash(s):
    """Compare subject names without their punctuation, which the download
    filename sanitiser already ate."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def classify(folder, filename):
    """One folder name and one filename in; kind, exam, year, section out.

    The folder says what a file is; the filename usually says it again, and
    more precisely. "2024-2025 PYQs/End Term 2024-25.pdf" has a folder that
    only knows the file is a paper and a name that knows which sitting -- so
    the filename is consulted first for `exam`, and the folder is the fallback
    rather than the authority.
    """
    f, n = folder.lower(), filename.lower()
    both = f + " " + n

    # kind, most specific first: "Physics Lab Records" is a lab record before
    # it is a record, and "Books and PPTs" is a shelf of books.
    if "book" in f:
        kind = "book"
    elif "syllabus" in f or "scheme" in f:
        kind = "syllabus"
    elif "lab" in f or "laborator" in f or "record" in f:
        kind = "lab"
    elif "assignment" in f:
        kind = "assignment"
    elif "note" in f:
        # Before slides, because "Notes and Slides" leads with Notes and holds
        # mostly notes. "Faculty Shared PPTs" and "Detailed Slides" have no
        # "note" in them and fall through to the next branch as they should.
        kind = "notes"
    elif "ppt" in f or "slide" in f:
        kind = "slides"
    elif "pyq" in f or "paper" in f or "question" in f or "test" in f:
        kind = "paper"
    else:
        kind = "paper" if re.search(r"\b(end|mid|mini)\b", n) else "notes"

    # exam, filename first. "mini" is tested before "mid" because a careless
    # prefix match on "mi" would find both in "Mini Test".
    exam = None
    if kind == "paper":
        for word in ("mini", "end", "mid"):
            if re.search(rf"\b{word}\b", n) or re.search(rf"\b{word}\b", f):
                exam = word
                break

    # year: the opening year of an academic year. "2025-26", "2025-2026" and
    # "2024-25" all mean the year the first number names. A lone "2023" means
    # 2023. Anything outside the window is a version number that happened to
    # look like a date.
    year = None
    m = re.search(r"\b(20\d{2})\s*[-/]\s*(?:20)?\d{2}\b", both)
    if m:
        year = int(m.group(1))
    else:
        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", both)
                 if 2000 <= int(y) <= 2100]
        if years:
            year = min(years)

    # section: "Section B", "Sec B", "Sec J Set 1" -- and "SecB", which is how
    # Chemistry's "MaterialsNotes SecB 2024.pdf" spells it, so the space is
    # optional. The trailing word boundary keeps "Sec B" from matching the B
    # in "BEEE", and a Set number is not a section: Maths 1 has "Sec J Set 1"
    # and "Sec J Set 2" and both are section J.
    section = None
    m = re.search(r"\bsec(?:tion)?\.?\s*([A-J])\b", filename, re.I)
    if m:
        section = m.group(1).upper()

    return kind, exam, year, section


def main(src):
    zips = sorted(pathlib.Path(src).glob("[MS]T__*.zip"))
    if not zips:
        sys.exit(f"no MT__/ST__ zips in {src}")

    by_squashed = {squash(name): code for name, code in SUBJECTS.items()}
    dest_root = ROOT / "library" / ".archive"
    rows, parked, skipped = [], [], []

    for z in zips:
        subject = z.stem.split("__", 1)[1].replace("-", " ")
        code = by_squashed.get(squash(subject))
        with zipfile.ZipFile(z) as zf:
            members = [m for m in zf.namelist() if not m.endswith("/")]
            if code is None:
                parked.append((subject, len(members)))
                continue
            for m in members:
                folder, _, filename = m.rpartition("/")
                kind, exam, year, section = classify(folder, filename)
                data = zf.read(m)
                if len(data) < 5000:
                    skipped.append((subject, m, len(data)))
                    continue
                key = f"{code}/{folder}/{filename}"
                out = dest_root / code / folder / filename
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
                rows.append((code, kind, exam, year, section,
                             pathlib.Path(filename).stem, key, len(data), SOURCE))

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        before = conn.execute("select count(*) from archive_documents").fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany(
                "insert into archive_documents "
                "(subject_code, kind, exam, year, set_for_section, title, "
                " file_key, size_bytes, source) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "on conflict (subject_code, file_key) do nothing",
                rows,
            )
        after = conn.execute("select count(*) from archive_documents").fetchone()[0]

    print(f"{len(zips)} zip(s), {len(rows)} file(s) staged")
    print(f"shelved {after - before} new row(s); "
          f"{len(rows) - (after - before)} already present")
    if skipped:
        print(f"skipped {len(skipped)} file(s) under 5KB "
              f"(challenge pages, not documents)")
    for subject, n in parked:
        print(f"PARKED {subject}: {n} file(s) -- no course code in SUBJECTS")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Downloads"))
