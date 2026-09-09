from conftest import as_user, as_admin_connection, make_user
from test_content import member


def test_score_counts_uploads_recordings_and_votes(db):
    me = member(db)
    voter = member(db)
    as_user(db, me)
    mid = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'a.pdf', 'ka', 10) returning id", (me,),
    ).fetchone()[0]
    db.execute(
        "insert into lectures (subject_code, uploader_id, audio_key, status) "
        "values ('CY1107', %s, 'audio1', 'done')", (me,),
    )
    as_user(db, voter)
    db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, voter))

    as_user(db, me)
    row = db.execute(
        "select uploads, recordings, votes_received, score from points where id = %s", (me,)
    ).fetchone()
    # 1 upload (5) + 1 recording (10) + 1 vote (1)
    assert row == (1, 1, 1, 16)
    # The vote belongs to the person whose material was voted on, not to the
    # voter and not to the class -- an uncorrelated count would score this 1.
    assert db.execute(
        "select uploads, recordings, votes_received, score from points where id = %s", (voter,)
    ).fetchone() == (0, 0, 0, 0)


def test_queued_lectures_do_not_score(db):
    me = member(db)
    as_user(db, me)
    db.execute(
        "insert into lectures (subject_code, uploader_id, audio_key) "
        "values ('CY1107', %s, 'pending-audio')", (me,),
    )
    assert db.execute("select recordings from points where id = %s", (me,)).fetchone()[0] == 0


def test_view_respects_rls(db):
    """security_invoker: a pending user must not read the class's scores."""
    member(db)
    as_admin_connection(db)
    outsider = make_user(db)
    db.execute("insert into profiles (id, name) values (%s, 'Outsider')", (outsider,))
    as_user(db, outsider)
    rows = db.execute("select id from points").fetchall()
    assert [str(r[0]) for r in rows] == [outsider], "a pending user saw other people's points"


def test_votes_are_not_multiplied_by_recordings(db):
    """One vote is one point however many lectures the uploader has recorded."""
    me = member(db)
    voter = member(db)
    as_user(db, me)
    mid = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'a.pdf', 'ka', 10) returning id", (me,),
    ).fetchone()[0]
    db.execute(
        "insert into lectures (subject_code, uploader_id, audio_key, status) "
        "values ('CY1107', %s, 'audio1', 'done'), ('CY1107', %s, 'audio2', 'done')",
        (me, me),
    )
    as_user(db, voter)
    db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, voter))

    as_user(db, me)
    row = db.execute(
        "select uploads, recordings, votes_received, score from points where id = %s", (me,)
    ).fetchone()
    # 1 upload (5) + 2 recordings (20) + 1 vote (1)
    assert row == (1, 2, 1, 26)
