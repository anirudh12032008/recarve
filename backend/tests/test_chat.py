"""The subject room: short messages, in order, newest at the bottom.

Half policies on a rolled-back transaction, half notes.py's own server over
HTTP. What the HTTP half is really testing is the polling contract, because
that is the part with a cost attached: an open room asks for what came after
the last id it holds, and the answer to "nothing has happened" has to be empty.
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


# --------------------------------------------------------- the policies


def test_the_room_says_who_and_when(db):
    me, reader = member(db), member(db)
    as_user(db, me)
    notes.db_say(db, me, "CY1107", "is the quiz on Friday?")
    as_user(db, reader)
    [m] = notes.db_chat(db, reader, "CY1107")
    assert (m["body"], m["by"], m["mine"]) == ("is the quiz on Friday?", "M", False)
    assert m["at"] and isinstance(m["id"], int)


def test_newest_at_the_bottom(db):
    me = member(db)
    as_user(db, me)
    for word in ("one", "two", "three"):
        notes.db_say(db, me, "CY1107", word)
    assert [m["body"] for m in notes.db_chat(db, me, "CY1107")] == \
        ["one", "two", "three"]


def test_a_room_is_per_subject(db):
    me = member(db)
    as_user(db, me)
    notes.db_say(db, me, "CY1107", "chemistry")
    notes.db_say(db, me, "MC1101", "maths")
    assert [m["body"] for m in notes.db_chat(db, me, "CY1107")] == ["chemistry"]


def test_since_asks_only_for_what_is_new(db):
    """The whole reason the id is a bigint. An open room must never ask for
    the history it already has."""
    me = member(db)
    as_user(db, me)
    first = notes.db_say(db, me, "CY1107", "hello")
    assert notes.db_chat(db, me, "CY1107", since=first) == []
    notes.db_say(db, me, "CY1107", "still there?")
    fresh = notes.db_chat(db, me, "CY1107", since=first)
    assert [m["body"] for m in fresh] == ["still there?"]


def test_opening_a_room_gets_the_tail_and_not_the_history(db):
    me = member(db)
    as_user(db, me)
    for i in range(12):
        notes.db_say(db, me, "CY1107", f"m{i}")
    tail = notes.db_chat(db, me, "CY1107", limit=5)
    assert [m["body"] for m in tail] == ["m7", "m8", "m9", "m10", "m11"], \
        "the last five, oldest first -- which is the order they are read in"


def test_a_long_message_is_trimmed_rather_than_refused(db):
    me = member(db)
    as_user(db, me)
    notes.db_say(db, me, "CY1107", "x" * 900)
    assert len(notes.db_chat(db, me, "CY1107")[0]["body"]) == notes.CHAT_BODY


def test_the_cap_is_the_databases_too(db):
    """Trimming in the handler is a kindness; this is the rule."""
    me = member(db)
    as_user(db, me)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into messages (subject_code, author_id, body) "
                   "values ('CY1107', %s, %s)", (me, "x" * 501))
    db.rollback()


def test_an_empty_message_is_refused(db):
    me = member(db)
    as_user(db, me)
    with pytest.raises(ValueError):
        notes.db_say(db, me, "CY1107", "   ")


def test_a_made_up_subject_is_refused(db):
    me = member(db)
    as_user(db, me)
    with pytest.raises(ValueError):
        notes.db_say(db, me, "ZZ9999", "hello")


def test_thirty_in_five_minutes_and_then_the_database_says_no(db):
    me = member(db)
    as_user(db, me)
    for i in range(30):
        notes.db_say(db, me, "CY1107", f"m{i}")
    with pytest.raises(psycopg.errors.RaiseException) as exc:
        notes.db_say(db, me, "CY1107", "one more")
    assert "slow down" in str(exc.value)
    db.rollback()


def test_the_flood_limit_is_per_person(db):
    a, b = member(db), member(db)
    as_user(db, a)
    for i in range(30):
        notes.db_say(db, a, "CY1107", f"a{i}")
    as_user(db, b)
    notes.db_say(db, b, "CY1107", "b0")
    assert notes.db_chat(db, b, "CY1107")[-1]["body"] == "b0"


def test_the_flood_limit_counts_every_room(db):
    """Thirty a person, not thirty a room: twelve subjects would otherwise be
    three hundred and sixty messages in five minutes."""
    me = member(db)
    as_user(db, me)
    for i in range(30):
        notes.db_say(db, me, "CY1107", f"m{i}")
    with pytest.raises(psycopg.errors.RaiseException):
        notes.db_say(db, me, "MC1101", "different room, same person")
    db.rollback()


def test_nobody_talks_in_somebody_elses_name(db):
    me, other = member(db), member(db)
    as_user(db, me)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into messages (subject_code, author_id, body) "
                   "values ('CY1107', %s, 'not mine')", (other,))
    db.rollback()


def test_a_pending_joiner_neither_reads_nor_talks(db):
    me = member(db)
    as_user(db, me)
    notes.db_say(db, me, "CY1107", "members only")
    pending = make_user(db)
    as_admin_connection(db)
    db.execute("insert into profiles (id, name, status, role) "
               "values (%s, 'P', 'pending', 'student')", (pending,))
    as_user(db, pending)
    assert notes.db_chat(db, pending, "CY1107") == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        notes.db_say(db, pending, "CY1107", "let me in")
    db.rollback()


def test_a_student_reads_and_talks(db):
    """Deliberately not a role. A student who may not upload can still be the
    one who knows the lab moved."""
    me = member(db, role="student")
    as_user(db, me)
    notes.db_say(db, me, "CY1107", "lab is in B2 today")
    assert len(notes.db_chat(db, me, "CY1107")) == 1


def test_taking_back_what_you_said(db):
    me, reader = member(db), member(db)
    as_user(db, me)
    mid = notes.db_say(db, me, "CY1107", "wrong room")
    notes.db_hide_message(db, mid)
    as_user(db, reader)
    assert notes.db_chat(db, reader, "CY1107") == []
    as_admin_connection(db)
    assert db.execute("select count(*) from messages").fetchone()[0] == 1, \
        "hidden, not deleted"


def test_a_bystander_cannot_remove_what_you_said(db):
    me, other = member(db), member(db)
    as_user(db, me)
    mid = notes.db_say(db, me, "CY1107", "mine")
    as_user(db, other)
    with pytest.raises(ValueError):
        notes.db_hide_message(db, mid)
    assert len(notes.db_chat(db, other, "CY1107")) == 1


def test_an_admin_removes_anybodys(db):
    boss, me = member(db, admin=True), member(db)
    as_user(db, me)
    mid = notes.db_say(db, me, "CY1107", "something vile")
    as_user(db, boss)
    notes.db_hide_message(db, mid)
    assert notes.db_chat(db, boss, "CY1107") == []


def test_a_message_is_not_editable(db):
    me = member(db)
    as_user(db, me)
    mid = notes.db_say(db, me, "CY1107", "as said")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("update messages set body = 'other' where id = %s", (mid,))
    db.rollback()


def test_nothing_reachable_from_a_session_destroys_a_message(db):
    boss, me = member(db, admin=True), member(db)
    as_user(db, me)
    mid = notes.db_say(db, me, "CY1107", "on the record")
    as_user(db, boss)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("delete from messages where id = %s", (mid,))
    db.rollback()


# ---------------------------------------------------- the real server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    os.environ["RECARVE_SECRET"] = SECRET.decode()
    lib = tmp_path_factory.mktemp("library")

    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("messages", "profiles"):
            conn.execute(f"delete from {table}")
        for name, role in (("Asha", "admin"), ("Chan", "student"),
                           ("Dia", "student")):
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
        for table in ("messages", "profiles", "auth.users"):
            conn.execute(f"delete from {table}")


@pytest.fixture(autouse=True)
def _empty_room(request):
    if "server" in request.fixturenames:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("delete from messages")
    yield


def say(port, cookie, **payload):
    payload.setdefault("subject", "CY1107")
    return call(port, "POST", "/chat", payload, cookie=cookie)


def room(port, cookie, since=0):
    status, body, _ = call(port, "GET", f"/chat?subject=CY1107&since={since}",
                           cookie=cookie)
    assert status == 200, body
    return json.loads(body)["messages"]


def test_two_members_talk_over_http(server):
    port, who = server
    assert say(port, who["Chan"], body="anyone in LT3?")[0] == 200
    assert say(port, who["Dia"], body="on my way")[0] == 200
    assert [(m["by"], m["body"]) for m in room(port, who["Asha"])] == [
        ("Chan", "anyone in LT3?"), ("Dia", "on my way")]


def test_a_poll_with_nothing_new_comes_back_empty(server):
    """The request an open room makes most of the time. It must cost nothing
    and it must not send the room again."""
    port, who = server
    say(port, who["Chan"], body="hello")
    last = room(port, who["Dia"])[-1]["id"]
    assert room(port, who["Dia"], since=last) == []
    say(port, who["Chan"], body="still here")
    assert [m["body"] for m in room(port, who["Dia"], since=last)] == ["still here"]


def test_posting_answers_with_only_what_the_phone_has_not_got(server):
    port, who = server
    say(port, who["Chan"], body="first")
    last = room(port, who["Dia"])[-1]["id"]
    status, body, _ = say(port, who["Dia"], body="second", since=last)
    assert status == 200
    assert [m["body"] for m in json.loads(body)["messages"]] == ["second"]


def test_the_room_is_named_by_the_request_and_refuses_a_made_up_one(server):
    port, who = server
    assert call(port, "GET", "/chat?subject=ZZ9999", cookie=who["Chan"])[0] == 400
    assert say(port, who["Chan"], subject="ZZ9999", body="hi")[0] == 400


def test_flooding_is_refused_over_http(server):
    port, who = server
    for i in range(30):
        assert say(port, who["Dia"], body=f"m{i}")[0] == 200
    status, body, _ = say(port, who["Dia"], body="one more")
    assert status == 429, body
    assert "slow down" in json.loads(body)["error"]


def test_an_admin_removes_a_message_over_http(server):
    port, who = server
    say(port, who["Chan"], body="something vile")
    mid = room(port, who["Asha"])[-1]["id"]
    status, body, _ = say(port, who["Asha"], id=mid, delete=True)
    assert status == 200 and json.loads(body)["removed"] == mid, body
    assert room(port, who["Dia"]) == []


def test_a_bystander_cannot_remove_a_message_over_http(server):
    port, who = server
    say(port, who["Chan"], body="not yours")
    mid = room(port, who["Dia"])[-1]["id"]
    assert say(port, who["Dia"], id=mid, delete=True)[0] == 400
    assert len(room(port, who["Dia"])) == 1


def test_a_signed_out_caller_gets_nowhere(server):
    port, _ = server
    assert call(port, "GET", "/chat?subject=CY1107")[0] in (302, 401, 403)
    assert call(port, "POST", "/chat", {"subject": "CY1107", "body": "x"})[0] \
        in (302, 401, 403)


def test_the_page_puts_a_message_on_the_screen_as_typing(server):
    port, who = server
    status, page, _ = call(port, "GET", "/", cookie=who["Dia"])
    assert status == 200
    fn = page[page.index("function chatLine("):]
    fn = fn[:fn.index("\nfunction ")]
    assert "innerHTML" not in fn, "a message must never be parsed as markup"
    assert "textContent" in fn
