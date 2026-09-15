"""Doubts: what one student asks about a lecture, and what the section answers.

Two halves, the same shape as test_announcements.py. The first drives the
policies on a transaction that rolls back -- who may ask, who may answer, whose
words may be taken down, and what a second student holding a session of their
own can do to somebody else's question. The second starts notes.py's own server
and posts over HTTP as each role, because a thread two levels deep is worth
exactly what the running server enforces, and here that is a policy rather than
a check in a handler.

The votes here are the same votes the notes have. That is deliberate, and it is
what these tests are checking as much as anything: one table, one
one-per-person rule, one "not your own".
"""

import json
import os
import pathlib
import sys
import tempfile
import threading

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL, as_admin_connection, as_user, make_user  # noqa: E402
from test_auth import SECRET, call  # noqa: E402
from test_content import member  # noqa: E402


def lecture(db, uploader, title="CY1107-week1"):
    """One recorded class, for questions to hang off."""
    as_admin_connection(db)
    lid = db.execute(
        "insert into lectures (subject_code, uploader_id, title, audio_key, status) "
        "values ('CY1107', %s, %s, %s, 'done') returning id",
        (uploader, title, "k-" + title),
    ).fetchone()[0]
    as_user(db, uploader)
    return str(lid)


# --------------------------------------------------------- the policies


def test_a_question_and_the_answers_under_it(db):
    asker, helper = member(db), member(db)
    lid = lecture(db, asker)

    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Why is the ratio 2:1?")
    as_user(db, helper)
    notes.db_ask(db, helper, "CY1107", lid, qid, "Because the equation balances.")

    thread = notes.db_doubts(db, helper, "CY1107", lid)
    assert [q["body"] for q in thread] == ["Why is the ratio 2:1?"]
    q = thread[0]
    assert q["by"] == "M" and q["mine"] is False
    assert [(a["body"], a["mine"]) for a in q["answers"]] == [
        ("Because the equation balances.", True)]
    assert q["at"] and q["answers"][0]["at"], "the thread says when, not just who"


def test_a_student_asks_and_a_student_answers(db):
    """The whole point of it. A student may not upload, may not record and may
    not spend the API budget -- and this is the one thing they can give."""
    asker, helper = member(db, role="student"), member(db, role="student")
    lid = lecture(db, member(db))

    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "What is a mole?")
    as_user(db, helper)
    notes.db_ask(db, helper, "CY1107", lid, qid, "6.022e23 of anything.")

    assert len(notes.db_doubts(db, asker, "CY1107", lid)[0]["answers"]) == 1


def test_a_question_can_be_about_a_subject_with_no_lecture_behind_it(db):
    """The library holds files the database has never heard of, and "why is
    this integral like that" must not need a recording to exist first."""
    asker = member(db, role="student")
    as_user(db, asker)
    notes.db_ask(db, asker, "MC1101", None, None, "Is the limit two-sided?")

    assert [q["body"] for q in notes.db_doubts(db, asker, "MC1101")] == [
        "Is the limit two-sided?"]
    assert notes.db_doubts(db, asker, "CY1107") == [], "one subject, not all of them"


def test_a_lecture_thread_and_the_subject_thread_are_not_the_same_thread(db):
    asker = member(db)
    lid = lecture(db, asker)
    as_user(db, asker)
    notes.db_ask(db, asker, "CY1107", lid, None, "About this lecture.")
    notes.db_ask(db, asker, "CY1107", None, None, "About the whole subject.")

    assert [q["body"] for q in notes.db_doubts(db, asker, "CY1107", lid)] == [
        "About this lecture."]
    assert [q["body"] for q in notes.db_doubts(db, asker, "CY1107")] == [
        "About the whole subject."]


def test_a_pending_joiner_can_neither_read_nor_ask(db):
    asker = member(db)
    lid = lecture(db, asker)
    as_user(db, asker)
    notes.db_ask(db, asker, "CY1107", lid, None, "Anybody?")

    as_admin_connection(db)
    waiting = make_user(db)
    db.execute("insert into profiles (id, name) values (%s, 'Waiting')", (waiting,))
    as_user(db, waiting)
    assert notes.db_doubts(db, waiting, "CY1107", lid) == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        notes.db_ask(db, waiting, "CY1107", lid, None, "Let me in.")


