"""The notice board, against the real database.

Two halves, the same shape as test_votes.py. The first drives the policies on a
transaction that rolls back: who may post, who may edit, and what "delete"
actually does to the row. The second starts notes.py's own server and posts
over HTTP as each role, because "class reps only" is worth exactly what the running
server enforces -- and here that is a policy, not a check in a handler.
"""

import json
import os
import pathlib
import sys
import threading

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL, as_admin_connection, as_user, make_user  # noqa: E402
from test_auth import SECRET, call  # noqa: E402
from test_content import member  # noqa: E402


def post(db, author, title="Lab moved", body="", pinned=False):
    return notes.db_write_announcement(db, author, None, title, body, pinned, None)


# --------------------------------------------------------- the policies


def test_an_admin_posts_and_the_class_reads_it(db):
    boss, student = member(db, admin=True), member(db, role="student")
    as_user(db, boss)
    post(db, boss, "Chemistry lab moved", "To **Friday**.", pinned=True)

    as_user(db, student)
    got = notes.db_announcements(db, student)
    assert [a["title"] for a in got] == ["Chemistry lab moved"]
    assert got[0]["body"] == "To **Friday**." and got[0]["pinned"] is True
    assert got[0]["by"] == "M", "the board says who wrote it"
    assert got[0]["unread"] is True and got[0]["mine"] is False


def test_a_student_cannot_post_one(db):
    """No handler decides this. The insert policy does, so a student holding a
    database connection gets no further than a student holding a phone."""
    student = member(db, role="student")
    as_user(db, student)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        post(db, student)


def test_a_trusted_member_cannot_post_one_either(db):
    """Trusted is what may spend the API budget and add notes. Telling the
    whole section something is a different thing."""
    trusted = member(db)
    as_user(db, trusted)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        post(db, trusted)


def test_an_admin_cannot_post_in_somebody_elses_name(db):
    """The row says who wrote it and the class reads that, so author_id is
    pinned to whoever is asking."""
    boss, other = member(db, admin=True), member(db, admin=True)
    as_user(db, boss)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        notes.db_write_announcement(db, other, None, "Not mine", "", False, None)


def test_pending_and_blocked_members_are_not_told_anything(db):
    boss = member(db, admin=True)
    as_user(db, boss)
    post(db, boss)

    as_admin_connection(db)
    for status in ("pending", "blocked"):
        uid = make_user(db)
        db.execute("insert into profiles (id, name, status, role) "
                   "values (%s, 'Outside', %s, 'student')", (uid, status))
        as_user(db, uid)
        assert notes.db_announcements(db, uid) == [], status
        # At the table, not through the reader: db_announcements joins profiles,
        # and profiles' own policy is enough to make the line above green with
        # is_approved() taken off this table entirely. Denormalise the author
        # name onto the row one day and that would uncover the titles and bodies
        # with the suite still passing. This is the clause the test is named for.
        assert db.execute(
            "select count(*) from announcements").fetchone()[0] == 0, status
        as_admin_connection(db)


def test_the_board_a_phone_is_sent_is_capped(db):
    """/data carries the board to every phone on every app open, so the caps
    are what decide how big that answer can get. Thirty notices, and a title
    trimmed by the server rather than left for the check constraint to refuse
    as "an announcement needs a title"."""
    boss = member(db, admin=True)
    as_user(db, boss)
    # Literal numbers on both sides on purpose: read through notes.ANN_LIMIT
    # the check agrees with whatever the constant happens to say, including
    # 100000.
    for i in range(31):
        post(db, boss, f"Notice {i}")
    assert len(notes.db_announcements(db, boss)) == 30

    # Read back by id: every row in this transaction shares one now(), so the
    # board's order among them is not a thing to reach into.
    aid = post(db, boss, "x" * 200, "y" * 4050)
    title, body = db.execute(
        "select title, body from announcements where id = %s", (aid,)).fetchone()
    assert title == "x" * 120, "a long title is trimmed, not refused"
    assert body == "y" * 4000


def test_an_admin_edits_their_own_and_only_their_own(db):
    mine, theirs = member(db, admin=True), member(db, admin=True)
    as_user(db, mine)
    aid = post(db, mine, "Mine")

    as_user(db, theirs)
    with pytest.raises(ValueError):
        notes.db_write_announcement(db, theirs, aid, "Hijacked", "", False, None)

    as_user(db, mine)
    notes.db_write_announcement(db, mine, aid, "Mine, fixed", "now with a body",
                                True, None)
    got = notes.db_announcements(db, mine)[0]
    assert (got["title"], got["body"], got["pinned"]) == ("Mine, fixed",
                                                          "now with a body", True)
    assert got["edited"], "an edited notice says so"


