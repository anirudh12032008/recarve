import os
import uuid

import psycopg
import pytest

DB_URL = os.environ.get("RECARVE_DB_URL", "postgresql:///recarve_test")


@pytest.fixture
def db():
    """A connection that rolls back after each test, so tests never see each other."""
    conn = psycopg.connect(DB_URL)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


def as_user(conn, user_id, role="authenticated"):
    """Run the rest of this transaction as `user_id`, so RLS policies apply.

    Supabase policies read auth.uid() out of the request.jwt.claims setting,
    which is exactly what PostgREST populates for a real request. Setting the
    same GUC here means a policy behaves identically in tests and production.

    Both settings are transaction-local, so the fixture's rollback undoes them.
    """
    conn.execute(
        "select set_config('request.jwt.claims', %s, true)",
        ('{"sub": "%s", "role": "%s"}' % (user_id, role),),
    )
    conn.execute("select set_config('role', %s, true)", (role,))


def as_admin_connection(conn):
    """Drop back to the owning superuser, bypassing RLS -- for test setup only.

    Use this between acting as two different people, or to assert on rows the
    current user is not allowed to see.
    """
    conn.execute("select set_config('role', 'none', true)")
    conn.execute("select set_config('request.jwt.claims', '', true)")


def make_user(conn, email=None):
    """Create an auth.users row and return its id.

    profiles.id is a foreign key onto auth.users, so every test person needs
    one of these before they can have a profile.
    """
    uid = str(uuid.uuid4())
    conn.execute(
        "insert into auth.users (id, instance_id, aud, role, email) "
        "values (%s, '00000000-0000-0000-0000-000000000000', 'authenticated', "
        "'authenticated', %s)",
        (uid, email or f"{uid}@test.local"),
    )
    return uid
