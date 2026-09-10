"""Function-level EXECUTE grants.

These sit below RLS: if a role is refused here, no policy is ever consulted.
They get their own file because the failure is invisible from the RLS tests --
a blanket `grant all on all routines` after a migration silently re-grants
every function the migration deliberately revoked, and every policy test
still passes. That is exactly what happened to claim_lecture() once.
"""

import pytest
from conftest import as_user, as_admin_connection, make_user
from test_content import member

# Functions the worker owns. A logged-in student must never reach these.
SERVICE_ONLY = ["claim_lecture()"]


@pytest.mark.parametrize("call", SERVICE_ONLY)
def test_members_cannot_execute_worker_functions(db, call):
    me = member(db)
    as_user(db, me)
    with pytest.raises(Exception) as exc:
        db.execute(f"select * from {call}")
    assert "permission denied" in str(exc.value).lower(), (
        f"{call} is callable by an ordinary member: {exc.value}"
    )


def test_service_role_can_execute_claim_lecture(db):
    """The revoke must not be so broad that the worker loses access too."""
    as_admin_connection(db)
    uploader = member(db)
    db.execute(
        "insert into lectures (subject_code, uploader_id, audio_key) "
        "values ('CY1107', %s, 'grant-check')", (uploader,),
    )
    db.execute("select set_config('role', 'service_role', true)")
    assert db.execute("select count(*) from claim_lecture()").fetchone()[0] == 1


def test_signup_rpc_is_reachable_by_a_brand_new_user(db):
    """join_with_invite is the one doorway in, so it must stay granted."""
    db.execute(
        "insert into invites (code, expires_at) values ('GRANT-CHECK', now() + interval '1 day')"
    )
    uid = make_user(db)
    as_user(db, uid)
    assert db.execute(
        "select join_with_invite('GRANT-CHECK', 'New Student', 'ROLL-1', null, 'a-real-password')"
    ).fetchone()[0] is True


def test_only_the_password_taking_join_function_exists(db):
    """`create or replace` with a new argument leaves the old arity standing
    beside the new one, and here the old arity is the one that inserts a
    profile with no password at all -- the claimable account 0013 exists to
    abolish -- still granted to every authenticated role. 0010 already had this
    happen once, which is why 0013 carries an explicit drop. The next migration
    that replaces this function will reintroduce it silently."""
    rows = db.execute(
        "select pg_get_function_identity_arguments(oid) from pg_proc "
        "where proname = 'join_with_invite'").fetchall()
    assert [r[0] for r in rows] == [
        "p_code text, p_name text, p_roll_no text, p_phone text, p_password text"]
