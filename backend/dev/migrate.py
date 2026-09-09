#!/usr/bin/env python3
"""Rebuild the local test database from scratch.

    python backend/dev/migrate.py

Drops the public schema, applies the dev auth shim, then every file in
supabase/migrations/ in filename order. Idempotent by construction: the
database is always rebuilt, never patched, so a migration can never
half-apply and leave a state no fresh checkout would reproduce.

Only supabase/migrations/ ships to production. The shim is dev-only.
"""

import pathlib
import sys

import psycopg

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHIM = ROOT / "backend" / "dev" / "auth_shim.sql"
MIGRATIONS = ROOT / "supabase" / "migrations"
DB_URL = "postgresql:///recarve_test"


def main():
    files = [SHIM]
    if MIGRATIONS.exists():
        files += sorted(MIGRATIONS.glob("*.sql"))

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("drop schema if exists public cascade")
        conn.execute("drop schema if exists auth cascade")
        conn.execute("create schema public")

        # Supabase grants these by default on every object it creates; plain
        # Postgres does not, and without them RLS is never even consulted --
        # the role is refused at the privilege layer first, which looks like a
        # policy bug and is not one.
        #
        # These MUST be default privileges applied BEFORE the migrations, not
        # blanket grants after them. A `grant all on all routines` afterwards
        # silently re-grants every function a migration deliberately revoked --
        # which is exactly how claim_lecture(), meant for the worker alone,
        # became callable by any logged-in student.
        for kind in ("tables", "sequences", "routines"):
            conn.execute(
                f"alter default privileges in schema public "
                f"grant all on {kind} to authenticated, service_role"
            )

        for f in files:
            try:
                conn.execute(f.read_text())
            except Exception as e:
                print(f"FAILED in {f.name}:\n  {e}", file=sys.stderr)
                return 1
            print(f"  applied {f.name}", file=sys.stderr)

    print(f"{len(files)} file(s) applied to recarve_test", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
