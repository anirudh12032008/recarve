"""Removing a file, for real -- the bug and the feature this session found.

The old "Remove" button flipped materials.status to 'removed' and stopped
there. Nothing ever read that column before listing a file: build_data globs
the uploads folder straight off disk, so the file stayed exactly as visible
and downloadable as it was before an admin pressed the button. And there was
no removal path for a lecture at all -- only a material could be reported, so
a bad recording had no way back.

These tests drive the real HTTP routes against a real (tmp_path) library, the
same way test_worker.py does, because the bug lived in the gap between "the
database says removed" and "the file is still on disk" -- a test that only
touches the database cannot see it.
"""

import json
import os
import threading
import urllib.error
import urllib.request

import psycopg
import pytest

import notes
from conftest import DB_URL, as_admin_connection, as_user
from test_content import member, upload_as_owner

SECRET = "a-cookie-secret-for-removal-tests"


def start(tmp_path):
    os.environ["RECARVE_SECRET"] = SECRET
    args = notes.argparse.Namespace(
        library=tmp_path / "library", out=tmp_path / "site" / "index.html",
        host="127.0.0.1", port=0, notes_model="claude-haiku-4-5", max_cost=1.0,
        max_explains=0, no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, args


def call(port, method, path, body=None, cookie=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {})
    if cookie:
        req.add_header("Cookie", f"{notes.SESSION_COOKIE}={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def cookie_for(uid):
    return notes.sign_session(uid, SECRET.encode())


def with_password(conn, uid):
    """member() leaves password null, which forces the set-password screen on
    every route -- correct for the app, unrelated to what these tests check."""
    conn.execute("update profiles set password = 'x' where id = %s", (uid,))
    return uid


# --------------------------------------------------------- db-level: materials

def test_removing_a_material_deletes_the_file_from_disk(db, tmp_path):
    """The bug, reproduced at the level it lived at: the row said removed and
    the file was still there. Now the disk copy has to actually be gone."""
    uploader = member(db)
    f = tmp_path / "handout.pdf"
    f.write_text("real content a classmate uploaded")
    mid = upload_as_owner(db, uploader, "handout.pdf", str(f))

    as_admin_connection(db)
    assert f.exists(), "sanity: the file is there before removal"
    notes.db_remove_material(db, mid)
    assert not f.exists(), "the file itself must be gone, not just the status"
    assert db.execute(
        "select status from materials where id = %s", (mid,)
    ).fetchone() == ("removed",), "the audit trail the report screen reads stays"


def test_removing_a_material_whose_file_is_already_gone_does_not_crash(db, tmp_path):
    """Disk and database can disagree -- a manual cleanup, a restore from
    backup missing one file. The row still gets marked; there is nothing else
    an admin pressing Remove a second time could be told."""
    uploader = member(db)
    missing = tmp_path / "already-deleted.pdf"
    mid = upload_as_owner(db, uploader, "already-deleted.pdf", str(missing))
    as_admin_connection(db)
    notes.db_remove_material(db, mid)  # must not raise
    assert db.execute(
        "select status from materials where id = %s", (mid,)
    ).fetchone() == ("removed",)


def test_removing_a_material_that_does_not_exist_is_a_clean_error(db):
    as_admin_connection(db)
    with pytest.raises(ValueError):
        notes.db_remove_material(db, "00000000-0000-0000-0000-000000000000")


# --------------------------------------------------------- db-level: lectures

def test_removing_a_lecture_deletes_its_row(db):
    admin = member(db, admin=True)
    as_user(db, admin)
    db.execute(
        "insert into lectures (subject_code, uploader_id, title, audio_key) "
        "values ('CY1107', %s, 'bad take', 'a1')", (admin,))
    notes.db_remove_lecture(db, "CY1107", "bad take")
    assert db.execute(
        "select count(*) from lectures where subject_code = 'CY1107' "
        "and title = 'bad take'").fetchone() == (0,)


def test_removing_a_lecture_that_does_not_exist_is_a_clean_error(db):
    admin = member(db, admin=True)
    as_user(db, admin)
    with pytest.raises(ValueError):
        notes.db_remove_lecture(db, "CY1107", "never recorded")


def test_removing_a_lecture_needs_a_real_subject(db):
    admin = member(db, admin=True)
    as_user(db, admin)
    with pytest.raises(ValueError):
        notes.db_remove_lecture(db, "NOPE9999", "anything")


def test_a_student_cannot_remove_someone_elses_lecture_through_the_database(db):
    """db_remove_lecture does not check the role itself -- the delete policy
    does, the same way db_remove_material leans on the update policy. A
    student's own connection must delete zero rows and the function must turn
    that into the same clean error a genuinely missing lecture gets."""
    owner = member(db)
    as_user(db, owner)
    db.execute(
        "insert into lectures (subject_code, uploader_id, title, audio_key) "
        "values ('CY1107', %s, 'someone elses', 'a2')", (owner,))
    as_admin_connection(db)
    thief = member(db, role="student")
    as_user(db, thief)
    with pytest.raises(ValueError):
        notes.db_remove_lecture(db, "CY1107", "someone elses")
    as_admin_connection(db)
    assert db.execute(
        "select count(*) from lectures where title = 'someone elses'"
    ).fetchone() == (1,), "the student's attempt must not have touched it"


# ------------------------------------------------------------- over the wire

@pytest.fixture
def server(tmp_path):
    srv, args = start(tmp_path)
    conn = psycopg.connect(DB_URL, autocommit=True)
    conn.execute("truncate auth.users cascade")
    try:
        yield srv, args, conn
    finally:
        srv.shutdown()
        conn.close()


def test_removing_a_lecture_over_the_wire_deletes_the_file_too(server):
    """The route an admin actually presses: the row goes, and so does the .md
    file build_data would otherwise keep reading forever."""
    srv, args, conn = server
    admin = with_password(conn, member(conn, admin=True))
    port = srv.server_address[1]

    md_dir = notes.subject_dir(args.library, "CY1107", "lectures")
    (md_dir / "a bad recording.md").write_text("# nonsense")
    conn.execute(
        "insert into lectures (subject_code, uploader_id, title, audio_key) "
        "values ('CY1107', %s, 'a bad recording', 'a3')", (admin,))

    status, body = call(port, "POST", "/remove-lecture",
                         {"subject": "CY1107", "title": "a bad recording"},
                         cookie=cookie_for(admin))
    assert status == 200, body
    assert not (md_dir / "a bad recording.md").exists(), \
        "the note itself must be gone, not just the database row"
    assert conn.execute(
        "select count(*) from lectures where title = 'a bad recording'"
    ).fetchone() == (0,)


def test_removing_a_material_over_the_wire(server):
    srv, args, conn = server
    admin = with_password(conn, member(conn, admin=True))
    port = srv.server_address[1]

    f = args.library / "handout.pdf"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("slides")
    mid = upload_as_owner(conn, admin, "handout.pdf", str(f))

    status, body = call(port, "POST", "/remove", {"id": str(mid)},
                         cookie=cookie_for(admin))
    assert status == 200, body
    assert not f.exists()


def test_a_student_cannot_reach_either_removal_route(server):
    srv, args, conn = server
    student = with_password(conn, member(conn, role="student"))
    port = srv.server_address[1]
    cookie = cookie_for(student)

    status, _ = call(port, "POST", "/remove", {"id": "x"}, cookie=cookie)
    assert status == 403
    status, _ = call(port, "POST", "/remove-lecture",
                      {"subject": "CY1107", "title": "x"}, cookie=cookie)
    assert status == 403


def test_a_trusted_member_who_is_not_admin_cannot_remove_either(server):
    """Uploading is trusted; taking something off the shelves stays admin,
    the same bar the reports queue already held materials to."""
    srv, args, conn = server
    trusted = with_password(conn, member(conn, role="trusted"))
    port = srv.server_address[1]
    cookie = cookie_for(trusted)

    status, _ = call(port, "POST", "/remove", {"id": "x"}, cookie=cookie)
    assert status == 403
    status, _ = call(port, "POST", "/remove-lecture",
                      {"subject": "CY1107", "title": "x"}, cookie=cookie)
    assert status == 403
