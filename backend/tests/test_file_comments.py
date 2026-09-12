"""Comments on an uploaded file -- which is the doubts thread, pointed at a file.

The request was "a comment section for the notes". A lecture note already has
one: 0025 gave it a thread of the section's own words, votable, two deep and
hideable, and calling it Doubts is a naming decision and not a missing feature.
What genuinely had nowhere to be said was anything about an UPLOADED file -- a
PDF of last year's paper, a photo of the board -- and 0036 is one nullable
column on the table that already does this, not a second table shaped like it.

So these tests are mostly about the thing that breaks when a table grows a
second thread key: whether the three threads stay apart.
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
from test_content import member, upload_as_owner  # noqa: E402


def a_file(db, uploader, name="last-years-paper.pdf"):
    mid = str(upload_as_owner(db, uploader, name, "k-" + name))
    as_user(db, uploader)
    return mid


def a_lecture(db, uploader, title="CY1107-week1"):
    as_admin_connection(db)
    lid = db.execute(
        "insert into lectures (subject_code, uploader_id, title, audio_key, status) "
        "values ('CY1107', %s, %s, %s, 'done') returning id",
        (uploader, title, "k-" + title)).fetchone()[0]
    as_user(db, uploader)
    return str(lid)


def test_a_comment_on_an_uploaded_file(db):
    me, reader = member(db), member(db)
    mid = a_file(db, me)
    as_user(db, reader)
    notes.db_ask(db, reader, "CY1107", None, None, "Page 3 is the 2019 paper.",
                 mid)
    [c] = notes.db_doubts(db, reader, "CY1107", material_id=mid)
    assert (c["body"], c["by"], c["mine"]) == \
        ("Page 3 is the 2019 paper.", "M", True)


def test_the_answers_under_it_are_the_same_answers(db):
    """Nothing about a file's thread is new. Two deep, votable, sorted by what
    the class thought -- because it is the same rows and the same code."""
    me, helper = member(db), member(db)
    mid = a_file(db, me)
    as_user(db, me)
    qid = notes.db_ask(db, me, "CY1107", None, None, "Which year is this?", mid)
    as_user(db, helper)
    aid = notes.db_ask(db, helper, "CY1107", None, qid, "2019.")
    notes.db_ask(db, helper, "CY1107", None, qid, "No idea.")
    as_user(db, me)
    notes.db_vote(db, aid, me, True, "doubt_id")

    [c] = notes.db_doubts(db, me, "CY1107", material_id=mid)
    assert [a["body"] for a in c["answers"]] == ["2019.", "No idea."], \
        "the class's answer first"


def test_a_files_thread_is_not_the_subjects_thread(db):
    me = member(db)
    mid = a_file(db, me)
    as_user(db, me)
    notes.db_ask(db, me, "CY1107", None, None, "about the file", mid)
    notes.db_ask(db, me, "CY1107", None, None, "about the subject")

    assert [q["body"] for q in notes.db_doubts(db, me, "CY1107", material_id=mid)] \
        == ["about the file"]
    assert [q["body"] for q in notes.db_doubts(db, me, "CY1107")] \
        == ["about the subject"]


def test_a_files_thread_is_not_a_lectures_thread(db):
    me = member(db)
    mid, lid = a_file(db, me), a_lecture(db, me)
    as_user(db, me)
    notes.db_ask(db, me, "CY1107", None, None, "about the file", mid)
    notes.db_ask(db, me, "CY1107", lid, None, "about the lecture")

    assert [q["body"] for q in notes.db_doubts(db, me, "CY1107", material_id=mid)] \
        == ["about the file"]
    assert [q["body"] for q in notes.db_doubts(db, me, "CY1107", lid)] \
        == ["about the lecture"]


def test_a_second_file_has_a_thread_of_its_own(db):
    me = member(db)
    one, two = a_file(db, me, "a.pdf"), a_file(db, me, "b.pdf")
    as_user(db, me)
    notes.db_ask(db, me, "CY1107", None, None, "about a", one)
    assert notes.db_doubts(db, me, "CY1107", material_id=two) == []


def test_a_question_cannot_be_about_a_lecture_and_a_file_at_once(db):
    me = member(db)
    mid, lid = a_file(db, me), a_lecture(db, me)
    as_user(db, me)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "insert into doubts (subject_code, lecture_id, material_id, author_id, "
            "body) values ('CY1107', %s, %s, %s, 'both')", (lid, mid, me))
    db.rollback()


def test_an_answer_carries_no_file(db):
    """An answer belongs to its question and the question says which thread
    this is -- the same rule 0025 wrote for lectures."""
    me = member(db)
    mid = a_file(db, me)
    as_user(db, me)
    qid = notes.db_ask(db, me, "CY1107", None, None, "q", mid)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into doubts (parent_id, material_id, author_id, body) "
                   "values (%s, %s, %s, 'a')", (qid, mid, me))
    db.rollback()


def test_db_ask_drops_the_file_from_an_answer(db):
    """The check above is the referee; this is nothing ever reaching it."""
    me = member(db)
    mid = a_file(db, me)
    as_user(db, me)
    qid = notes.db_ask(db, me, "CY1107", None, None, "q", mid)
    notes.db_ask(db, me, "CY1107", None, qid, "a", mid)
    assert len(notes.db_doubts(db, me, "CY1107", material_id=mid)[0]["answers"]) == 1


def test_hiding_a_files_question_takes_its_answers_with_it(db):
    me, helper = member(db), member(db)
    mid = a_file(db, me)
    as_user(db, me)
    qid = notes.db_ask(db, me, "CY1107", None, None, "q", mid)
    as_user(db, helper)
    notes.db_ask(db, helper, "CY1107", None, qid, "a")
    as_user(db, me)
    notes.db_hide_doubt(db, qid)
    assert notes.db_doubts(db, helper, "CY1107", material_id=mid) == []


def test_a_removed_file_takes_its_thread_with_it(db):
    """on delete cascade, so a comment cannot outlive the thing it is about."""
    me = member(db)
    mid = a_file(db, me)
    as_user(db, me)
    notes.db_ask(db, me, "CY1107", None, None, "about it", mid)
    as_admin_connection(db)
    db.execute("delete from materials where id = %s", (mid,))
    assert db.execute("select count(*) from doubts").fetchone()[0] == 0


# ---------------------------------------------------- the real server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    os.environ["RECARVE_SECRET"] = SECRET.decode()
    lib = tmp_path_factory.mktemp("library")

    people, ids = {}, {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("votes", "doubts", "materials", "lectures", "profiles"):
            conn.execute(f"delete from {table}")
        for name, role in (("Asha", "admin"), ("Chan", "student")):
            uid = make_user(conn)
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role, password) "
                "values (%s, %s, %s, 'approved', %s, 'a-real-password')",
                (uid, name, name, role))
            people[name] = uid
        ids["file"] = str(conn.execute(
            "insert into materials (subject_code, uploader_id, filename, file_key, "
            "size_bytes) values ('CY1107', %s, 'paper.pdf', 'k1', 10) returning id",
            (people["Asha"],)).fetchone()[0])

    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], ids, {n: notes.sign_session(i, SECRET)
                                       for n, i in people.items()}

    srv.shutdown()
    srv.server_close()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("votes", "doubts", "materials", "profiles", "auth.users"):
            conn.execute(f"delete from {table}")


def test_a_file_names_its_own_subject_over_http(server):
    """The phone sends an id and nothing else: a material row already says
    which subject it is in, and a second fact off the phone is a second fact
    that can disagree with the first."""
    port, ids, who = server
    status, body, _ = call(port, "POST", "/doubts",
                           {"material": ids["file"], "body": "Is this the 2019 one?"},
                           cookie=who["Chan"])
    assert status == 200, body
    assert [q["body"] for q in json.loads(body)["doubts"]] == ["Is this the 2019 one?"]

    status, body, _ = call(port, "GET", f"/doubts?material={ids['file']}",
                           cookie=who["Asha"])
    assert status == 200
    assert [q["body"] for q in json.loads(body)["doubts"]] == ["Is this the 2019 one?"]


def test_a_made_up_file_is_refused(server):
    port, _, who = server
    for bad in ("not-a-uuid", "99999999-9999-9999-9999-999999999999"):
        status, body, _ = call(port, "GET", f"/doubts?material={bad}",
                               cookie=who["Chan"])
        assert status == 400, body
        assert "no such file" in json.loads(body)["error"]
