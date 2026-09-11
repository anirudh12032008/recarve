"""Several photos of one handout, uploaded together, under one title.

Before this, every file was its own upload with no way to say two of them
belonged together -- a phone photographing eight pages of a handout produced
eight rows named after whatever the camera called each one. batch_id and
title are both nullable on materials and touched by nothing until an upload
actually asks for them, so every test in test_content.py that predates this
still describes the truth for a lone upload.
"""

import uuid

import psycopg
import pytest

import notes
from conftest import DB_URL, as_admin_connection
from test_content import member
from test_removal import cookie_for, start, with_password


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


def send_file(port, cookie, name, subject, body=b"x", batch=None, title=None):
    import urllib.error
    import urllib.request

    headers = {"X-Filename": name, "X-Subject": subject,
               "Content-Type": "application/octet-stream"}
    if batch:
        headers["X-Batch"] = batch
    if title:
        headers["X-Title"] = title
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/upload", method="POST", data=body, headers=headers)
    req.add_header("Cookie", f"{notes.SESSION_COOKIE}={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_two_files_uploaded_with_the_same_batch_share_it_in_the_database(server):
    srv, args, conn = server
    uploader = with_password(conn, member(conn, role="trusted"))
    port = srv.server_address[1]
    cookie = cookie_for(uploader)
    batch = str(uuid.uuid4())

    for name in ("page1.jpg", "page2.jpg"):
        status, body = send_file(port, cookie, name, "MC1101",
                                  batch=batch, title="Unit 3 handout")
        assert status == 200, body

    rows = conn.execute(
        "select filename, batch_id, title from materials "
        "where filename in ('page1.jpg', 'page2.jpg') order by filename"
    ).fetchall()
    assert len(rows) == 2
    for filename, batch_id, title in rows:
        assert str(batch_id) == batch
        assert title == "Unit 3 handout"


def test_an_upload_with_no_batch_gets_neither_column_set(server):
    """The overwhelmingly common case -- one file, no batch header at all --
    must come through exactly as it always has."""
    srv, args, conn = server
    uploader = with_password(conn, member(conn, role="trusted"))
    port = srv.server_address[1]

    status, body = send_file(port, cookie_for(uploader), "solo.pdf", "MC1101")
    assert status == 200, body
    row = conn.execute(
        "select batch_id, title from materials where filename = 'solo.pdf'"
    ).fetchone()
    assert row == (None, None)


def test_a_malformed_batch_header_is_refused_before_it_reaches_the_database(server):
    srv, args, conn = server
    uploader = with_password(conn, member(conn, role="trusted"))
    port = srv.server_address[1]

    status, body = send_file(port, cookie_for(uploader), "x.pdf", "MC1101",
                              batch="not-a-real-uuid")
    assert status == 400, body
    assert conn.execute(
        "select count(*) from materials where filename = 'x.pdf'"
    ).fetchone() == (0,), "a rejected batch id must not leave a half-written row"


def test_a_student_still_cannot_upload_at_all_batch_or_not(server):
    """The role gate does not move for this: adding is trusted, whatever
    headers ride along with the request."""
    srv, args, conn = server
    student = with_password(conn, member(conn, role="student"))
    port = srv.server_address[1]

    status, body = send_file(port, cookie_for(student), "x.pdf", "MC1101",
                              batch=str(uuid.uuid4()), title="sneaky")
    assert status == 403, body


def test_db_meta_folds_the_batch_and_title_onto_each_file(db):
    uploader = member(db)
    as_admin_connection(db)
    batch = str(uuid.uuid4())
    for name in ("a.jpg", "b.jpg"):
        db.execute(
            "insert into materials (subject_code, uploader_id, filename, "
            "file_key, size_bytes, batch_id, title) "
            "values ('CY1107', %s, %s, %s, 10, %s, 'Together')",
            (uploader, name, name, batch))

    mats, _ = notes.db_meta(db, uploader)
    a, b = mats[("CY1107", "a.jpg")], mats[("CY1107", "b.jpg")]
    assert a["batch"] == b["batch"] == batch
    assert a["title"] == b["title"] == "Together"