def test_you_cannot_ask_in_somebody_elses_name(db):
    """'members ask and answer': the row you insert has to be about you."""
    me, patsy = member(db), member(db)
    lid = lecture(db, me)
    as_user(db, me)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into doubts (subject_code, lecture_id, author_id, body) "
                   "values ('CY1107', %s, %s, 'not mine')", (lid, patsy))


def test_a_second_student_cannot_take_down_somebody_elses_question(db):
    """The attack, not the happy path. A classmate holds a session of their own
    and aims it at the asker's question -- by the handler's own function, and
    then by hand with the UPDATE the handler would have run. Both leave the row
    exactly as it was."""
    asker, attacker = member(db, role="student"), member(db, role="student")
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Mine, and mine to delete.")

    as_user(db, attacker)
    with pytest.raises(ValueError):
        notes.db_hide_doubt(db, qid)
    db.execute("update doubts set deleted_at = now() where id = %s", (qid,))
    db.execute("update doubts set body = 'rewritten' where id = %s", (qid,))

    as_admin_connection(db)
    row = db.execute("select body, deleted_at from doubts where id = %s",
                     (qid,)).fetchone()
    assert row == ("Mine, and mine to delete.", None)


def test_a_second_student_cannot_answer_in_the_first_ones_name(db):
    asker, attacker = member(db, role="student"), member(db, role="student")
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Anybody?")

    as_user(db, attacker)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into doubts (parent_id, author_id, body) "
                   "values (%s, %s, 'signed by somebody else')", (qid, asker))


def test_deleting_hides_the_row_rather_than_destroying_it(db):
    """Same as the notice board. A hundred and ten people may have read it, and
    a row that is gone is a row nobody can look at again."""
    asker = member(db)
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Never mind.")
    notes.db_hide_doubt(db, qid)

    assert notes.db_doubts(db, asker, "CY1107", lid) == []
    as_admin_connection(db)
    assert db.execute("select count(*) from doubts where id = %s",
                      (qid,)).fetchone()[0] == 1


def test_nobody_may_destroy_a_doubt_even_their_own(db):
    """No delete policy at all, on purpose: a session cannot reach the row.

    And no delete GRANT either (0025 grants select, insert, update), so the
    refusal arrives a layer earlier than the policy -- the statement is thrown
    out before any row is considered. Asserting the raise rather than a
    zero rowcount is what says which of the two is doing the work.
    """
    asker = member(db, admin=True)
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Mine.")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction():
            db.execute("delete from doubts where id = %s", (qid,))
    as_admin_connection(db)
    assert db.execute("select count(*) from doubts where id = %s",
                      (qid,)).fetchone()[0] == 1


def test_an_admin_can_take_down_anybodys(db):
    student = member(db, role="student")
    boss = member(db, admin=True)
    lid = lecture(db, boss)
    as_user(db, student)
    qid = notes.db_ask(db, student, "CY1107", lid, None, "Out of order.")

    as_user(db, boss)
    notes.db_hide_doubt(db, qid)
    assert notes.db_doubts(db, boss, "CY1107", lid) == []


def test_a_hidden_question_takes_its_answers_with_it(db):
    asker, helper = member(db), member(db)
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Gone in a moment.")
    as_user(db, helper)
    notes.db_ask(db, helper, "CY1107", lid, qid, "Still here?")

    as_user(db, asker)
    notes.db_hide_doubt(db, qid)
    assert notes.db_doubts(db, helper, "CY1107", lid) == []


def test_a_thread_is_two_deep_and_no_deeper(db):
    """The insert policy, not the screen. A reply to a reply is one request
    away otherwise, and every thread on every note grows a recursion."""
    asker, helper = member(db), member(db)
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Why?")
    as_user(db, helper)
    aid = notes.db_ask(db, helper, "CY1107", lid, qid, "Because.")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        notes.db_ask(db, helper, "CY1107", lid, aid, "Because of what?")


def test_you_cannot_answer_a_question_that_is_gone(db):
    asker, helper = member(db), member(db)
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Never mind.")
    notes.db_hide_doubt(db, qid)

    as_user(db, helper)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        notes.db_ask(db, helper, "CY1107", lid, qid, "Too late.")


