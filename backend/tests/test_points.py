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


# ---------------------------------------------------------------- the board
#
# standings is the same three counts as points, ranked, plus a rolling week.
# These tests clear the content tables first: the board is a ranking of the
# whole class, so a row left behind by another test is a rival nobody created.

import pathlib  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402


def clean(db):
    """An empty library, inside this test's transaction. Rolled back after."""
    as_admin_connection(db)
    for table in ("votes", "materials", "lectures"):
        db.execute(f"delete from {table}")


def upload(db, who, key, when=None, status="visible"):
    """One material, optionally backdated, on the table owner's connection.

    The status is set afterwards rather than in the insert because the BEFORE
    INSERT trigger forces a student's upload to 'pending' whatever the payload
    says -- which is right, and is exactly what makes a planted 'visible' row
    for a student need a second statement.
    """
    when = f", now() - interval '{when}'" if when else ""
    col = ", created_at" if when else ""
    mid = db.execute(
        f"insert into materials (subject_code, uploader_id, filename, file_key, "
        f"size_bytes{col}) values ('CY1107', %s, %s, %s, 10{when}) returning id",
        (who, key + ".pdf", key),
    ).fetchone()[0]
    db.execute("update materials set status = %s where id = %s", (status, mid))
    return mid


def record(db, who, key, when=None):
    when = f", now() - interval '{when}'" if when else ""
    col = ", recorded_at" if when else ""
    db.execute(
        f"insert into lectures (subject_code, uploader_id, audio_key, status{col}) "
        f"values ('CY1107', %s, %s, 'done'{when})", (who, key),
    )


def test_ties_share_a_place_and_the_next_one_skips(db):
    """1, 1, 3 -- equal work is equal, and the third person is third."""
    clean(db)
    as_admin_connection(db)
    top1, top2, third = (member(db) for _ in range(3))
    for who in (top1, top2):
        record(db, who, f"tie-{who}")          # 10 points each
    upload(db, third, "third-1")               # 5
    as_user(db, top1)
    board = notes.db_standings(db, top1)["all"]["top"]
    by = {r["id"]: r for r in board}
    assert by[str(top1)]["score"] == by[str(top2)]["score"] == 10
    assert by[str(top1)]["rank"] == by[str(top2)]["rank"] == 1, "a tie is one place"
    assert by[str(third)]["rank"] == 3, "and the place after a two-way tie is 3rd"
    # Ordered for display by rank then name, so a tie never flickers between paints.
    assert [r["rank"] for r in board] == sorted(r["rank"] for r in board)


def test_your_own_row_comes_back_even_from_the_bottom(db):
    """The point of `you`: a board of two, and the viewer is on neither."""
    clean(db)
    as_admin_connection(db)
    a, b = member(db), member(db)
    me = member(db, role="student")
    record(db, a, "l-a")
    upload(db, b, "m-b")
    as_user(db, me)
    got = notes.db_standings(db, me, top=2)
    for window in ("all", "week"):
        top = got[window]["top"]
        assert len(top) == 2 and all(not r["you"] for r in top)
        you = got[window]["you"]
        assert you and you["id"] == str(me), f"{window}: the viewer fell off the board"
        assert you["score"] == 0
    # And a top-of-the-board viewer is on the board itself, not pinned twice.
    as_user(db, a)
    mine = notes.db_standings(db, a)["all"]
    assert [r["you"] for r in mine["top"]] == [True, False]
    assert mine["you"]["id"] == str(a)


def test_day_one_is_an_empty_board_not_a_class_list(db):
    """A hundred and ten people with nothing yet is not a ranking."""
    clean(db)
    as_admin_connection(db)
    for _ in range(3):
        member(db)
    me = member(db)
    as_user(db, me)
    got = notes.db_standings(db, me)
    assert got["all"]["top"] == [], "nobody with nothing may take a place"
    assert got["week"]["top"] == []
    assert got["all"]["you"]["score"] == 0, "the viewer is still an answer"


