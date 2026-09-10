"""The worker API, over a real socket, against the real database.

The point of this split is that the machine serving the site is not the machine
with the GPU. That makes a second kind of credential -- a shared token, held by
a process on somebody's laptop -- and a second way into the queue. Both are
worth exactly what they do in the server that actually runs, so every test here
starts notes.py's own server and talks HTTP to it.

The two credentials must not touch. A session cookie is a person and buys the
library; a worker token is a machine and buys the queue. Neither may be
mistaken for the other, and that is what the first two sections check.
"""

import http.client
import json
import os
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL  # noqa: E402

TOKEN = "a-worker-token-for-tests"
SECRET = "a-cookie-secret-for-worker-tests"
AUDIO = b"ID3\x04\x00" + b"not really audio, but bytes are bytes" * 20


# --------------------------------------------------------------- plumbing


def wipe():
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("votes", "reports", "lectures", "materials", "timetable",
                      "profiles", "invites"):
            try:
                conn.execute(f"delete from {table}")
            except psycopg.Error:
                pass
        conn.execute("delete from auth.users")
        conn.execute("insert into invites (code, expires_at) "
                     "values ('LETMEIN', now() + interval '1 day')")


def start(lib, remote):
    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False, remote=remote,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(port, method, path, body=None, cookie=None, token=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {})
    if cookie:
        req.add_header("Cookie", f"{notes.SESSION_COOKIE}={cookie}")
    if token is not None:
        req.add_header("X-Worker-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def js(reply):
    status, body, _ = reply
    return status, json.loads(body or b"{}")


def join(port, name, roll):
    status, body, headers = call(port, "POST", "/join", {
        "name": name, "roll_no": roll, "phone": "9876543210",
        "code": "LETMEIN", "password": "a-real-password"})
    assert status == 200, body
    return headers["Set-Cookie"].split(";")[0].split("=", 1)[1]


def send(port, cookie, name, data, subject="MC1101", declared=None):
    """One upload, with a Content-Length we can lie about.

    Raw http.client rather than urllib because the over-size tests must send
    the header and never the body: proving the refusal arrives before the
    hundred megabytes do is the whole point of checking the length first.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.putrequest("POST", "/upload")
    conn.putheader("X-Filename", name)
    conn.putheader("X-Subject", subject)
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("Content-Length", str(len(data) if declared is None else declared))
    conn.putheader("Cookie", f"{notes.SESSION_COOKIE}={cookie}")
    conn.endheaders()
    if declared is None:
        conn.send(data)
    try:
        r = conn.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"))
    finally:
        conn.close()
    return out


def status_of(lecture_id):
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        return conn.execute("select status, attempts, error from lectures where id = %s",
                            (lecture_id,)).fetchone()


def jobs_of(env):
    return js(call(env["port"], "GET", "/jobs", cookie=env["admin"]))[1]["jobs"]


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    """A server that hands its queue out, plus the admin who fills it."""
    os.environ["RECARVE_SECRET"] = SECRET
    os.environ["RECARVE_WORKER_TOKEN"] = TOKEN
    lib = tmp_path_factory.mktemp("remote-library")
    wipe()
    srv = start(lib, remote=True)
    port = srv.server_address[1]
    got = {"port": port, "lib": lib, "admin": join(port, "Asha", "24U001")}
    yield got
    srv.shutdown()
    srv.server_close()
    wipe()


def claim(port, token=TOKEN):
    return js(call(port, "POST", "/worker/claim", {}, token=token))


# ------------------------------------------------- the token, and only it

WORKER_ROUTES = [("POST", "/worker/claim"), ("GET", "/worker/audio?id=x"),
                 ("POST", "/worker/progress"), ("POST", "/worker/done"),
                 ("POST", "/worker/failed")]


@pytest.mark.parametrize("method,path", WORKER_ROUTES)
def test_no_token_reaches_nothing(remote, method, path):
    status, _ = js(call(remote["port"], method, path,
                        {} if method == "POST" else None))
    assert status == 403, f"{method} {path} answered {status}"


@pytest.mark.parametrize("method,path", WORKER_ROUTES)
def test_a_wrong_token_is_refused_exactly_as_no_token_is(remote, method, path):
    """Two different refusals would tell a guesser when they were getting close."""
    blank = js(call(remote["port"], method, path,
                    {} if method == "POST" else None))
    wrong = js(call(remote["port"], method, path,
                    {} if method == "POST" else None, token=TOKEN[:-1] + "x"))
    assert wrong[0] == 403 and wrong == blank, f"{method} {path}: {wrong} vs {blank}"


@pytest.mark.parametrize("role", ["student", "trusted", "admin"])
@pytest.mark.parametrize("method,path", WORKER_ROUTES)
def test_a_member_session_is_not_a_worker(remote, role, method, path):
    """Every role, the admin included -- the most privileged session there is.

    The gate settles a worker path before it ever reads a cookie, so this is
    the same 403 a stranger gets. If an admin cannot reach the queue, no
    session can.
    """
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("update profiles set role = %s where name = 'Asha'", (role,))
    try:
        status, _ = js(call(remote["port"], method, path,
                            {} if method == "POST" else None, cookie=remote["admin"]))
        assert status == 403, f"a {role} session reached {method} {path}"
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("update profiles set role = 'admin' where name = 'Asha'")


def test_a_worker_token_is_not_a_session(remote):
    """The other direction: the token opens the queue, not the library."""
    for method, path in [("GET", "/data"), ("GET", "/jobs"), ("GET", "/me"),
                         ("GET", "/pending"), ("POST", "/upload"), ("POST", "/explain")]:
        status, _ = js(call(remote["port"], method, path,
                            {} if method == "POST" else None, token=TOKEN))
        assert status == 403, f"the worker token reached {method} {path}"


# ----------------------------------------------------- claim and download


def test_a_worker_claims_downloads_and_reports(remote):
    port, lib = remote["port"], remote["lib"]
    status, body = send(port, remote["admin"], "week1.m4a", AUDIO)
    assert status == 200, body

    status, job = claim(port)
    assert status == 200 and job
    assert job["subject"] == "MC1101"
    assert job["name"] == "MC1101-week1.m4a"
    assert job["attempts"] == 1

    code, audio, headers = call(port, "GET", f"/worker/audio?id={job['id']}", token=TOKEN)
    assert code == 200 and audio == AUDIO, "the bytes must survive the round trip"
    assert headers["X-Filename"] == "MC1101-week1.m4a"

    # Progress is what keeps the phone's job list alive while the Mac works.
    assert js(call(port, "POST", "/worker/progress",
                   {"id": job["id"], "detail": "42% · 5 of 12 min · ~7m00s left"},
                   token=TOKEN))[0] == 200
    running = jobs_of(remote)[0]
    assert running["state"] == "transcribing" and "42%" in running["detail"]

    status, body = js(call(port, "POST", "/worker/done", {
        "id": job["id"], "transcript": "[00:00] limits and continuity",
        "notes": "## Summary\n\nwhat a limit is\n"}, token=TOKEN))
    assert status == 200, body

    # Stored exactly where process() would have put it, which is the only
    # place the library ever looks.
    md = lib / "MC1101-Mathematics-1" / "lectures" / "MC1101-week1.md"
    assert md.exists(), "the notes are not where the app reads them"
    text = md.read_text()
    assert "what a limit is" in text
    assert "limits and continuity" in text, "the transcript rides along in the note"
    assert (lib / ".transcripts" / "MC1101-week1.txt").exists()
    data = js(call(port, "GET", "/data", cookie=remote["admin"]))[1]
    titles = [n["title"] for s in data["subjects"] if s["code"] == "MC1101"
              for n in s["notes"]]
    assert "MC1101-week1" in titles, f"the page does not list it: {titles}"

    # And the audio is gone: once the notes exist it is the biggest thing on
    # the disk and nobody will ever open it again.
    assert list((lib / ".inbox").glob("*")) == [], "the audio outlived its notes"
    assert status_of(job["id"])[0] == "done"
    assert jobs_of(remote)[0]["state"] == "done"


def test_an_empty_queue_answers_nothing_rather_than_waiting(remote):
    assert claim(remote["port"]) == (200, {})


def test_two_workers_never_get_the_same_lecture(remote):
    """claim_lecture()'s `for update skip locked` is the whole mechanism."""
    port = remote["port"]
    for i in range(4):
        assert send(port, remote["admin"], f"race{i}.m4a", AUDIO)[0] == 200

    got, lock = [], threading.Lock()
    ready = threading.Barrier(10)

    def grab():
        ready.wait()          # all ten ask at the same instant
        for _ in range(5):
            try:
                _, job = claim(port)
                break
            except urllib.error.URLError:
                # Ten simultaneous connections outrun the listen backlog; a
                # refused connection is the socket, not the queue.
                time.sleep(0.1)
        else:
            return
        if job:
            with lock:
                got.append(job["id"])

    threads = [threading.Thread(target=grab) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(got) == 4, f"four lectures queued, {len(got)} claims came back"
    assert len(set(got)) == len(got), f"the same lecture went out twice: {got}"

    # Leave the queue as it was found.
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("delete from lectures")
    for leftover in (remote["lib"] / ".inbox").glob("*"):
        leftover.unlink()


# -------------------------------------------------- failure and giving up


def test_a_failure_is_retried_and_then_given_up_on(remote):
    port, lib = remote["port"], remote["lib"]
    assert send(port, remote["admin"], "broken.m4a", AUDIO)[0] == 200
    audio = lib / ".inbox" / "MC1101-broken.m4a"

    for attempt in (1, 2):
        _, job = claim(port)
        assert job and job["attempts"] == attempt
        assert js(call(port, "POST", "/worker/failed",
                       {"id": job["id"], "error": "ffmpeg could not read it"},
                       token=TOKEN)) == (200, {"retrying": True})
        state, attempts, error = status_of(job["id"])
        assert (state, attempts) == ("queued", attempt), "it must go back on the queue"
        assert error == "ffmpeg could not read it"
        assert audio.exists(), "a lecture still to be retried keeps its audio"
        # The uploader sees the reason, not a job that silently stopped moving.
        shown = jobs_of(remote)[0]
        assert "ffmpeg could not read it" in shown["detail"]
        assert "retrying" in shown["detail"]

    _, job = claim(port)
    assert job and job["attempts"] == notes.MAX_ATTEMPTS
    assert js(call(port, "POST", "/worker/failed",
                   {"id": job["id"], "error": "ffmpeg could not read it"},
                   token=TOKEN)) == (200, {"retrying": False})
    assert status_of(job["id"])[0] == "failed"
    assert claim(port) == (200, {}), "a lecture given up on must not come back"
    # A file nothing can read may not pin a hundred megabytes of disk forever.
    assert not audio.exists(), "the audio of a permanently failed lecture stayed"
    shown = jobs_of(remote)[0]
    assert shown["state"] == "failed" and "ffmpeg" in shown["detail"]


def test_a_lecture_whose_audio_vanished_is_not_handed_out(remote):
    """Otherwise every worker in turn downloads a 404 and reports the same
    failure, three times over, before the queue lets go of it."""
    port, lib = remote["port"], remote["lib"]
    assert send(port, remote["admin"], "ghost.m4a", AUDIO)[0] == 200
    (lib / ".inbox" / "MC1101-ghost.m4a").unlink()
    assert claim(port) == (200, {})
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        state, error = conn.execute(
            "select status, error from lectures where title = 'MC1101-ghost'").fetchone()
    assert state == "failed" and "no longer on the server" in error


# ----------------------------------------------------------- upload limits


def test_an_oversize_recording_is_refused_before_it_is_sent(remote):
    over = notes.MAX_AUDIO_BYTES + 1
    status, body = send(remote["port"], remote["admin"], "huge.m4a", b"", declared=over)
    assert status == 413, body
    assert "the limit for a recording is 100.0 MB" in body["error"], body
    assert "huge.m4a is 100.1 MB" in body["error"], (
        "one byte over must not read back as exactly the limit: " + body["error"])
    assert body["limit"] == notes.MAX_AUDIO_BYTES and body["size"] == over
    assert list((remote["lib"] / ".inbox").glob("huge*")) == [], "a refusal left a file"


def test_an_oversize_document_is_refused_with_its_own_smaller_limit(remote):
    over = notes.MAX_DOC_BYTES + 1
    status, body = send(remote["port"], remote["admin"], "huge.pdf", b"", declared=over)
    assert status == 413, body
    assert "the limit for notes and slides is 25.0 MB" in body["error"], body
    assert "huge.pdf is 25.1 MB" in body["error"], body["error"]
    assert body["limit"] == notes.MAX_DOC_BYTES
    # A document the size of a recording is still refused: the caps are per kind.
    assert send(remote["port"], remote["admin"], "big.pdf", b"",
                declared=notes.MAX_AUDIO_BYTES)[0] == 413


def test_a_document_under_its_limit_still_goes_through(remote):
    status, body = send(remote["port"], remote["admin"], "slides.pdf", b"%PDF-1.4 small")
    assert status == 200, body
    assert (remote["lib"] / "MC1101-Mathematics-1" / "uploads" / "slides.pdf").exists()


def test_the_page_shows_the_same_limits_it_enforces():
    """A limit nobody is told about is a limit discovered by failing at it."""
    assert f"up to {notes.MAX_AUDIO_BYTES // 1048576} MB" in notes.PAGE
    assert f"up to {notes.MAX_DOC_BYTES // 1048576} MB" in notes.PAGE
    assert "__AUDIO_MB__" not in notes.PAGE and "__DOC_MB__" not in notes.PAGE


# ------------------------------------------------ the single-machine setup


@pytest.fixture(scope="module")
def local(tmp_path_factory):
    """A server that still does its own transcribing, which is what is live.

    Whisper and the Claude API are the two things a test cannot really run, so
    both are stood in for. Everything between them -- the queue, the thread,
    process(), where the file lands, the row that says it is done, the audio
    being deleted -- is the real thing.
    """
    os.environ["RECARVE_SECRET"] = SECRET
    os.environ["RECARVE_WORKER_TOKEN"] = TOKEN

    class Usage:
        input_tokens, output_tokens = 10, 20

    def fake_transcribe(path, model, language, verbose=True, checkpoint=None,
                        on_progress=None):
        if on_progress:
            on_progress(60, 120, "en")
        return [(0.0, "the local queue still works")], ["en"]

    real = notes.transcribe, notes.make_notes
    notes.transcribe = fake_transcribe
    notes.make_notes = lambda transcript, lang, model, context=(): (
        "## Summary\n\nmade on this machine\n", Usage())

    lib = tmp_path_factory.mktemp("local-library")
    wipe()
    srv = start(lib, remote=False)
    port = srv.server_address[1]
    got = {"port": port, "lib": lib, "admin": join(port, "Asha", "24U001")}
    yield got
    srv.shutdown()
    srv.server_close()
    notes.transcribe, notes.make_notes = real
    wipe()


def test_the_in_process_queue_still_works_end_to_end(local):
    port, lib = local["port"], local["lib"]
    status, body = send(port, local["admin"], "monday.m4a", AUDIO)
    assert status == 200, body

    for _ in range(200):
        jobs = jobs_of(local)
        if jobs and jobs[0]["state"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert jobs[0]["state"] == "done", jobs[0]

    md = lib / "MC1101-Mathematics-1" / "lectures" / "MC1101-monday.md"
    assert md.exists(), "the local path wrote nothing"
    assert "made on this machine" in md.read_text()
    assert not (lib / ".inbox" / "MC1101-monday.m4a").exists(), "the audio was kept"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute("select status from lectures where title = 'MC1101-monday'"
                            ).fetchone()[0] == "done"


def test_a_worker_may_not_take_work_from_a_server_doing_its_own(local):
    """Two consumers of one queue would transcribe the same lecture twice."""
    status, body = claim(local["port"])
    assert status == 503 and "runs its own queue" in body["error"]
