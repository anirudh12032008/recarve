"""Upvotes and contributions, against the real database.

Two halves, the same shape as test_auth.py. The first drives the policies
directly on a transaction that rolls back. The second starts notes.py's own
server on a real socket and votes over HTTP, because "one vote per person" is
only worth what the running server enforces -- and what enforces it is the
votes primary key, not a check in a handler.
"""

import json
import os
import pathlib
import sys
import threading
import urllib.request

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL, as_admin_connection, as_user, make_user  # noqa: E402
from test_auth import SECRET, call  # noqa: E402
from test_content import member  # noqa: E402


def material(db, owner, filename="a.pdf", code="CY1107"):
    return db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values (%s, %s, %s, %s, 10) returning id", (code, owner, filename, filename),
    ).fetchone()[0]


# --------------------------------------------------------- the policies


def test_a_member_can_upvote_someone_elses_notes(db):
    owner, voter = member(db), member(db)
    as_user(db, owner)
    mid = material(db, owner)

    as_user(db, voter)
    assert notes.db_vote(db, mid, voter, True) == {"votes": 1, "voted": True}


def test_the_same_person_cannot_vote_twice(db):
    """No handler decides this. The votes primary key does."""
    owner, voter = member(db), member(db)
    as_user(db, owner)
    mid = material(db, owner)

    as_user(db, voter)
    notes.db_vote(db, mid, voter, True)
    with pytest.raises(psycopg.errors.UniqueViolation):
        notes.db_vote(db, mid, voter, True)


def test_a_vote_can_be_taken_back_and_recast(db):
    owner, voter = member(db), member(db)
    as_user(db, owner)
    mid = material(db, owner)

    as_user(db, voter)
    notes.db_vote(db, mid, voter, True)
    assert notes.db_vote(db, mid, voter, False) == {"votes": 0, "voted": False}
    assert notes.db_vote(db, mid, voter, True) == {"votes": 1, "voted": True}


def test_the_count_is_one_per_person(db):
    owner = member(db)
    voters = [member(db) for _ in range(3)]
    as_user(db, owner)
    mid = material(db, owner)

    for n, v in enumerate(voters, 1):
        as_user(db, v)
        assert notes.db_vote(db, mid, v, True) == {"votes": n, "voted": True}

    as_user(db, owner)
    assert db.execute("select count(*) from votes where material_id = %s",
                      (mid,)).fetchone()[0] == 3


def test_you_cannot_vote_in_somebody_elses_name(db):
    """'vote as yourself': the row you insert has to be about you."""
    owner, voter, patsy = member(db), member(db), member(db)
    as_user(db, owner)
    mid = material(db, owner)

    as_user(db, voter)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, patsy))


def test_you_cannot_upvote_your_own_upload(db):
    """'vote as yourself' means somebody else's work.

    votes_received is a third of the score and the board is public, so a vote
    for yourself is a point you awarded yourself. The policy refuses it, not a
    check in the handler -- a leaked connection string gets the same answer.
    """
    owner = member(db)
    as_user(db, owner)
    mid = material(db, owner)

    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        notes.db_vote(db, mid, owner, True)
    assert db.execute("select count(*) from votes where material_id = %s",
                      (mid,)).fetchone()[0] == 0


def test_a_pending_joiner_cannot_vote(db):
    owner = member(db)
    as_user(db, owner)
    mid = material(db, owner)

    as_admin_connection(db)
    waiting = make_user(db)
    db.execute("insert into profiles (id, name) values (%s, 'Waiting')", (waiting,))
    as_user(db, waiting)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, waiting))


def test_an_unauthenticated_caller_cannot_vote(db):
    """No session at all: not a policy question, the role has no grant."""
    owner = member(db)
    as_user(db, owner)
    mid = material(db, owner)

    db.execute("select set_config('request.jwt.claims', '', true)")
    db.execute("select set_config('role', 'anon', true)")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, owner))


# ------------------------------------------- attribution and ranking