def test_an_empty_question_is_refused(db):
    asker = member(db)
    lid = lecture(db, asker)
    as_user(db, asker)
    with pytest.raises(ValueError):
        notes.db_ask(db, asker, "CY1107", lid, None, "   ")


# ------------------------------------------- the votes, which are the notes'


def test_the_best_answer_rises(db):
    """The whole reason answers are on the votes table: the class sorts them."""
    asker, first, second, voter = (member(db), member(db), member(db), member(db))
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Which is it?")
    as_user(db, first)
    early = notes.db_ask(db, first, "CY1107", lid, qid, "Answered first.")
    as_user(db, second)
    good = notes.db_ask(db, second, "CY1107", lid, qid, "Answered better.")

    as_user(db, voter)
    assert notes.db_vote(db, good, voter, True, "doubt_id") == {
        "votes": 1, "voted": True}
    answers = notes.db_doubts(db, voter, "CY1107", lid)[0]["answers"]
    assert [a["id"] for a in answers] == [good, early], "votes first, then oldest"
    assert (answers[0]["votes"], answers[0]["voted"]) == (1, True)
    assert (answers[1]["votes"], answers[1]["voted"]) == (0, False)


def test_you_cannot_upvote_your_own_answer(db):
    """0022's rule in the other direction. Answering yourself and upvoting it
    is one person agreeing with themselves at the top of the thread."""
    asker, helper = member(db), member(db)
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Anybody?")
    as_user(db, helper)
    aid = notes.db_ask(db, helper, "CY1107", lid, qid, "Me, obviously.")

    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        notes.db_vote(db, aid, helper, True, "doubt_id")
    as_admin_connection(db)
    assert db.execute("select count(*) from votes where doubt_id = %s",
                      (aid,)).fetchone()[0] == 0


def test_the_same_person_cannot_upvote_an_answer_twice(db):
    """No handler decides this. The unique index on (doubt_id, voter_id) does."""
    asker, helper, voter = member(db), member(db), member(db)
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Anybody?")
    as_user(db, helper)
    aid = notes.db_ask(db, helper, "CY1107", lid, qid, "Here.")

    as_user(db, voter)
    notes.db_vote(db, aid, voter, True, "doubt_id")
    with pytest.raises(psycopg.errors.UniqueViolation):
        notes.db_vote(db, aid, voter, True, "doubt_id")


def test_a_vote_is_for_exactly_one_kind_of_thing(db):
    """The check that replaced the primary key. A row for both, or for
    neither, is not a vote anybody could count."""
    voter = member(db)
    as_user(db, voter)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("insert into votes (voter_id) values (%s)", (voter,))


def test_answer_votes_do_not_touch_the_board(db):
    """Points are status, and this feature does not quietly change the sums the
    class is ranked by. A vote on an answer is not a vote on an upload."""
    asker, helper, voter = member(db), member(db), member(db)
    lid = lecture(db, asker)
    as_user(db, asker)
    qid = notes.db_ask(db, asker, "CY1107", lid, None, "Anybody?")
    as_user(db, helper)
    aid = notes.db_ask(db, helper, "CY1107", lid, qid, "Here.")
    as_user(db, voter)
    notes.db_vote(db, aid, voter, True, "doubt_id")

    as_user(db, helper)
    assert notes.db_contributions(db, helper)["points"] == {
        "uploads": 0, "recordings": 0, "votes_received": 0, "score": 0}


def test_upvoting_an_upload_still_works_exactly_as_it_did(db):
    """The table changed shape underneath it; the notes' votes did not."""
    owner, voter = member(db), member(db)
    as_user(db, owner)
    mid = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, "
        "size_bytes) values ('CY1107', %s, 'a.pdf', 'a.pdf', 10) returning id",
        (owner,),
    ).fetchone()[0]

    as_user(db, voter)
    assert notes.db_vote(db, mid, voter, True) == {"votes": 1, "voted": True}
    assert notes.db_vote(db, mid, voter, False) == {"votes": 0, "voted": False}