def test_deleting_hides_the_row_rather_than_destroying_it(db):
    """A notice goes to a hundred and ten people at once, so a mistake has to
    be recoverable. It is hidden from everybody, kept for its author, and put
    back by the same call that hid it."""
    boss, student = member(db, admin=True), member(db, role="student")
    as_user(db, boss)
    aid = post(db, boss, "Posted at midnight")
    notes.db_write_announcement(db, boss, aid, None, None, None, True)

    as_user(db, student)
    assert notes.db_announcements(db, student) == [], "hidden means hidden"

    as_admin_connection(db)
    assert db.execute("select count(*) from announcements").fetchone()[0] == 1, \
        "the row is still there to be recovered"

    as_user(db, boss)
    hidden = notes.db_announcements(db, boss)
    assert len(hidden) == 1 and hidden[0]["deleted"] is True, \
        "its author can still see it, which is how they put it back"

    notes.db_write_announcement(db, boss, aid, None, None, None, False)
    as_user(db, student)
    assert [a["title"] for a in notes.db_announcements(db, student)] \
        == ["Posted at midnight"]


def test_nothing_reachable_from_a_session_can_destroy_one(db):
    """There is no delete policy on the table at all, so this is refused even
    for the admin who wrote it."""
    boss = member(db, admin=True)
    as_user(db, boss)
    aid = post(db, boss)
    db.execute("delete from announcements where id = %s", (aid,))
    as_admin_connection(db)
    assert db.execute("select count(*) from announcements").fetchone()[0] == 1


def test_pinned_first_then_newest(db):
    """Pinned above everything, and newest first under it.

    The ages are set by hand because every post in one test shares one
    transaction, and now() is the transaction's clock: three rows inserted here
    are the same age to the second, which is not a state the running server can
    produce and not one worth ordering by chance.
    """
    boss = member(db, admin=True)
    as_user(db, boss)
    ids = {t: post(db, boss, t, pinned=(t == "pinned"))
           for t in ("oldest", "middle", "newest", "pinned")}
    as_admin_connection(db)
    for title, days in (("oldest", 3), ("middle", 2), ("newest", 1), ("pinned", 4)):
        db.execute("update announcements set created_at = now() - %s * interval '1 day' "
                   "where id = %s", (days, ids[title]))
    as_user(db, boss)
    assert [a["title"] for a in notes.db_announcements(db, boss)] \
        == ["pinned", "newest", "middle", "oldest"], \
        "a pinned notice stays on top even when it is the oldest one there"


def test_a_notice_with_no_title_is_refused_by_both_layers(db):
    boss = member(db, admin=True)
    as_user(db, boss)
    with pytest.raises(ValueError):
        post(db, boss, "   ")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into announcements (author_id, title) values (%s, ' ')",
                   (boss,))


# ------------------------------------------------------- read, per person


def test_unread_is_per_person(db):
    boss, one, two = member(db, admin=True), member(db), member(db)
    as_user(db, boss)
    aid = post(db, boss)

    as_user(db, one)
    notes.db_mark_read(db, one, [aid])
    assert notes.db_announcements(db, one)[0]["unread"] is False

    as_user(db, two)
    assert notes.db_announcements(db, two)[0]["unread"] is True, \
        "one person reading it does not read it for the class"


def test_reading_twice_is_not_an_error(db):
    """The phone sends what is on screen, and what is on screen is often
    something already read on the laptop."""
    boss, reader = member(db, admin=True), member(db)
    as_user(db, boss)
    aid = post(db, boss)
    as_user(db, reader)
    notes.db_mark_read(db, reader, [aid])
    notes.db_mark_read(db, reader, [aid])
    assert notes.db_announcements(db, reader)[0]["unread"] is False


def test_nobody_can_mark_a_notice_read_for_somebody_else(db):
    boss, one, two = member(db, admin=True), member(db), member(db)
    as_user(db, boss)
    aid = post(db, boss)
    as_user(db, one)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into announcement_reads (announcement_id, profile_id) "
                   "values (%s, %s)", (aid, two))


def test_read_marks_are_your_own_business(db):
    boss, one, two = member(db, admin=True), member(db), member(db)
    as_user(db, boss)
    aid = post(db, boss)
    as_user(db, one)
    notes.db_mark_read(db, one, [aid])
    as_user(db, two)
    assert db.execute("select count(*) from announcement_reads").fetchone()[0] == 0


# ---------------------------------------------------- the real server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """notes.py's own server on a real socket, with one person in each role."""
    os.environ["RECARVE_SECRET"] = SECRET.decode()
    lib = tmp_path_factory.mktemp("library")

    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("announcement_reads", "announcements", "votes", "materials",
                      "lectures", "profiles"):
            conn.execute(f"delete from {table}")
        for name, role in (("Asha", "admin"), ("Devi", "admin"),
                           ("Bilal", "trusted"), ("Chan", "student")):
            uid = make_user(conn)
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role, password) "
                "values (%s, %s, %s, 'approved', %s, 'a-real-password')",
                (uid, name, name, role))
            people[name] = uid

    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], {n: notes.sign_session(i, SECRET)
                                  for n, i in people.items()}

    srv.shutdown()
    srv.server_close()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("announcement_reads", "announcements", "profiles", "auth.users"):
            conn.execute(f"delete from {table}")


def board(port, cookie):
    """What Home and Campus draw from: the board rides on /data, so neither tab
    spends a request of its own on it."""
    status, body, _ = call(port, "GET", "/data", cookie=cookie)
    assert status == 200, body
    return json.loads(body).get("announcements")


