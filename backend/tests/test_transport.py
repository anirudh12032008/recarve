"""The transport: connections that are kept, and a principal that is remembered.

A hundred phones arrive in the same minute, every one of them polling /jobs
every two seconds. Two things in the layer under the app decide whether that is
a busy server or a broken one, and neither is visible from any screen:

    * whether a request costs a new TCP connection and a new thread, which is
      what HTTP/1.0 means -- and keeping the connection is only safe if every
      response says how long it is, so that is what is checked here;
    * whether a request costs a new Postgres connection, which is what reading
      the principal from the database every time meant.

Both are checked against the real server over a real socket, like test_worker,
because a transport bug is exactly the kind that a stand-in does not have.
"""

import http.client
import json
import os
import pathlib
import sys
import threading
import urllib.error
import urllib.request

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL  # noqa: E402

SECRET = "a-cookie-secret-for-transport-tests"


# --------------------------------------------------------------- plumbing


def wipe():
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("truncate auth.users cascade")
        conn.execute("delete from invites")
        conn.execute("insert into invites (code, expires_at) "
                     "values ('LETMEIN', now() + interval '1 day')")


def call(port, method, path, body=None, cookie=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {})
    if cookie:
        req.add_header("Cookie", f"{notes.SESSION_COOKIE}={cookie}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def join(port, name, roll):
    """A new member, and the cookie they leave with. Pending until approved."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/join", method="POST",
        data=json.dumps({"name": name, "roll_no": roll, "phone": "9876543210",
                         "code": "LETMEIN", "password": "a-real-password"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]


def id_of(roll):
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        return str(conn.execute("select id from profiles where roll_no = %s",
                                (roll,)).fetchone()[0])


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """A real server, its admin -- whoever joins first is one -- and a member."""
    os.environ["RECARVE_SECRET"] = SECRET
    os.environ["RECARVE_WORKER_TOKEN"] = "a-worker-token-for-transport-tests"
    lib = tmp_path_factory.mktemp("transport-library")
    wipe()
    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False, remote=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    yield {"port": port, "srv": srv, "admin": join(port, "Asha", "24U001")}
    srv.shutdown()
    srv.server_close()
    wipe()


def member(env, name, roll):
    """Somebody joined and approved, with an empty slate in the memory."""
    cookie = join(env["port"], name, roll)
    status, body = call(env["port"], "POST", "/approve", {"id": id_of(roll)},
                        cookie=env["admin"])
    assert status == 200, body
    return cookie


# ------------------------------------------------ one connection, many requests


def test_a_kept_connection_carries_the_length_of_everything_it_sends(env):
    """Four kinds of response down one socket, each saying how long it is.

    This is the whole risk of HTTP/1.1: a response without a Content-Length is
    a browser waiting for a body that has already arrived. The page itself goes
    through send_head, /data and /jobs through reply, and /sw.js through
    send_js -- four different senders, one connection, no reconnect.
    """
    conn = http.client.HTTPConnection("127.0.0.1", env["port"], timeout=20)
    conn.connect()
    first = conn.sock
    for path in ("/", "/data", "/jobs", "/sw.js"):
        conn.request("GET", path, headers={
            "Cookie": f"{notes.SESSION_COOKIE}={env['admin']}"})
        r = conn.getresponse()
        body = r.read()
        assert r.version == 11, f"{path} answered HTTP/1.0"
        assert r.getheader("Content-Length") == str(len(body)), \
            f"{path} said {r.getheader('Content-Length')} and sent {len(body)}"
        assert not r.will_close, f"{path} hung up"
        assert conn.sock is first, f"{path} needed a second connection"
    conn.close()


def test_a_refusal_says_how_long_it_is_and_then_hangs_up(env):
    """A 404 through the stdlib's own error path, on a kept connection."""
    conn = http.client.HTTPConnection("127.0.0.1", env["port"], timeout=20)
    conn.request("GET", "/no-such-note.html", headers={
        "Cookie": f"{notes.SESSION_COOKIE}={env['admin']}"})
    r = conn.getresponse()
    body = r.read()
    assert r.status == 404
    assert r.getheader("Content-Length") == str(len(body))
    conn.close()


def test_a_post_refused_before_its_body_was_read_ends_the_connection(env):
    """The one thing a kept connection cannot survive.

    Half the POST handlers answer --no-auth, admins-only or too-much before
    reading the body. Those bytes are still on the socket, and on a connection
    that is kept the next request line is read out of the middle of somebody's
    JSON. Every POST hangs up for that reason, and this is the check that says
    so -- the refusal must come back, and the connection must not be offered
    for another request.
    """
    them = member(env, "Rohit", "24U101")
    conn = http.client.HTTPConnection("127.0.0.1", env["port"], timeout=20)
    payload = json.dumps({"id": id_of("24U001")}).encode()
    conn.request("POST", "/approve", body=payload, headers={
        "Content-Type": "application/json",
        "Cookie": f"{notes.SESSION_COOKIE}={them}"})
    r = conn.getresponse()
    body = r.read()
    assert r.status == 403, body          # /approve is admins-only
    assert r.getheader("Content-Length") == str(len(body))
    assert r.will_close, "a refusal kept a connection with an unread body on it"
    conn.close()
