import psycopg
import pytest

import notes

from conftest import as_user, as_admin_connection, make_user


def test_both_curriculums_are_seeded(db):
    """Twelve for Group-ST (0001) and twelve more for Group-MT (0047). Two of
    the twenty-four -- MC1101 and NC1151 -- are taught to both, which is why
    the sum is 24 and not 26."""
    assert db.execute("select count(*) from subjects").fetchone()[0] == 24


def test_subject_codes_match_notes_py(db):
    codes = {r[0] for r in db.execute("select code from subjects").fetchall()}
    assert codes == {
        "MC1101", "CY1107", "EE1108", "ME1109", "CY1110", "BS1111",
        "HS1112", "EE1125", "CY1126", "ME1127", "SA1143", "NC1151",
        "PY1102", "CE1103", "ME1104", "CS1105", "HS1106", "CE1121",
        "PY1122", "ME1123", "CS1124", "HS1128", "SA1141", "SA1142",
    }
    assert codes == set(notes.SUBJECTS), \
        "notes.py names the folders these codes are filed under; a code in one "\
        "and not the other has either no folder or no row"


def test_profile_defaults_to_pending_and_untrusted(db):
    uid = make_user(db)
    db.execute("insert into profiles (id, name) values (%s, 'Test Student')", (uid,))
    row = db.execute(
        "select status, trusted, is_admin from profiles where id = %s", (uid,)
    ).fetchone()
    assert row == ("pending", False, False)


def test_status_is_constrained(db):
    uid = make_user(db)
    try:
        db.execute(
            "insert into profiles (id, name, status) values (%s, 'X', 'superuser')", (uid,)
        )
    except Exception as e:
        assert "check" in str(e).lower()
    else:
        raise AssertionError("an invalid status was accepted")


def approved(db, admin=False):
    uid = make_user(db)
    db.execute(
        "insert into profiles (id, name, status, role) "
        "values (%s, 'A', 'approved', %s)", (uid, "admin" if admin else "student"),
    )
    return uid


def pending(db):
    uid = make_user(db)
    db.execute("insert into profiles (id, name) values (%s, 'P')", (uid,))
    return uid


def test_pending_user_sees_only_their_own_profile(db):
    me = pending(db)
    approved(db)
    as_user(db, me)
    rows = db.execute("select id from profiles").fetchall()
    assert [str(r[0]) for r in rows] == [me]


def test_approved_user_sees_the_whole_class(db):
    me = approved(db)
    approved(db)
    as_user(db, me)
    assert db.execute("select count(*) from profiles").fetchone()[0] >= 2


def test_nobody_can_promote_themselves_to_admin(db):
    """role is the one place privilege is written, so this is the whole attack:
    every earlier version of it -- is_admin, trusted -- is now a generated
    column that refuses any write at all."""
    me = approved(db)
    as_user(db, me)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("update profiles set role = 'admin' where id = %s", (me,))
    as_admin_connection(db)
    assert db.execute(
        "select role, is_admin from profiles where id = %s", (me,)
    ).fetchone() == ("student", False)


def test_nobody_can_approve_themselves(db):
    me = pending(db)
    as_user(db, me)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("update profiles set status = 'approved' where id = %s", (me,))
    as_admin_connection(db)
    assert db.execute(
        "select status from profiles where id = %s", (me,)
    ).fetchone()[0] == "pending"


def test_the_year_is_read_off_the_front_of_the_scholar_number():
    """A B.Tech year runs July to June, so January does not promote anybody."""
    import datetime
    sep26, jan27 = datetime.date(2026, 9, 14), datetime.date(2027, 1, 20)
    assert notes.year_of_study("26113011101", sep26) == 1
    assert notes.year_of_study("26113011101", jan27) == 1, "January is still first year"
    assert notes.year_of_study("26113011101", datetime.date(2027, 7, 1)) == 2
    assert notes.year_of_study("23113011101", sep26) == 4
    # Not a guess, and never a zeroth year.
    assert notes.year_of_study(None, sep26) is None
    assert notes.year_of_study("", sep26) is None
    assert notes.year_of_study("ab113011101", sep26) is None
    assert notes.year_of_study("99113011101", sep26) == 1, "an intake not yet arrived"


