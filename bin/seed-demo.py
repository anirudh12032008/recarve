#!/usr/bin/env python3
"""Fill a throwaway database with invented people so the demo is not empty.

Only ever pointed at a fresh RECARVE_DB_URL. Every name, roll number and phone
number below is made up; nothing here touches the real install. Run once, after
the migrations, before the demo instance takes its first visitor:

    RECARVE_DB_URL=postgresql:///recarve_demo ./.venv/bin/python bin/seed-demo.py

Re-running is a no-op: the first invented student's roll number is the marker.
"""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notes  # noqa: E402

# (name, roll, phone, role). The first one joins an empty database and is
# therefore crowned admin by crown_if_first -- which is what mints the invite
# codes and lets the notice board have an author.
PEOPLE = [
    ("Asha Nair",       "26I001", "9000000001", "admin"),
    ("Rohit Verma",     "26I002", "9000000002", "cr"),
    ("Meera Iyer",      "26I003", "9000000003", "trusted"),
    ("Kabir Das",       "26I004", "9000000004", "trusted"),
    ("Sana Qureshi",    "26I005", "9000000005", "student"),
    ("Dev Pillai",      "26I006", "9000000006", "student"),
    ("Tara Bhatt",      "26I007", "9000000007", "student"),
    ("Imran Sheikh",    "26I008", "9000000008", "student"),
]

NOTICES = [
    ("Chemistry lab moved to Thursday",
     "The Tuesday batch shifts to Thursday 2pm this week only. Same lab, same "
     "groups. Bring the observation book you were told to bring last time."),
    ("Maths tutorial sheet 3 is up",
     "Covers the whole of last week. The professor said sheet 3 questions are "
     "the ones that come back in the sessional."),
]

WALL = [
    "Does anyone have the EVS notes from the 8th? I was in the med room.",
    "Found a grey water bottle outside LT-2, tell me if it is yours.",
    "Reminder that the maths sessional is closer than it feels.",
    "Whoever recorded today's chemistry class, thank you, genuinely.",
]


# The institute's own timetable and campus rows, which are what make the week
# grid and the Campus tab look like anything at all. seed_roll_list.sql is
# deliberately NOT here: it is 1054 real students, and this database is open.
SEEDS = ("seed_timetables.sql", "seed_campus.sql")
ROOT = Path(__file__).resolve().parent.parent


def seed_sql(conn):
    """Apply the dev data seeds, with the institute's name taken out of them."""
    for name in SEEDS:
        sql = (ROOT / "backend" / "dev" / name).read_text()
        sql = sql.replace("MANIT Bhopal", "the institute").replace("MANIT", "the institute")
        conn.execute(sql)
        print(f"applied {name}")


def main():
    with notes.db() as conn:
        if conn.execute("select 1 from profiles where roll_no = %s",
                        (PEOPLE[0][1],)).fetchone():
            print("already seeded; nothing to do")
            return

        seed_sql(conn)

        made = []
        for name, roll, phone, role in PEOPLE:
            code = notes.db_bootstrap(conn) or notes.db_invite(conn)
            if not code:
                # Past the first joiner, an admin's section has a live code;
                # mint one directly if the section was handed out already.
                section = conn.execute(
                    "select id from sections order by name limit 1").fetchone()[0]
                code = secrets.token_hex(4)
                conn.execute(
                    "insert into invites (code, section_id, expires_at) values "
                    "(%s, %s, now() + interval '30 days')", (code, str(section)))
            got = notes.db_join(conn, code, name, roll, phone,
                                secrets.token_urlsafe(16))
            if not got:
                raise SystemExit(f"could not seed {name}: the invite was refused")
            conn.execute(
                "update profiles set status = 'approved', role = %s where id = %s",
                (role, got[0]))
            made.append((name, got[0], role))
            print(f"seeded {name} ({role})")

        board = next(i for n, i, r in made if r in ("admin", "cr"))
        for title, body in NOTICES:
            with notes.db(board) as c:
                c.execute(
                    "insert into announcements (author_id, title, body) "
                    "values (%s, %s, %s)", (board, title, body))
        print(f"seeded {len(NOTICES)} notices")

        for (name, pid, _), body in zip(made[2:], WALL):
            with notes.db(pid) as c:
                c.execute("insert into posts (kind, author_id, body) "
                          "values ('feed', %s, %s)", (pid, body))
        print(f"seeded {min(len(WALL), len(made) - 2)} wall posts")

    print("done. Start the server with RECARVE_DEMO=1.")


if __name__ == "__main__":
    main()