def test_nothing_else_is_votable(db):
    """db_vote writes a column name into its own SQL, so the two words it will
    accept are written down in one place and nothing else reaches Postgres."""
    voter = member(db)
    as_user(db, voter)
    with pytest.raises(ValueError):
        notes.db_vote(db, voter, voter, True, "voter_id")


# ---------------------------------------------------- the real server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """notes.py's own server on a real socket, with one person in each role."""
    os.environ["RECARVE_SECRET"] = SECRET.decode()
    lib = tmp_path_factory.mktemp("library")

    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("doubts", "votes", "materials", "lectures", "profiles"):
            conn.execute(f"delete from {table}")
        for name, role in (("Asha", "admin"), ("Bilal", "trusted"),
                           ("Chan", "student"), ("Dia", "student")):
            uid = make_user(conn)
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role, password) "
                "values (%s, %s, %s, 'approved', %s, 'a-real-password')",
                (uid, name, name, role))
            people[name] = uid
        conn.execute(
            "insert into lectures (subject_code, uploader_id, title, audio_key, "
            "status) values ('CY1107', %s, 'CY1107-week1', 'k', 'done')",
            (people["Asha"],))

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
        for table in ("votes", "doubts", "lectures", "profiles", "auth.users"):
            conn.execute(f"delete from {table}")


def thread(port, cookie, title="CY1107-week1"):
    status, body, _ = call(
        port, "GET", f"/doubts?subject=CY1107&title={title}", cookie=cookie)
    assert status == 200, body
    return json.loads(body)["doubts"]


def write(port, cookie, **payload):
    payload.setdefault("subject", "CY1107")
    payload.setdefault("title", "CY1107-week1")
    return call(port, "POST", "/doubts", payload, cookie=cookie)


def test_a_student_asks_and_another_student_answers_over_http(server):
    port, who = server
    status, body, _ = write(port, who["Chan"], body="Why 2:1?")
    assert status == 200, body
    qid = json.loads(body)["doubts"][0]["id"]

    status, body, _ = write(port, who["Dia"], parent=qid, body="It balances.")
    assert status == 200, body
    q = json.loads(body)["doubts"][0]
    assert q["body"] == "Why 2:1?"
    assert [a["body"] for a in q["answers"]] == ["It balances."]
    assert [a["by"] for a in q["answers"]] == ["Dia"]

    # And it is there for everybody, not just the two of them.
    assert [x["body"] for x in thread(port, who["Bilal"])] == ["Why 2:1?"]


def test_a_second_student_cannot_delete_what_the_first_one_asked(server):
    """The attack over the wire, with a real signed session of their own."""
    port, who = server
    status, body, _ = write(port, who["Chan"], body="Chan's own question.")
    qid = next(q["id"] for q in json.loads(body)["doubts"]
               if q["body"] == "Chan's own question.")

    status, body, _ = write(port, who["Dia"], id=qid, delete=True)
    assert status == 400, body
    assert "yours" in json.loads(body)["error"]
    assert any(q["id"] == qid for q in thread(port, who["Chan"]))

    # The asker's own delete works, which is what makes the refusal a refusal.
    assert write(port, who["Chan"], id=qid, delete=True)[0] == 200
    assert not any(q["id"] == qid for q in thread(port, who["Chan"]))


def test_an_admin_can_take_down_anybodys_over_http(server):
    port, who = server
    status, body, _ = write(port, who["Dia"], body="Out of order.")
    qid = next(q["id"] for q in json.loads(body)["doubts"]
               if q["body"] == "Out of order.")
    assert write(port, who["Asha"], id=qid, delete=True)[0] == 200
    assert not any(q["id"] == qid for q in thread(port, who["Dia"]))


