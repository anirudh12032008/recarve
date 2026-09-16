"""The demo door, which exists only while RECARVE_DEMO=1 is set.

Temporary: this whole door is a throwaway for one programme submission and
comes out with the branch. What it has to be true about while it is here is
the two things that would embarrass the demo -- that a visitor lands in the
section the seeded people are actually in, and that a second visitor is the
same person as the first rather than a new row in the class list.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import as_admin_connection  # noqa: E402


def seed_a_populated_section(db, section_name, how_many=3):
    """Put `how_many` people into one named section and return its id."""
    section = db.execute("select id from sections where name = %s",
                         (section_name,)).fetchone()[0]
    for n in range(how_many):
        db.execute(
            "insert into invites (code, section_id, expires_at) values "
            "(%s, %s, now() + interval '1 day')", (f"SEED{n}", str(section)))
        notes.db_join(db, f"SEED{n}", f"Seeded {n}", f"26X{n:03d}", None, "a-password")
    return str(section)


def test_the_visitor_lands_where_the_people_are(db):
    db.execute("delete from profiles")
    busy = seed_a_populated_section(db, "E")  # not the alphabetically first
    as_admin_connection(db)

    profile_id = notes.db_demo_session(db)

    landed = db.execute("select section_id from profiles where id = %s",
                        (profile_id,)).fetchone()[0]
    assert str(landed) == busy, "the demo dropped its one visitor into an empty section"


def test_a_second_visitor_is_the_same_visitor(db):
    db.execute("delete from profiles")
    seed_a_populated_section(db, "E")
    as_admin_connection(db)

    first = notes.db_demo_session(db)
    second = notes.db_demo_session(db)

    assert first == second, "every refresh would add a ghost to the class list"
    count = db.execute("select count(*) from profiles where roll_no = %s",
                       (notes.DEMO_ROLL,)).fetchone()[0]
    assert count == 1


def test_the_visitor_can_actually_read_the_app(db):
    db.execute("delete from profiles")
    seed_a_populated_section(db, "E")
    as_admin_connection(db)

    profile_id = notes.db_demo_session(db)

    status, role = db.execute("select status, role from profiles where id = %s",
                              (profile_id,)).fetchone()
    # pending shows an empty app, and a seeded database has no admin sitting
    # there to approve anybody.
    assert status == "approved"
    assert role == "trusted", "the AI routes are the thing worth demonstrating"