def test_only_a_class_rep_can_post_and_everyone_approved_reads(server):
    port, cookies = server
    status, body, _ = call(port, "POST", "/announce",
                           {"title": "Lab moved to Friday",
                            "body": "Bring **goggles**.", "pinned": True},
                           cookie=cookies["Asha"])
    assert status == 200, body
    assert [a["title"] for a in json.loads(body)["announcements"]] \
        == ["Lab moved to Friday"]

    for who in ("Asha", "Bilal", "Chan"):
        got = board(port, cookies[who])
        assert [a["title"] for a in got] == ["Lab moved to Friday"], who
        assert got[0]["by"] == "Asha"
        assert got[0]["mine"] is (who == "Asha")

    for who in ("Bilal", "Chan"):
        status, body, _ = call(port, "POST", "/announce", {"title": "Class off"},
                               cookie=cookies[who])
        assert status == 403, f"{who} posted an announcement"
        assert json.loads(body)["required"] == "cr"
    assert len(board(port, cookies["Asha"])) == 1, "and nothing of theirs landed"


def test_a_stranger_gets_nothing_and_posts_nothing(server):
    port, cookies = server
    assert call(port, "POST", "/announce", {"title": "x"})[0] == 403
    assert call(port, "POST", "/announce", {"title": "x"}, cookie="garbage")[0] == 403
    assert call(port, "POST", "/read", {"ids": []})[0] == 403


def test_unread_counts_are_per_person_over_the_socket(server):
    port, cookies = server
    aid = board(port, cookies["Chan"])[0]["id"]
    assert board(port, cookies["Chan"])[0]["unread"] is True

    assert call(port, "POST", "/read", {"ids": [aid]}, cookie=cookies["Chan"])[0] == 200
    assert board(port, cookies["Chan"])[0]["unread"] is False
    assert board(port, cookies["Bilal"])[0]["unread"] is True, \
        "Chan reading it did not read it for Bilal"


def test_a_body_cannot_put_a_script_on_anybody_else_s_page(server):
    """Bodies are written by an admin and rendered as markdown on 110 phones.

    The server stores what was typed -- escaping it here would show the escape
    to every reader -- and the page is what refuses to build a tag out of it:
    mdSafe escapes every '<' before marked sees one. This asserts the storage
    half; test_page.py's `mdSafe` checks assert the rendering half.
    """
    port, cookies = server
    nasty = "<script>fetch('/admin')</script>\n\n[tap](javascript:alert(1))"
    status, body, _ = call(port, "POST", "/announce",
                           {"title": "<img src=x onerror=alert(1)>", "body": nasty},
                           cookie=cookies["Asha"])
    assert status == 200, body
    got = next(a for a in board(port, cookies["Chan"])
               if a["title"].startswith("<img"))
    assert got["body"] == nasty, "stored as typed, escaped where it is drawn"
    # And the page it is drawn on never interpolates either of them into HTML.
    assert "textContent = a.title" in notes.PAGE
    assert "innerHTML = mdSafe(a.body)" in notes.PAGE


def test_an_admin_edits_and_hides_their_own_over_the_socket(server):
    port, cookies = server
    aid = next(a["id"] for a in board(port, cookies["Asha"])
               if a["title"].startswith("Lab moved"))

    status, body, _ = call(port, "POST", "/announce",
                           {"id": aid, "title": "Lab moved to Monday",
                            "body": "Sorry.", "pinned": False},
                           cookie=cookies["Devi"])
    assert status == 400, body
    assert "not yours" in json.loads(body)["error"]

    assert call(port, "POST", "/announce",
                {"id": aid, "title": "Lab moved to Monday", "body": "Sorry."},
                cookie=cookies["Asha"])[0] == 200
    got = next(a for a in board(port, cookies["Chan"]) if a["id"] == aid)
    assert (got["title"], got["body"]) == ("Lab moved to Monday", "Sorry.")
    assert got["edited"]

    assert call(port, "POST", "/announce", {"id": aid, "deleted": True},
                cookie=cookies["Asha"])[0] == 200
    assert not [a for a in board(port, cookies["Chan"]) if a["id"] == aid], \
        "a hidden notice is off the class's board"
    hidden = next(a for a in board(port, cookies["Asha"]) if a["id"] == aid)
    assert hidden["deleted"] is True, "and still on its author's, to be put back"

    assert call(port, "POST", "/announce", {"id": aid, "deleted": False},
                cookie=cookies["Asha"])[0] == 200
    assert [a for a in board(port, cookies["Chan"]) if a["id"] == aid]


def test_a_notice_that_is_nothing_is_refused_rather_than_500(server):
    port, cookies = server
    assert call(port, "POST", "/announce", {"title": "  "},
                cookie=cookies["Asha"])[0] == 400
    assert call(port, "POST", "/announce", {"id": "not-a-uuid", "title": "x"},
                cookie=cookies["Asha"])[0] == 404
    assert call(port, "POST", "/read", {"ids": "nope"},
                cookie=cookies["Chan"])[0] == 400
    assert call(port, "POST", "/read", {"ids": ["not-a-uuid"]},
                cookie=cookies["Chan"])[0] == 400