def test_the_class_votes_an_answer_to_the_top_over_http(server):
    port, who = server
    status, body, _ = write(port, who["Chan"], body="Which one is right?")
    qid = next(q["id"] for q in json.loads(body)["doubts"]
               if q["body"] == "Which one is right?")
    write(port, who["Dia"], parent=qid, body="First answer.")
    status, body, _ = write(port, who["Bilal"], parent=qid, body="Better answer.")
    good = next(a["id"] for a in
                next(q for q in json.loads(body)["doubts"]
                     if q["id"] == qid)["answers"]
                if a["body"] == "Better answer.")

    status, body, _ = call(port, "POST", "/vote",
                           {"answer": good, "on": True}, cookie=who["Chan"])
    assert status == 200, body
    assert json.loads(body) == {"votes": 1, "voted": True}

    q = next(x for x in thread(port, who["Chan"]) if x["id"] == qid)
    assert [a["body"] for a in q["answers"]] == ["Better answer.", "First answer."]

    # Twice is still once.
    assert call(port, "POST", "/vote", {"answer": good, "on": True},
                cookie=who["Chan"])[0] == 409
    # And you cannot upvote your own.
    status, body, _ = call(port, "POST", "/vote", {"answer": good, "on": True},
                           cookie=who["Bilal"])
    assert status == 403, body
    assert "your own" in json.loads(body)["error"]

    assert write(port, who["Chan"], id=qid, delete=True)[0] == 200


def test_a_note_with_no_lecture_row_falls_back_to_the_subject(server):
    """A static export, or a file the database never adopted. The question
    belongs to the subject rather than to nothing at all."""
    port, who = server
    status, body, _ = call(
        port, "POST", "/doubts",
        {"subject": "CY1107", "title": "no-such-note", "body": "About chemistry."},
        cookie=who["Chan"])
    assert status == 200, body
    assert [q["body"] for q in json.loads(body)["doubts"]] == ["About chemistry."]
    # Not on the lecture's thread, which is a different conversation.
    assert not any(q["body"] == "About chemistry."
                   for q in thread(port, who["Chan"]))
    assert [q["body"] for q in thread(port, who["Chan"], title="")] == [
        "About chemistry."]

    qid = json.loads(body)["doubts"][0]["id"]
    assert call(port, "POST", "/doubts",
                {"subject": "CY1107", "title": "", "id": qid, "delete": True},
                cookie=who["Chan"])[0] == 200


def test_a_made_up_subject_is_refused(server):
    port, who = server
    status, body, _ = call(port, "POST", "/doubts",
                           {"subject": "ZZ9999", "body": "hello"},
                           cookie=who["Chan"])
    assert status == 400, body


def test_an_empty_question_is_refused_over_http(server):
    port, who = server
    status, body, _ = write(port, who["Chan"], body="  ")
    assert status == 400, body


def test_a_signed_out_caller_gets_nowhere(server):
    port, _ = server
    assert call(port, "GET", "/doubts?subject=CY1107")[0] in (302, 401, 403)
    assert call(port, "POST", "/doubts", {"subject": "CY1107", "body": "x"})[0] \
        in (302, 401, 403)


# ----------------------------------------------- Ask AI, over HTTP
#
# The bot answers nothing that is not already a row this caller can read, and
# it answers the same question once. Both of those are the whole feature: the
# first is its only permission check, and the second is what keeps a hundred
# and ten classmates on one thread inside a free tier.
#
# Groq itself is stubbed everywhere below. What is under test is the route --
# who it refuses, what it looks up, and when it declines to call out at all --
# and a test that needs the network to say so is a test that fails on a train.


@pytest.fixture
def ai(server):
    """A second server on the same database, with a budget to spend.

    Its own, and function-scoped, because the budget is a closure inside
    build_server: one call spent by one test is a call the next one does not
    have, and a shared server would make these pass or fail in file order.
    """
    port, who = server
    lib = pathlib.Path(tempfile.mkdtemp())
    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=2,
        ai_model="stub-model", no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], who, port
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def calls(monkeypatch):
    """Every groq() the route makes, and what it was asked."""
    seen = []

    def stub(system, user, model, max_tokens=1200):
        seen.append({"system": system, "user": user, "model": model})
        return "Because the denominator is what changes."

    monkeypatch.setattr(notes, "groq", stub)
    return seen


def test_the_machine_answers_a_question_on_the_thread(ai, calls):
    aiport, who, port = ai
    status, body, _ = write(port, who["Chan"], body="Why does the ratio flip?")
    assert status == 200, body
    qid = json.loads(body)["doubts"][0]["id"]

    status, body, _ = call(aiport, "POST", "/doubts/ai", {"id": qid},
                           cookie=who["Chan"])
    assert status == 200, body
    assert json.loads(body)["text"] == "Because the denominator is what changes."
    # Asked about the question as stored, under the subject it was asked in --
    # not about anything the phone put in the request.
    assert len(calls) == 1
    assert "Why does the ratio flip?" in calls[0]["user"]
    assert calls[0]["model"] == "stub-model"


