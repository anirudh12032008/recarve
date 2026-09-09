import pytest
from conftest import as_user, as_admin_connection, make_user
from test_content import member

READS = ["profiles", "subjects", "lectures", "materials", "votes"]


@pytest.mark.parametrize("table", READS)
def test_pending_user_reads_no_class_content(db, table):
    as_admin_connection(db)
    owner = member(db)
    mid = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'x.pdf', 'pm1', 10) returning id", (owner,),
    ).fetchone()[0]
    db.execute(
        "insert into lectures (subject_code, uploader_id, audio_key) "
        "values ('CY1107', %s, 'pm-a1')", (owner,),
    )
    db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, owner))
    outsider = make_user(db)
    db.execute("insert into profiles (id, name) values (%s, 'Out')", (outsider,))
    # Without a row to hide, "reads 0 rows" passes even with RLS switched off.
    assert db.execute(f"select count(*) from {table}").fetchone()[0] > 0, (
        f"setup left {table} empty, so the assertion below would prove nothing"
    )
    as_user(db, outsider)
    n = db.execute(f"select count(*) from {table}").fetchone()[0]
    expected = 1 if table == "profiles" else 0  # they see only their own profile
    assert n == expected, f"a pending user read {n} rows from {table}"


def test_blocked_user_loses_access(db):
    me = member(db)
    db.execute(
        "insert into lectures (subject_code, uploader_id, audio_key) "
        "values ('CY1107', %s, 'bl-a1')", (me,),
    )
    as_user(db, me)
    assert db.execute("select count(*) from lectures").fetchone()[0] == 1
    as_admin_connection(db)
    db.execute("update profiles set status = 'blocked' where id = %s", (me,))
    as_user(db, me)
    assert db.execute("select count(*) from lectures").fetchone()[0] == 0


def test_every_public_table_has_rls_enabled(db):
    rows = db.execute(
        "select t.tablename from pg_tables t "
        "where t.schemaname = 'public' and not exists ("
        "  select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "  where c.relname = t.tablename and n.nspname = 'public' and c.relrowsecurity)"
    ).fetchall()
    assert rows == [], f"tables without RLS: {[r[0] for r in rows]}"