def test_meta_says_who_added_a_file_and_how_it_scored(db):
    owner, voter = member(db), member(db)
    as_user(db, owner)
    mid = material(db, owner, "unit1.pdf")
    db.execute(
        "insert into lectures (subject_code, uploader_id, title, audio_key, status) "
        "values ('CY1107', %s, 'CY1107-week1', 'k', 'done')", (owner,))

    as_user(db, voter)
    notes.db_vote(db, mid, voter, True)

    mats, lecs = notes.db_meta(db, voter)
    assert mats[("CY1107", "unit1.pdf")] == {
        "id": str(mid), "by": "M", "votes": 1, "voted": True}
    assert lecs[("CY1107", "CY1107-week1")] == "M"

    # The same rows read by someone who did not vote: same count, not voted.
    mats, _ = notes.db_meta(db, owner)
    assert mats[("CY1107", "unit1.pdf")]["votes"] == 1
    assert mats[("CY1107", "unit1.pdf")]["voted"] is False


def test_a_recording_is_credited_under_the_name_its_notes_will_take(db, tmp_path):
    """Two halves of one join. The row is titled after the file the transcriber
    will write -- dest.stem, not what the phone called the blob -- because that
    title is the only handle the library keeps on it. And the worker marking it
    done is what turns a recording into points."""
    me = member(db)
    as_user(db, me)
    dest = tmp_path / "inbox" / "CY1107-week1.m4a"
    dest.parent.mkdir()
    dest.write_bytes(b"audio")
    notes.db_record_upload(db, me, "CY1107", "week1.m4a", dest, True)
    assert notes.db_contributions(db, me)["points"]["recordings"] == 0, "queued is not done"

    notes.db_mark_transcribed(db, dest)          # the worker, once process() returns
    lectures = tmp_path / "CY1107-Engineering-Chemistry" / "lectures"
    lectures.mkdir(parents=True)
    (lectures / "CY1107-week1.md").write_text("## Summary\nmoles\n")

    subjects = notes.apply_meta(notes.build_data(tmp_path, tmp_path),
                                *notes.db_meta(db, me))
    cy = next(s for s in subjects if s["code"] == "CY1107")
    assert [(n["title"], n["by"]) for n in cy["notes"]] == [("CY1107-week1", "M")], \
        "the row and the note it became have to key the same way"
    assert notes.db_contributions(db, me)["points"] == {
        "uploads": 0, "recordings": 1, "votes_received": 0, "score": 10}


def test_backfill_adopts_each_file_once_however_often_the_server_restarts(db, tmp_path):
    """It runs on every boot. Without its guard a restart inserts every file
    again -- doubling the admin's score, and stranding votes on the duplicate
    row the page stops keying to."""
    member(db, admin=True)
    folder = tmp_path / "CY1107-Engineering-Chemistry"
    (folder / "lectures").mkdir(parents=True)
    (folder / "uploads").mkdir(parents=True)
    (folder / "lectures" / "CY1107-week1.md").write_text("## Summary\nmoles\n")
    (folder / "uploads" / "unit1.pdf").write_bytes(b"%PDF-1.4")

    assert notes.db_backfill(db, tmp_path) == 2, "one lecture and one upload"
    assert notes.db_backfill(db, tmp_path) == 0, "a restart adopts nothing twice"
    assert db.execute("select count(*) from materials where file_key = %s",
                      (str(folder / "uploads" / "unit1.pdf"),)).fetchone()[0] == 1


def test_uploads_are_ranked_by_votes_and_lectures_are_not():
    """Only uploaded notes compete. Lectures are a record of what happened, in
    the order it happened."""
    subjects = [{
        "code": "CY1107", "name": "Chemistry",
        "notes": [{"title": "week1"}, {"title": "week2"}],
        "uploads": [{"name": "quiet.pdf"}, {"name": "loud.pdf"}, {"name": "unknown.pdf"}],
    }]
    mats = {("CY1107", "quiet.pdf"): {"id": "1", "by": "Asha", "votes": 1, "voted": False},
            ("CY1107", "loud.pdf"): {"id": "2", "by": "Bilal", "votes": 9, "voted": True}}
    lecs = {("CY1107", "week1"): "Asha"}

    notes.apply_meta(subjects, mats, lecs)
    s = subjects[0]
    assert [u["name"] for u in s["uploads"]] == ["loud.pdf", "quiet.pdf", "unknown.pdf"]
    assert [n["title"] for n in s["notes"]] == ["week1", "week2"], "lectures keep date order"
    assert [n["by"] for n in s["notes"]] == ["Asha", None]
    # A file with no row behind it carries no id, so the page offers no vote
    # button for something the database has never heard of.
    assert "id" not in s["uploads"][2]


