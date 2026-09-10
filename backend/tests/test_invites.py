import psycopg
import pytest
from conftest import as_user, as_admin_connection, make_user


def seed_invite(db, code="SEC-I-2026", uses=0, max_uses=200, days=30):
    db.execute(
        "insert into invites (code, expires_at, max_uses, uses) "
        "values (%s, now() + (%s || ' days')::interval, %s, %s)",
        (code, str(days), max_uses, uses),
    )
    return code


def test_valid_code_creates_a_pending_profile(db):
    code = seed_invite(db)
    uid = make_user(db)
    as_user(db, uid)
    assert db.execute(
        "select join_with_invite(%s, 'Anirudh', '2026I001', null, 'a-real-password')", (code,)
    ).fetchone()[0] is True
    as_admin_connection(db)
    assert db.execute(
        "select name, status from profiles where id = %s", (uid,)
    ).fetchone() == ("Anirudh", "pending")


def test_wrong_code_is_rejected(db):
    seed_invite(db)
    uid = make_user(db)
    as_user(db, uid)
    assert db.execute(
        "select join_with_invite('NOPE', 'X', '1', null, 'a-real-password')"
    ).fetchone()[0] is False
    as_admin_connection(db)
    assert db.execute(
        "select count(*) from profiles where id = %s", (uid,)
    ).fetchone()[0] == 0


def test_expired_code_is_rejected(db):
    code = seed_invite(db, code="OLD", days=-1)
    uid = make_user(db)
    as_user(db, uid)
    assert db.execute(
        "select join_with_invite(%s, 'X', '2', null, 'a-real-password')", (code,)
    ).fetchone()[0] is False


def test_exhausted_code_is_rejected(db):
    code = seed_invite(db, code="FULL", uses=200, max_uses=200)
    uid = make_user(db)
    as_user(db, uid)
    assert db.execute(
        "select join_with_invite(%s, 'X', '3', null, 'a-real-password')", (code,)
    ).fetchone()[0] is False


def test_successful_join_increments_uses(db):
    code = seed_invite(db, code="COUNT")
    uid = make_user(db)
    as_user(db, uid)
    db.execute("select join_with_invite(%s, 'X', '4', null, 'a-real-password')", (code,))
    as_admin_connection(db)
    assert db.execute("select uses from invites where code = %s", (code,)).fetchone()[0] == 1


def test_invite_table_is_not_readable_by_members(db):
    """Codes must not be enumerable, or anyone could invite the whole college."""
    seed_invite(db)
    uid = make_user(db)
    db.execute(
        "insert into profiles (id, name, status) values (%s, 'A', 'approved')", (uid,)
    )
    as_user(db, uid)
    assert db.execute("select count(*) from invites").fetchone()[0] == 0


def test_the_function_itself_refuses_an_account_with_no_password(db):
    """The handler's check_password shadows this on the only path the app uses,
    which is exactly why the database needs its own: the guard exists to catch
    a caller that never went through the handler, and nothing else proves it
    would. A blank password must also not cost a use of the invite."""
    code = seed_invite(db, code="NOPW")
    uid = make_user(db)
    as_user(db, uid)
    # A savepoint, not a rollback: as_user's settings are transaction-local, so
    # throwing the whole transaction away between attempts would leave
    # auth.uid() null and the next two calls would be refused by the wrong
    # guard -- the test would pass with the password check deleted.
    for blank in (None, "", "   "):
        db.execute("savepoint attempt")
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute("select join_with_invite(%s, 'X', '9', null, %s)", (code, blank))
        db.execute("rollback to savepoint attempt")
    as_admin_connection(db)
    assert db.execute(
        "select uses from invites where code = %s", (code,)).fetchone()[0] == 0
