import psycopg
from conftest import DB_URL, as_admin_connection
from test_content import member


def queue_lecture(db, uploader, key):
    return db.execute(
        "insert into lectures (subject_code, uploader_id, audio_key) "
        "values ('CY1107', %s, %s) returning id", (uploader, key),
    ).fetchone()[0]


def test_claim_marks_transcribing_and_counts_attempt(db):
    me = member(db)
    lid = queue_lecture(db, me, "a1")
    row = db.execute(
        "select id, status, attempts, claimed_at from claim_lecture()"
    ).fetchone()
    assert row[0] == lid and row[1] == "transcribing" and row[2] == 1
    # the stamp the stale-reclaim path below reads; unstamped == stranded forever
    assert row[3] is not None


def test_empty_queue_returns_nothing(db):
    assert db.execute("select id from claim_lecture()").fetchall() == []


def test_stale_claim_is_reclaimed(db):
    me = member(db)
    lid = queue_lecture(db, me, "a2")
    db.execute(
        "update lectures set status = 'transcribing', "
        "claimed_at = now() - interval '3 hours' where id = %s", (lid,),
    )
    assert db.execute("select id from claim_lecture()").fetchone()[0] == lid


def test_fresh_claim_is_not_stolen(db):
    me = member(db)
    lid = queue_lecture(db, me, "a3")
    db.execute(
        "update lectures set status = 'transcribing', claimed_at = now() where id = %s",
        (lid,),
    )
    assert db.execute("select id from claim_lecture()").fetchall() == []


def test_two_workers_never_get_the_same_lecture(db):
    """The real concurrency case: two connections claim simultaneously."""
    as_admin_connection(db)
    me = member(db)
    queue_lecture(db, me, "concurrent")
    db.commit()  # make it visible to the other connections

    a = psycopg.connect(DB_URL)
    b = psycopg.connect(DB_URL)
    a.autocommit = False
    b.autocommit = False
    try:
        got_a = a.execute("select id from claim_lecture()").fetchall()
        got_b = b.execute("select id from claim_lecture()").fetchall()
        a.commit()
        b.commit()
        assert len(got_a) + len(got_b) == 1, f"double-claimed: {got_a} {got_b}"
    finally:
        a.close()
        b.close()
        cleanup = psycopg.connect(DB_URL)
        cleanup.execute("delete from lectures where audio_key = 'concurrent'")
        cleanup.execute("delete from profiles where id = %s", (me,))
        cleanup.execute("delete from auth.users where id = %s", (me,))
        cleanup.commit()
        cleanup.close()