def test_contributions_count_uploads_recordings_and_votes_received(db):
    me, voter = member(db), member(db)
    as_user(db, me)
    mid = material(db, me, "mine.pdf")
    db.execute(
        "insert into lectures (subject_code, uploader_id, title, audio_key, status) "
        "values ('CY1107', %s, 'CY1107-week1', 'k', 'done')", (me,))
    as_user(db, voter)
    notes.db_vote(db, mid, voter, True)

    as_user(db, me)
    got = notes.db_contributions(db, me)
    assert got["points"] == {"uploads": 1, "recordings": 1, "votes_received": 1, "score": 16}
    assert got["uploads"] == [
        {"name": "mine.pdf", "subject": "CY1107", "votes": 1, "status": "visible"}]
    assert got["recordings"] == [
        {"title": "CY1107-week1", "subject": "CY1107", "status": "done"}]


# ---------------------------------------------------- the real server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """notes.py's own server over a real socket, with a class already in it.

    The admin and one member exist before the server starts, so startup has
    somebody to attribute the files already on disk to -- which is the backfill
    under test.
    """
    os.environ["RECARVE_SECRET"] = SECRET.decode()

    lib = tmp_path_factory.mktemp("library")
    folder = lib / "CY1107-Engineering-Chemistry"
    (folder / "lectures").mkdir(parents=True)
    (folder / "uploads").mkdir(parents=True)
    (folder / "lectures" / "CY1107-week1.md").write_text("## Summary\nmoles\n")
    (folder / "uploads" / "a-first.pdf").write_bytes(b"%PDF-1.4 one")
    (folder / "uploads" / "z-last.pdf").write_bytes(b"%PDF-1.4 two")

    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("votes", "materials", "lectures", "profiles"):
            conn.execute(f"delete from {table}")
        for name, admin in (("Asha", True), ("Bilal", False), ("Chan", False)):
            uid = make_user(conn)
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role, password) "
                "values (%s, %s, %s, 'approved', %s, 'a-real-password')",
                (uid, name, name, "admin" if admin else "trusted"))
            people[name] = uid

    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cookies = {n: notes.sign_session(i, SECRET) for n, i in people.items()}
    yield srv.server_address[1], cookies

    srv.shutdown()
    srv.server_close()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("votes", "materials", "lectures", "profiles", "auth.users"):
            conn.execute(f"delete from {table}")


def uploads_of(port, cookie, code="CY1107"):
    status, body, _ = call(port, "GET", "/data", cookie=cookie)
    assert status == 200, body
    return next(s for s in json.loads(body)["subjects"] if s["code"] == code)


def test_startup_adopts_the_files_that_were_already_there(server):
    """Nothing on disk predates the class as far as the page is concerned."""
    port, cookies = server
    s = uploads_of(port, cookies["Bilal"])
    assert [u["name"] for u in s["uploads"]] == ["a-first.pdf", "z-last.pdf"]
    assert all(u["by"] == "Asha" for u in s["uploads"]), "backfill credits the admin"
    assert all(u["votes"] == 0 and u["voted"] is False for u in s["uploads"])
    assert [(n["title"], n["by"]) for n in s["notes"]] == [("CY1107-week1", "Asha")]


def test_a_stranger_cannot_vote(server):
    """No cookie, no session: the gate refuses before any handler sees it."""
    port, cookies = server
    s = uploads_of(port, cookies["Bilal"])
    target = s["uploads"][0]["id"]
    assert call(port, "POST", "/vote", {"id": target})[0] == 403
    assert call(port, "POST", "/vote", {"id": target}, cookie="garbage")[0] == 403
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute("select count(*) from votes").fetchone()[0] == 0


def test_voting_once_counts_and_voting_again_is_refused(server):
    port, cookies = server
    s = uploads_of(port, cookies["Bilal"])
    target = next(u for u in s["uploads"] if u["name"] == "z-last.pdf")["id"]

    status, body, _ = call(port, "POST", "/vote", {"id": target}, cookie=cookies["Bilal"])
    assert (status, json.loads(body)) == (200, {"votes": 1, "voted": True})

    status, body, _ = call(port, "POST", "/vote", {"id": target}, cookie=cookies["Bilal"])
    assert status == 409, body
    assert "already voted" in json.loads(body)["error"]

    # And the count did not move on the second press.
    got = next(u for u in uploads_of(port, cookies["Bilal"])["uploads"]
               if u["name"] == "z-last.pdf")
    assert (got["votes"], got["voted"]) == (1, True)
    # Somebody who has not voted sees the count but not the pressed state.
    got = next(u for u in uploads_of(port, cookies["Chan"])["uploads"]
               if u["name"] == "z-last.pdf")
    assert (got["votes"], got["voted"]) == (1, False)


def test_the_server_refuses_a_vote_for_your_own_upload(server):
    """The 403, in words, over the socket. Asha owns everything the backfill
    adopted, so every file on this server is her own.

    The toggle loop closes with it: off-and-on re-dates a vote, which is how a
    single vote could keep somebody on the rolling seven-day board forever
    without adding anything. A classmate's vote is now the only one that can.
    """
    port, cookies = server
    target = next(u for u in uploads_of(port, cookies["Asha"])["uploads"]
                  if u["name"] == "z-last.pdf")["id"]
    before = json.loads(call(port, "GET", "/me", cookie=cookies["Asha"])[1])["points"]

    status, body, _ = call(port, "POST", "/vote", {"id": target}, cookie=cookies["Asha"])
    assert status == 403, body
    assert "your own" in json.loads(body)["error"], "not the duplicate-vote wording"
    assert json.loads(call(port, "GET", "/me", cookie=cookies["Asha"])[1])["points"] \
        == before, "a refused vote must not move the score"


def test_votes_lift_a_note_above_the_others(server):
    """The whole point of ranking: the set the class uses is the one on top."""
    port, cookies = server
    assert [u["name"] for u in uploads_of(port, cookies["Asha"])["uploads"]] \
        == ["z-last.pdf", "a-first.pdf"], "one vote already outranks none"

    target = next(u for u in uploads_of(port, cookies["Asha"])["uploads"]
                  if u["name"] == "a-first.pdf")["id"]
    for who in ("Bilal", "Chan"):
        assert call(port, "POST", "/vote", {"id": target}, cookie=cookies[who])[0] == 200
    s = uploads_of(port, cookies["Asha"])
    assert [(u["name"], u["votes"]) for u in s["uploads"]] \
        == [("a-first.pdf", 2), ("z-last.pdf", 1)]

    # Taking it back drops it again.
    assert call(port, "POST", "/vote", {"id": target, "on": False},
                cookie=cookies["Chan"])[0] == 200
    assert [u["name"] for u in uploads_of(port, cookies["Asha"])["uploads"]] \
        == ["a-first.pdf", "z-last.pdf"], "1-1 ties break by name"


def test_a_vote_for_nothing_is_not_a_500(server):
    port, cookies = server
    assert call(port, "POST", "/vote", {"id": ""}, cookie=cookies["Bilal"])[0] == 400
    assert call(port, "POST", "/vote", {"id": "not-a-uuid"}, cookie=cookies["Bilal"])[0] == 404
    assert call(port, "POST", "/vote", {"id": "11111111-1111-1111-1111-111111111111"},
                cookie=cookies["Bilal"])[0] == 404


def test_contributions_are_the_students_own_and_nobody_elses(server):
    port, cookies = server
    status, body, _ = call(port, "GET", "/me", cookie=cookies["Asha"])
    assert status == 200
    mine = json.loads(body)
    # The admin owns all three backfilled items and has had two votes cast on them.
    assert mine["points"]["uploads"] == 2
    assert mine["points"]["recordings"] == 1
    assert mine["points"]["votes_received"] == 2
    assert mine["points"]["score"] == 2 * 5 + 1 * 10 + 2
    assert {u["name"] for u in mine["uploads"]} == {"a-first.pdf", "z-last.pdf"}
    assert [r["title"] for r in mine["recordings"]] == ["CY1107-week1"]

    # Bilal cast a vote but added nothing, so his own page is empty and his
    # score is zero -- and he can still read every note in the library.
    theirs = json.loads(call(port, "GET", "/me", cookie=cookies["Bilal"])[1])
    assert theirs["points"]["score"] == 0
    assert theirs["uploads"] == [] and theirs["recordings"] == []
    assert call(port, "GET", "/", cookie=cookies["Bilal"])[0] == 200, \
        "a score of zero must never gate the library"
    assert uploads_of(port, cookies["Bilal"])["notes"], "nor hide a single note"


def test_an_upload_over_http_credits_the_person_who_sent_it(server):
    port, cookies = server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/upload", method="POST", data=b"%PDF-1.4 bilal",
        headers={"X-Filename": "bilal.pdf", "X-Subject": "CY1107",
                 "Cookie": f"{notes.SESSION_COOKIE}={cookies['Bilal']}"},
    )
    with urllib.request.urlopen(req) as r:
        assert r.status == 200

    got = next(u for u in uploads_of(port, cookies["Chan"])["uploads"]
               if u["name"] == "bilal.pdf")
    assert got["by"] == "Bilal" and got["votes"] == 0
    assert json.loads(call(port, "GET", "/me", cookie=cookies["Bilal"])[1])["uploads"] \
        == [{"name": "bilal.pdf", "subject": "CY1107", "votes": 0, "status": "visible"}]


def test_no_auth_offers_no_votes_at_all(tmp_path):
    """The single-user mode has no database and nobody to credit."""
    folder = tmp_path / "CY1107-Engineering-Chemistry" / "uploads"
    folder.mkdir(parents=True)
    (folder / "solo.pdf").write_bytes(b"%PDF-1.4")
    args = notes.argparse.Namespace(
        library=tmp_path, out=tmp_path / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=True, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        s = uploads_of(port, None)
        assert [u["name"] for u in s["uploads"]] == ["solo.pdf"]
        assert "id" not in s["uploads"][0], "no row behind it, so no vote button"
        assert call(port, "POST", "/vote", {"id": "x"})[0] == 404
        assert call(port, "GET", "/me")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_me_tab_hands_the_invite_code_to_an_admin_and_to_nobody_else(server):
    """The Me tab is where the class admin fetches the code for a new joiner,
    so /me carries it -- but only theirs. A member's copy has no code in it at
    all, which is the only version of this that cannot leak one."""
    port, cookies = server
    invite = lambda who: json.loads(call(port, "GET", "/me", cookie=cookies[who])[1])
    # Cleared first: the pick is table-wide, so anything already live in here
    # would answer for the code under test.
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("delete from invites")
        conn.execute(
            "insert into invites (code, expires_at, max_uses, uses) values "
            "('DEAD', now() - interval '1 day', 200, 0), "        # ran out of time
            "('SPENT', now() + interval '90 days', 5, 5)")        # ran out of uses
    try:
        assert invite("Asha")["invite"] is None, "a dead code is not one to hand out"

        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("insert into invites (code, expires_at) values "
                         "('SOON', now() + interval '1 day'), "
                         "('SEC-I', now() + interval '30 days')")
        mine = invite("Asha")
        # Of the live ones, the longest-lived: it is the code a new joiner is
        # most likely to still be able to use by the time they type it.
        assert (mine["admin"], mine["invite"]) == (True, "SEC-I")

        theirs = json.loads(call(port, "GET", "/me", cookie=cookies["Bilal"])[1])
        assert theirs["admin"] is False
        assert theirs["invite"] is None, "a member must never be handed an invite code"
        # And the screen behind the link stays admin-only whatever /me says.
        assert call(port, "GET", "/admin", cookie=cookies["Bilal"])[0] == 403
        assert call(port, "GET", "/admin", cookie=cookies["Asha"])[0] == 200
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("delete from invites")