def test_the_same_question_is_answered_once_for_the_whole_section(ai, calls):
    aiport, who, port = ai
    status, body, _ = write(port, who["Chan"], body="What cancels on the left?")
    qid = json.loads(body)["doubts"][0]["id"]

    first = call(aiport, "POST", "/doubts/ai", {"id": qid}, cookie=who["Chan"])
    second = call(aiport, "POST", "/doubts/ai", {"id": qid}, cookie=who["Dia"])
    assert first[0] == second[0] == 200, second[1]
    assert json.loads(first[1])["text"] == json.loads(second[1])["text"]
    # A different classmate, on the same question, and still one call out.
    assert len(calls) == 1
    assert json.loads(second[1])["cached"] is True


def test_a_student_may_ask_the_machine(ai, calls):
    """Not in ROLE_REQUIRED, and this is the test that says so.

    The rung on /explain exists because Anthropic bills for it. This one is
    free, and a student being able to use it is the point rather than an
    oversight -- so it is pinned here, where removing the line in
    ROLE_REQUIRED would be caught.
    """
    aiport, who, port = ai
    status, body, _ = write(port, who["Chan"], body="Is the sign right here?")
    qid = json.loads(body)["doubts"][0]["id"]
    assert call(aiport, "POST", "/doubts/ai", {"id": qid},
                cookie=who["Chan"])[0] == 200


def test_a_question_that_is_gone_is_not_answered(ai, calls):
    aiport, who, port = ai
    status, body, _ = write(port, who["Chan"], body="Taken back in a moment.")
    qid = json.loads(body)["doubts"][0]["id"]
    write(port, who["Chan"], id=qid, delete=True)

    status, body, _ = call(aiport, "POST", "/doubts/ai", {"id": qid},
                           cookie=who["Chan"])
    assert status == 404, body
    assert not calls          # and nothing was spent finding that out


def test_a_made_up_question_id_is_not_answered(ai, calls):
    aiport, who, _ = ai
    assert call(aiport, "POST", "/doubts/ai", {"id": "not-a-uuid"},
                cookie=who["Chan"])[0] == 404
    assert call(aiport, "POST", "/doubts/ai",
                {"id": "00000000-0000-0000-0000-000000000000"},
                cookie=who["Chan"])[0] == 404
    assert not calls


def test_an_answer_is_not_a_question(ai, calls):
    """You may ask the machine about a question, and not about an answer.

    The route reads `parent_id is null`, so an answer's id finds no row -- the
    bot arguing with a classmate under that classmate's own answer is not a
    thing this can be pointed at.
    """
    aiport, who, port = ai
    status, body, _ = write(port, who["Chan"], body="Where does the 2 come from?")
    qid = json.loads(body)["doubts"][0]["id"]
    status, body, _ = write(port, who["Dia"], parent=qid, body="From the balance.")
    aid = json.loads(body)["doubts"][0]["answers"][0]["id"]

    assert call(aiport, "POST", "/doubts/ai", {"id": aid},
                cookie=who["Chan"])[0] == 404
    assert not calls


def test_the_budget_stops_it(ai, calls):
    """Two calls is the whole run, and the third is refused rather than made."""
    aiport, who, port = ai
    ids = []
    for text in ("First distinct question.", "Second distinct question.",
                 "Third distinct question."):
        status, body, _ = write(port, who["Chan"], body=text)
        ids.append(json.loads(body)["doubts"][0]["id"])

    got = [call(aiport, "POST", "/doubts/ai", {"id": i}, cookie=who["Chan"])[0]
           for i in ids]
    assert got == [200, 200, 429]
    assert len(calls) == 2


def test_a_signed_out_caller_cannot_ask_the_machine(ai, calls):
    aiport, _, _ = ai
    assert call(aiport, "POST", "/doubts/ai", {"id": "x"})[0] in (302, 401, 403)
    assert not calls