def test_the_week_window_is_a_rolling_seven_days(db):
    """The boundary, from both sides, for all three kinds of point."""
    clean(db)
    as_admin_connection(db)
    me = member(db)
    voter = member(db)
    inside = upload(db, me, "fresh", when="6 days 23 hours")
    upload(db, me, "stale", when="7 days 1 hour")
    record(db, me, "fresh-audio", when="6 days 23 hours")
    record(db, me, "stale-audio", when="7 days 1 hour")
    db.execute("insert into votes (material_id, voter_id, created_at) "
               "values (%s, %s, now() - interval '6 days 23 hours')", (inside, voter))
    db.execute("insert into votes (material_id, voter_id, created_at) "
               "values (%s, %s, now() - interval '7 days 1 hour')",
               (upload(db, me, "old-voted", when="8 days"), voter))
    as_user(db, me)
    row = db.execute(
        "select uploads, recordings, votes_received, score, "
        "       week_uploads, week_recordings, week_votes, week_score "
        "from standings where id = %s", (me,)).fetchone()
    assert row[:4] == (3, 2, 2, 3 * 5 + 2 * 10 + 2), f"all-time counts everything: {row}"
    assert row[4:] == (1, 1, 1, 5 + 10 + 1), f"the week counts one of each: {row}"


def test_the_weights_are_the_ones_the_page_prints(db):
    """One set of numbers. The view computes them, the Me tab explains them, and
    a change to either that is not a change to both is this test failing."""
    clean(db)
    as_admin_connection(db)
    me = member(db)
    upload(db, me, "w1")
    record(db, me, "w-audio")
    db.execute("insert into votes (material_id, voter_id) values (%s, %s)",
               (upload(db, me, "w2"), member(db)))
    as_user(db, me)
    row = db.execute("select uploads, recordings, votes_received, score "
                     "from standings where id = %s", (me,)).fetchone()
    assert row == (2, 1, 1, 2 * 5 + 1 * 10 + 1)
    for printed in ("['uploads', 5,", "['recordings', 10,", "['votes_received', 1,"):
        assert printed in notes.PAGE, f"the Me tab stopped saying {printed}"


def test_a_score_never_decides_what_you_can_read(db):
    """The owner's explicit product decision, tested where it would actually be
    broken. Two students, one with points and one with none, must see exactly
    the same library -- and RLS, not the handler, is what says so.
    """
    clean(db)
    as_admin_connection(db)
    author = member(db)
    upload(db, author, "shared-1")
    record(db, author, "shared-audio")

    rich = member(db, role="student")
    poor = member(db, role="student")
    as_admin_connection(db)
    # Give one of them a real score without giving them a role: planted rows,
    # because a student cannot upload. Points, and nothing else, now differ.
    upload(db, rich, "rich-1")
    record(db, rich, "rich-audio")

    seen = {}
    for who in (rich, poor):
        as_user(db, who)
        seen[who] = (
            sorted(r[0] for r in db.execute("select file_key from materials")),
            sorted(r[0] for r in db.execute("select audio_key from lectures")),
        )
        assert db.execute("select count(*) from subjects").fetchone()[0] == 12

    as_admin_connection(db)
    scores = {w: db.execute("select score from points where id = %s", (w,)).fetchone()[0]
              for w in (rich, poor)}
    assert scores[rich] > 0 and scores[poor] == 0, f"the two must differ: {scores}"
    assert seen[rich] == seen[poor], "a score changed what somebody could read"
    assert "shared-1" in seen[poor][0] and "shared-audio" in seen[poor][1]


def test_the_board_is_not_readable_by_a_stranger(db):
    """security_invoker, on the new view as on the old one: a pending account
    must not get a ranked list of the class it has not been let into."""
    clean(db)
    member(db)
    member(db)
    as_admin_connection(db)
    outsider = make_user(db)
    db.execute("insert into profiles (id, name) values (%s, 'Outsider')", (outsider,))
    as_user(db, outsider)
    rows = db.execute("select id from standings").fetchall()
    assert [str(r[0]) for r in rows] == [outsider], "a pending user saw the class board"
