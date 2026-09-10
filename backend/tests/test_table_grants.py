"""Every table a member touches must say so out loud, in its own migration.

This guard exists because production and the test database disagree about
grants, and the test database is the more forgiving of the two.

backend/dev/migrate.py applies `alter default privileges ... grant` BEFORE it
runs the migrations, so anything created afterwards is reachable by the
`authenticated` role whether or not its migration said to grant it. Production
has no such default: a table arrives there with no privileges at all.

So a migration that writes policies and forgets grants passes the whole suite
and then tells the first student who touches the feature "permission denied for
table doubts" -- which is exactly what happened to 0025_doubts.sql, in
production, minutes after it was deployed. Policies decide who may do what;
grants decide whether the role may reach the table at all. Both are needed, and
only one of them was being checked.

Reading the migration text rather than the database is the whole point. Asking
recarve_test would always answer yes, because the default privileges already
granted it -- the test would pass forever and catch nothing.
"""

import re
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"

# `create table foo (`, tolerating `if not exists`.
CREATE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)", re.I)
# `alter table foo enable row level security`
RLS = re.compile(
    r"alter\s+table\s+([a-z_][a-z0-9_]*)\s+enable\s+row\s+level\s+security", re.I)
# `grant select, insert on foo to authenticated` -- one statement may name
# several tables, and the role list may hold more than one role.
GRANT = re.compile(
    r"grant\s+[^;]*?\s+on\s+((?:[a-z_][a-z0-9_]*\s*,\s*)*[a-z_][a-z0-9_]*)"
    r"\s+to\s+([^;]+);", re.I)


def sql():
    """Every migration's text, oldest first -- the order production applies."""
    return "\n".join(p.read_text(errors="replace")
                     for p in sorted(MIGRATIONS.glob("*.sql")))


def granted_tables(text):
    out = set()
    for names, roles in GRANT.findall(text):
        if "authenticated" not in roles.lower():
            continue
        for n in names.split(","):
            out.add(n.strip().lower())
    return out


def test_every_table_a_member_reads_is_granted_to_authenticated():
    """A table with row level security on it is a table members reach.

    RLS is only ever switched on for tables the app serves to a member -- so
    "has RLS" is exactly the set that needs a grant, and a table without it is
    either reference data or something only the owner touches.
    """
    text = sql()
    created = {m.lower() for m in CREATE.findall(text)}
    secured = {m.lower() for m in RLS.findall(text)}
    granted = granted_tables(text)

    # Sanity: this test is worthless if the patterns match nothing.
    assert created, "no create table found -- the pattern has drifted"
    assert secured, "no RLS found -- the pattern has drifted"

    missing = sorted(t for t in secured if t not in granted)
    assert not missing, (
        "these tables have row level security but no grant to authenticated, so "
        "every member gets 'permission denied for table ...' in production even "
        "though the test database allows it: " + ", ".join(missing))


def test_the_guard_notices_a_migration_that_forgets_its_grant():
    """The check above passes today; prove it can fail.

    Without this, a pattern that silently stopped matching would leave the
    guard green forever -- the exact failure it was written to stop.
    """
    forgot = """
      create table widgets (id uuid primary key);
      alter table widgets enable row level security;
      create policy "members read widgets" on widgets for select using (true);
    """
    secured = {m.lower() for m in RLS.findall(forgot)}
    assert secured == {"widgets"}
    assert not granted_tables(forgot), "nothing was granted here"
    assert secured - granted_tables(forgot) == {"widgets"}, \
        "a table with policies and no grant must be caught"


@pytest.mark.parametrize("table", ["doubts", "attendance", "academic_calendar"])
def test_the_newest_tables_state_their_grants(table):
    """Named rather than left to the sweep above, because these three are the
    ones this rule was learned on: doubts shipped without a grant, and the two
    beside it were written the same week.
    """
    assert table in granted_tables(sql()), f"{table} must grant to authenticated"
