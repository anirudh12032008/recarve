import psycopg
import pytest

import notes
from conftest import as_admin_connection, as_user
from test_content import member


def test_saving_and_taking_back(db):
    """On, then off. A bookmark is a toggle, never a row that changes."""
    uid = member(db, role="student")
    as_user(db, uid)
    assert notes.db_bookmarks(db, uid) == []

    notes.db_set_bookmark(db, uid, "MC1101", "Limits", True)
    saved = notes.db_bookmarks(db, uid)
    assert saved == [{"code": "MC1101", "title": "Limits"}]

    # Saving twice is not two rows -- the primary key already refuses that,
    # and the function is written to let it rather than to hide the conflict.
    notes.db_set_bookmark(db, uid, "MC1101", "Limits", True)
    assert notes.db_bookmarks(db, uid) == saved

    notes.db_set_bookmark(db, uid, "MC1101", "Limits", False)
    assert notes.db_bookmarks(db, uid) == []


def test_one_student_never_sees_another(db):
    """Private in the way a mark of attendance is private, not a fact about
    the class -- so the policy is 'for all', not just select."""
    a, b = member(db, role="student"), member(db, role="student")

    as_user(db, a)
    notes.db_set_bookmark(db, a, "MC1101", "Limits", True)

    as_user(db, b)
    assert notes.db_bookmarks(db, b) == [], "one student's saves are not another's"
    # Two different refusals, and the difference is the point. DELETE is
    # granted, so the `using` clause filters: b's delete matches zero of a's
    # rows rather than being refused outright -- worth asserting, because a
    # policy that silently touches nothing looks the same as one that is only
    # ever exercised on rows it happens to own.
    assert db.execute(
        "delete from bookmarks where profile_id = %s", (a,)).rowcount == 0
    # UPDATE is not granted at all (0032 grants select, insert, delete), so it
    # never reaches a policy. That is the stronger guarantee of the two: a
    # title cannot be rewritten by anybody, including its owner.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction():
            db.execute("update bookmarks set title = 'taken' where profile_id = %s",
                       (a,))

    as_admin_connection(db)
    still = db.execute(
        "select title from bookmarks where profile_id = %s", (a,)).fetchall()
    assert still == [("Limits",)], "b's attempts changed nothing of a's"


def test_a_bookmark_needs_a_real_subject_and_a_title(db):
    """The subject foreign key is the backstop; the function's own checks are
    what turn a bad request into a readable error instead of a 500."""
    uid = member(db, role="student")
    as_user(db, uid)
    with pytest.raises(ValueError):
        notes.db_set_bookmark(db, uid, "NOPE9999", "Anything", True)
    with pytest.raises(ValueError):
        notes.db_set_bookmark(db, uid, "MC1101", "  ", True)


def test_only_approval_is_required_not_a_role(db):
    """Any approved member may save a note for themselves -- this is not an
    upload, not a recording, not the API budget. Even a plain student."""
    uid = member(db, role="student")
    as_user(db, uid)
    notes.db_set_bookmark(db, uid, "MC1101", "Limits", True)
    assert notes.db_bookmarks(db, uid) == [{"code": "MC1101", "title": "Limits"}]