def test_the_profile_carries_the_branch_the_registrar_printed(db):
    """Branch and year come off roll_list, matched on the roll number folded,
    and a student the list has never heard of still gets a profile."""
    as_admin_connection(db)
    listed, stranger = make_user(db), make_user(db)
    db.execute("insert into profiles (id, name, roll_no, status) values"
               " (%s, 'Listed', ' 26a099 ', 'approved'),"
               " (%s, 'Stranger', '26Z001', 'approved')", (listed, stranger))
    db.execute("insert into roll_list (scholar_no, roll_no, name, section_id, branch)"
               " select '26113011199', '26A099', 'Listed', id,"
               "        'Electrical Engineering'"
               "   from sections where name = 'I' and grad_year = 2030")

    as_user(db, listed)
    mine = notes.db_profile(db, listed)
    assert mine["branch"] == "Electrical Engineering", "folded roll number still matches"
    assert mine["scholar_no"] == "26113011199"
    assert mine["year"] == notes.year_of_study("26113011199")

    as_user(db, stranger)
    theirs = notes.db_profile(db, stranger)
    assert theirs["name"] == "Stranger", "not on the list is not an error"
    assert theirs["branch"] is None and theirs["year"] is None


def test_the_policy_and_roll_variants_agree_on_a_seat(db):
    """One rule about what spells a seat, said in two languages.

    roll_variants() decides it in Python for the missing list and the Google
    door; same_seat() decides it in SQL for the policy on roll_list. They have
    to agree, and the day one of them learns a new spelling the other has to.
    """
    as_admin_connection(db)
    for spellings in [("26I060", "I060", "I60"), ("26A001", "A001", "A1"),
                      ("26J110", "J110", "J110")]:
        seats = {db.execute("select same_seat(%s)", (s,)).fetchone()[0]
                 for s in spellings}
        assert len(seats) == 1, f"SQL split one seat across {seats}: {spellings}"
        # And the function the rest of the app uses reaches the same set.
        full = spellings[0]
        assert set(notes.roll_variants(full)) >= set(spellings), \
            f"roll_variants({full}) missed one of {spellings}"


def test_one_student_cannot_read_another_students_line_of_the_roll_list(db):
    """The policy names exactly one row: yours."""
    as_admin_connection(db)
    me, them = make_user(db), make_user(db)
    db.execute("insert into profiles (id, name, roll_no, status) values"
               " (%s, 'Me', '26A097', 'approved'), (%s, 'Them', '26A098', 'approved')",
               (me, them))
    db.execute("insert into roll_list (scholar_no, roll_no, name, section_id, branch)"
               " select v.s, v.r, v.n, s.id, 'Civil Engineering'"
               "   from (values ('26111011197','26A097','Me'),"
               "                ('26111011198','26A098','Them')) as v(s,r,n),"
               "        sections s where s.name = 'I' and s.grad_year = 2030")
    as_user(db, me)
    seen = [r[0] for r in db.execute("select roll_no from roll_list").fetchall()]
    assert seen == ['26A097'], f"a member reached somebody else's line: {seen}"


def test_the_admin_who_typed_a_short_roll_number_still_gets_his_branch(db):
    """The case the whole roll_variants business exists for, from this end.

    This install's admin joined as I60 a year before anybody had the
    registrar's file, which writes him 26I060. String equality gives him a
    profile with no branch and no year, and lists him as a student who never
    turned up.
    """
    as_admin_connection(db)
    uid = make_user(db)
    db.execute("insert into profiles (id, name, roll_no, status) "
               "values (%s, 'Old Hand', 'I60', 'approved')", (uid,))
    sid = db.execute("insert into roll_list (scholar_no, roll_no, name, section_id, branch)"
                     " select '26116011160', '26I060', 'Old Hand', id,"
                     "        'Electronics & Communications Engineering'"
                     "   from sections where name = 'I' and grad_year = 2030"
                     " returning section_id").fetchone()[0]

    as_user(db, uid)
    mine = notes.db_profile(db, uid)
    assert mine["branch"] == "Electronics & Communications Engineering", \
        "I60 and 26I060 are one seat"
    assert mine["scholar_no"] == "26116011160" and mine["year"] == 1

    as_admin_connection(db)
    gone = [m["roll_no"] for m in notes.db_section(db, sid)["missing"]]
    assert "26I060" not in gone, "he is sitting in the members list above"
