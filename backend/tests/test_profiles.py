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
