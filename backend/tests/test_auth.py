"""The web server's gate, against the real database.

Two halves. The first drives notes.py's auth functions directly on a
transaction that rolls back, the way the rest of this suite works. The second
starts the actual server on a real socket and talks HTTP to it, because a gate
is only worth what it does in the server that actually runs -- every previous
version of this file that tested a stand-in handler would have passed while the
real one served the library to anyone who asked.
"""

import json
import os
import pathlib
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL, as_admin_connection, as_user, make_user  # noqa: E402

SECRET = b"test-secret-not-the-real-one"


def make_invite(conn, code="OPENSESAME", max_uses=200, expires="1 day"):
    conn.execute(
        f"insert into invites (code, expires_at, max_uses) "
        f"values (%s, now() + interval '{expires}', %s)",
        (code, max_uses),
    )
    return code


# --------------------------------------------------------------- cookies


def test_signed_cookie_round_trips():
    cookie = notes.sign_session("11111111-1111-1111-1111-111111111111", SECRET)
    assert notes.unsign_session(cookie, SECRET) == "11111111-1111-1111-1111-111111111111"


def test_cookie_naming_someone_else_is_refused():
    """The id is readable. Swapping it for another one must not verify."""
    mine = notes.sign_session("11111111-1111-1111-1111-111111111111", SECRET)
    tag = mine.split(".", 1)[1]
    forged = f"22222222-2222-2222-2222-222222222222.{tag}"
    assert notes.unsign_session(forged, SECRET) is None


@pytest.mark.parametrize(
    "bad",
    ["", "no-dot", "11111111-1111-1111-1111-111111111111.", ".abc",
     "11111111-1111-1111-1111-111111111111.deadbeef"],
)
def test_junk_cookies_are_refused(bad):
    assert notes.unsign_session(bad, SECRET) is None


def test_a_cookie_signed_with_another_secret_is_refused():
    assert notes.unsign_session(notes.sign_session("abc", b"other"), SECRET) is None


def test_secret_is_generated_once_and_reused(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=x")
    monkeypatch.delenv("RECARVE_SECRET", raising=False)
    first = notes.session_secret(env)
    assert len(first) >= 32
    assert "RECARVE_SECRET=" in env.read_text()
    assert "ANTHROPIC_API_KEY=x" in env.read_text(), "must append, not overwrite"
    assert notes.session_secret(env) == first, "a restart must not log everyone out"


# ------------------------------------------------------------------ join


def test_first_joiner_becomes_the_admin(db):
    db.execute("delete from profiles")  # an empty class, as on day one
    make_invite(db)
    user_id, status, is_admin = notes.db_join(db, "OPENSESAME", "Asha", "24U001")
    assert (status, is_admin) == ("approved", True), "nobody could ever approve anybody"


def test_the_first_joiner_can_publish_from_the_first_upload(db):
    """Admin is not enough. An untrusted uploader's files are forced pending by
    the materials trigger, and the one person nobody can ever approve is the
    admin -- so their whole library would be invisible to the class."""
    db.execute("delete from profiles")
    make_invite(db)
    user_id, _, _ = notes.db_join(db, "OPENSESAME", "Asha", "24U001")
    as_user(db, user_id)
    assert db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, "
        "size_bytes) values ('CY1107', %s, 'a.pdf', 'k', 10) returning status",
        (user_id,),
    ).fetchone()[0] == "visible"


def test_everybody_after_the_first_waits(db):
    db.execute("delete from profiles")
    make_invite(db)
    notes.db_join(db, "OPENSESAME", "Asha", "24U001")
    _, status, is_admin = notes.db_join(db, "OPENSESAME", "Bilal", "24U002")
    assert (status, is_admin) == ("pending", False)


def test_a_bad_code_leaves_nothing_behind(db):
    db.execute("delete from profiles")
    make_invite(db)
    before = db.execute("select count(*) from auth.users").fetchone()[0]
    assert notes.db_join(db, "WRONG", "Mallory", "24U999") is None
    after = db.execute("select count(*) from auth.users").fetchone()[0]
    assert after == before, "a failed join must not leave an auth row"
    assert db.execute("select uses from invites where code = 'OPENSESAME'").fetchone()[0] == 0


def test_an_expired_or_exhausted_code_does_not_let_anyone_in(db):
    db.execute("delete from profiles")
    make_invite(db, "STALE", expires="-1 day")
    make_invite(db, "USEDUP", max_uses=1)
    db.execute("update invites set uses = 1 where code = 'USEDUP'")
    assert notes.db_join(db, "STALE", "A", "24U101") is None
    assert notes.db_join(db, "USEDUP", "B", "24U102") is None


def test_two_people_cannot_take_the_same_roll_number(db):
    db.execute("delete from profiles")
    make_invite(db)
    notes.db_join(db, "OPENSESAME", "Asha", "24U001")
    with pytest.raises(psycopg.errors.UniqueViolation):
        notes.db_join(db, "OPENSESAME", "Imposter", "24U001")


# ------------------------------------------------------------- principal


def test_a_session_reads_back_as_its_owner(db):
    db.execute("delete from profiles")
    make_invite(db)
    user_id, _, _ = notes.db_join(db, "OPENSESAME", "Asha", "24U001")
    as_admin_connection(db)
    who = notes.db_principal(db, user_id)
    assert who["name"] == "Asha" and who["status"] == "approved" and who["admin"]


def test_a_cookie_for_a_deleted_profile_is_nobody(db):
    assert notes.db_principal(db, str(uuid.uuid4())) is None


def test_a_cookie_that_is_not_a_uuid_is_nobody(db):
    """Only reachable with the signing key, but it must not raise if it happens."""
    assert notes.db_principal(db, "not-a-uuid") is None


def test_status_is_read_fresh_so_blocking_takes_effect_at_once(db):
    db.execute("delete from profiles")
    make_invite(db)
    user_id, _, _ = notes.db_join(db, "OPENSESAME", "Asha", "24U001")
    as_admin_connection(db)
    db.execute("update profiles set status = 'blocked' where id = %s", (user_id,))
    assert notes.db_principal(db, user_id)["status"] == "blocked"


# --------------------------------------------------------------- approve


def test_approving_lets_someone_in_and_publishes_what_they_uploaded(db):
    db.execute("delete from profiles")
    make_invite(db)
    admin_id, _, _ = notes.db_join(db, "OPENSESAME", "Asha", "24U001")
    joiner_id, _, _ = notes.db_join(db, "OPENSESAME", "Bilal", "24U002")

    as_admin_connection(db)
    db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('MC1101', %s, 'unit1.pdf', 'k', 10)",
        (joiner_id,),
    )
    assert db.execute("select status from materials where uploader_id = %s",
                      (joiner_id,)).fetchone()[0] == "pending"

    as_user(db, admin_id)
    assert notes.db_pending(db) == [{"id": joiner_id, "name": "Bilal", "roll_no": "24U002"}]
    published = notes.db_approve(db, joiner_id)

    as_admin_connection(db)
    assert published == 1
    row = db.execute(
        "select p.status, p.trusted, m.status from profiles p join materials m "
        "on m.uploader_id = p.id where p.id = %s", (joiner_id,)).fetchone()
    assert row == ("approved", True, "visible")


def test_a_member_cannot_approve_anybody(db):
    db.execute("delete from profiles")
    make_invite(db)
    notes.db_join(db, "OPENSESAME", "Asha", "24U001")          # the admin
    member_id, _, _ = notes.db_join(db, "OPENSESAME", "Bilal", "24U002")
    victim_id, _, _ = notes.db_join(db, "OPENSESAME", "Chan", "24U003")
    as_admin_connection(db)
    db.execute("update profiles set status = 'approved' where id = %s", (member_id,))

    as_user(db, member_id)
    with pytest.raises(psycopg.errors.RaiseException):
        notes.db_approve(db, victim_id)


def test_bootstrap_offers_a_code_only_while_the_class_is_empty(db):
    db.execute("delete from profiles")
    db.execute("delete from invites")
    code = notes.db_bootstrap(db)
    assert code and notes.db_bootstrap(db) == code, "a restart must not change the code"

    notes.db_join(db, code, "Asha", "24U001")
    assert notes.db_bootstrap(db) is None, "the admin hands out invites from here on"


# ------------------------------------------------- the number and the inviter


@pytest.mark.parametrize("typed", [
    "9876543210", "+919876543210", "+91 98765 43210", "98765-43210",
    "098765 43210", " +91-98765-43210 ", "0091 9876543210", "(+91) 98765 43210",
])
def test_one_number_typed_seven_ways_is_stored_once(typed):
    """A class list that holds the same student three ways is not a list."""
    assert notes.normalise_phone(typed) == "+919876543210"


@pytest.mark.parametrize("bad", [
    "", "   ", "12345", "5876543210", "1234567890", "98765 4321",
    "98765432100", "+1 202 555 0134", "not a number", "+91",
])
def test_a_number_nobody_could_ring_is_refused_with_a_reason(bad):
    with pytest.raises(ValueError, match="10 digits"):
        notes.normalise_phone(bad)


def seed_admin(conn, name, roll, days_ago, role="admin", status="approved"):
    """An admin by default -- and, with the two keywords, one of the people the
    fallback has to walk past to find them."""
    uid = make_user(conn)
    conn.execute(
        "insert into profiles (id, name, roll_no, status, role, created_at) values "
        "(%s, %s, %s, %s, %s, now() - (%s || ' days')::interval)",
        (uid, name, roll, status, role, str(days_ago)),
    )
    return uid


def test_invited_by_names_whoever_made_the_code(db):
    db.execute("delete from profiles")
    db.execute("delete from invites where code = 'VANSHCODE'")
    seed_admin(db, "Anirudh", "I60", 9)
    vansh = seed_admin(db, "Vansh", "I61", 2)
    db.execute("insert into invites (code, created_by, expires_at) "
               "values ('VANSHCODE', %s, now() + interval '1 day')", (vansh,))
    assert notes.db_inviter(db, "VANSHCODE") == "Vansh"


def test_invited_by_falls_back_to_the_admin_who_has_been_here_longest(db):
    """The live invite was minted by the bootstrap, before anybody existed to
    credit it to, so this fallback is the branch that actually runs today."""
    db.execute("delete from profiles")
    # Both older than either admin, and neither of them is one to credit: this
    # line goes on a page anyone can load, so an approved student must not be
    # named as the inviter, and somebody still pending or blocked must not have
    # their name shown to a stranger at all. Without these two rows the query
    # answers the same with either filter deleted.
    seed_admin(db, "Sneha", "I58", 12, role="student")
    seed_admin(db, "Mallory", "I59", 20, status="pending")
    seed_admin(db, "Anirudh", "I60", 9)
    seed_admin(db, "Vansh", "I61", 2)
    db.execute("delete from invites where code = 'NOAUTHOR'")
    db.execute("insert into invites (code, expires_at) "
               "values ('NOAUTHOR', now() + interval '1 day')")
    assert notes.db_inviter(db, "NOAUTHOR") == "Anirudh"
    assert notes.db_inviter(db, "NEVER-EXISTED") == "Anirudh", "a bad code says the same"
    assert notes.db_inviter(db, "") == "Anirudh", "and so does no code at all"


def test_a_spent_code_names_the_same_person_a_live_one_does(db):
    """Otherwise the line under the heading is a checker for invite codes."""
    db.execute("delete from profiles")
    seed_admin(db, "Anirudh", "I60", 9)
    # Vansh minted both dead codes, and Anirudh is who the fallback names. With
    # one admin the branch and the fallback answer the same thing either way,
    # so the clause this is here to guard could be deleted whole.
    vansh = seed_admin(db, "Vansh", "I61", 2)
    db.execute("delete from invites where code in ('SPENT', 'EXPIRED')")
    db.execute("insert into invites (code, created_by, expires_at, max_uses, uses) "
               "values ('SPENT', %s, now() + interval '1 day', 1, 1)", (vansh,))
    db.execute("insert into invites (code, created_by, expires_at) "
               "values ('EXPIRED', %s, now() - interval '1 day')", (vansh,))
    assert notes.db_inviter(db, "SPENT") == "Anirudh"
    assert notes.db_inviter(db, "EXPIRED") == "Anirudh", "and so does one that ran out"


def test_nobody_is_named_before_there_is_an_admin(db):
    """Day zero: the bootstrap code exists and there is no one to credit."""
    db.execute("delete from profiles")
    db.execute("delete from invites where code = 'BOOTSTRAP'")
    db.execute("insert into invites (code, expires_at) "
               "values ('BOOTSTRAP', now() + interval '1 day')")
    assert notes.db_inviter(db, "BOOTSTRAP") is None
    assert "Invited by" not in notes.join_body("BOOTSTRAP", None), \
        "an empty name is worse than no line"


# ------------------------------------------------------- the real server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """notes.py's own server, listening, with an empty class and one invite.

    Nothing here can roll back -- the server opens its own connections -- so it
    wipes the two tables it owns on the way in and on the way out. recarve_test
    is disposable by construction (backend/dev/migrate.py rebuilds it).
    """
    os.environ["RECARVE_SECRET"] = SECRET.decode()

    lib = tmp_path_factory.mktemp("library")
    lectures = lib / "MC1101-Mathematics-1" / "lectures"
    lectures.mkdir(parents=True)
    (lectures / "week1.md").write_text("## Summary\nlimits and continuity\n")
    (lib / "MC1101-Mathematics-1" / "revision.md").write_text("## Revision\nall of it\n")

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("delete from profiles")
        conn.execute("delete from invites")
        conn.execute("insert into invites (code, expires_at) "
                     "values ('LETMEIN', now() + interval '1 day')")

    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]

    srv.shutdown()
    srv.server_close()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("delete from lectures")
        conn.execute("delete from materials")
        conn.execute("delete from profiles")
        conn.execute("delete from invites")
        conn.execute("delete from auth.users")


def call(port, method, path, body=None, cookie=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    if cookie:
        req.add_header("Cookie", f"{notes.SESSION_COOKIE}={cookie}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


def join(port, name, roll, code="LETMEIN", phone="9876543210"):
    status, body, headers = call(port, "POST", "/join",
                                 {"name": name, "roll_no": roll, "phone": phone,
                                  "code": code})
    cookie = None
    if headers.get("Set-Cookie"):
        cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    return status, json.loads(body), cookie


# These share `server`, so they run in order: the first person to join is the
# admin, which is the behaviour under test.
def test_a_stranger_gets_the_join_screen_and_nothing_else(server):
    status, body, _ = call(server, "GET", "/")
    assert status == 200 and 'id="f"' in body, "the front door is the join form"
    assert "limits and continuity" not in body

    for method, path in [("GET", "/data"), ("GET", "/log"), ("GET", "/jobs"),
                         ("GET", "/pending"), ("GET", "/admin"), ("GET", "/notes.py"),
                         ("POST", "/explain"), ("POST", "/upload"), ("POST", "/revise"),
                         ("POST", "/approve")]:
        code, _, _ = call(server, method, path)
        assert code == 403, f"{method} {path} answered {code} to a stranger"


def test_a_wrong_invite_code_does_not_get_in(server):
    status, body, cookie = join(server, "Mallory", "24U999", code="NOPE")
    assert status == 403 and cookie is None
    assert "wrong, expired or used up" in body["error"]


def test_the_first_joiner_is_the_admin_and_can_read(server):
    status, body, cookie = join(server, "Asha", "24U001")
    assert (status, body) == (200, {"status": "approved", "admin": True})

    code, page, _ = call(server, "GET", "/", cookie=cookie)
    assert code == 200 and "limits and continuity" in page

    code, data, _ = call(server, "GET", "/data", cookie=cookie)
    assert code == 200
    subjects = json.loads(data)["subjects"]
    assert subjects[0]["code"] == "MC1101"
    # Every subject ships, empty ones included: you cannot file a chemistry
    # recording under a subject the app never told you was there.
    assert len(subjects) == len(notes.SUBJECTS) == 12
    empty = [s for s in subjects if not s["notes"] and not s["uploads"]]
    assert len(empty) == 11, "the eleven subjects with nothing in them are still listed"
    # `kind` is what groups Lectures apart from the Revision sheet on the phone.
    assert {n["title"]: n["kind"] for n in subjects[0]["notes"]} == {
        "week1": "lecture", "Revision sheet": "revision"}

    assert call(server, "GET", "/admin", cookie=cookie)[0] == 200
    pytest.admin_cookie = cookie


def test_the_front_door_opens_when_it_cannot_say_who_invited_you(server, monkeypatch):
    """Who is inviting them is a nicety; being able to join is not. The lookup
    runs on the owning connection before anybody has a session, so a dropped
    connection there used to be a 500 on the one page a stranger may see."""
    def cannot(conn, code):
        raise psycopg.OperationalError("the connection is closed")

    monkeypatch.setattr(notes, "db_inviter", cannot)
    status, body, _ = call(server, "GET", "/?code=LETMEIN")
    assert status == 200, "a missing name may not take the front door down"
    assert 'id="code"' in body and 'value="LETMEIN"' in body, "the form still fills in"
    assert "Invited by" not in body, "and simply says nobody"


def test_being_let_in_is_not_permission_to_read_the_repo(server):
    """Approval buys you the library, not the machine it runs on.

    .env holds ANTHROPIC_API_KEY and RECARVE_SECRET; with the latter any
    session can be minted. The admin is the most privileged session there is,
    so if it cannot fetch these, no member can.
    """
    for path in ["/.env", "/notes.py", "/.git/config", "/backend/",
                 "/supabase/migrations/0002_profiles_rls.sql"]:
        code, _, _ = call(server, "GET", path, cookie=pytest.admin_cookie)
        assert code == 404, f"GET {path} answered {code}"
        code, _, _ = call(server, "HEAD", path, cookie=pytest.admin_cookie)
        assert code == 404, f"HEAD {path} answered {code}"


def test_a_pending_joiner_reads_nothing(server):
    status, body, cookie = join(server, "Bilal", "24U002")
    assert (status, body) == (200, {"status": "pending", "admin": False})

    code, page, _ = call(server, "GET", "/", cookie=cookie)
    assert code == 200 and "Almost in" in page, "waiting screen, not the library"
    assert "limits and continuity" not in page

    for method, path in [("GET", "/data"), ("GET", "/log"), ("GET", "/jobs"),
                         ("GET", "/notes.py"), ("POST", "/explain"), ("POST", "/upload")]:
        assert call(server, method, path, cookie=cookie)[0] == 403, f"{method} {path} leaked"

    # Nor may they promote themselves.
    assert call(server, "GET", "/pending", cookie=cookie)[0] == 403
    assert call(server, "GET", "/admin", cookie=cookie)[0] == 403
    assert call(server, "POST", "/approve", {"id": "x"}, cookie=cookie)[0] == 403
    pytest.pending_cookie = cookie


def test_a_forged_cookie_is_not_a_session(server):
    """Reading a classmate's cookie must not be enough to mint your own."""
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        admin_id = conn.execute(
            "select id from profiles where role = 'admin'").fetchone()[0]
    tag = pytest.pending_cookie.split(".", 1)[1]
    assert call(server, "GET", "/data", cookie=f"{admin_id}.{tag}")[0] == 403
    assert call(server, "GET", "/data", cookie=f"{admin_id}.")[0] == 403
    assert call(server, "GET", "/data", cookie="garbage")[0] == 403


def test_the_admin_can_see_and_approve_the_queue(server):
    code, body, _ = call(server, "GET", "/pending", cookie=pytest.admin_cookie)
    waiting = json.loads(body)["pending"]
    assert [p["name"] for p in waiting] == ["Bilal"]

    code, _, _ = call(server, "POST", "/approve", {"id": waiting[0]["id"]},
                      cookie=pytest.admin_cookie)
    assert code == 200

    # Same cookie as before the approval; only the row changed.
    code, data, _ = call(server, "GET", "/data", cookie=pytest.pending_cookie)
    assert code == 200 and json.loads(data)["subjects"][0]["code"] == "MC1101"


def test_an_approved_member_is_still_not_an_admin(server):
    """Bilal is approved now, so the gate lets him through -- and is_admin is
    the only thing left between him and the approval queue."""
    for method, path, body in [("GET", "/admin", None), ("GET", "/pending", None),
                               ("POST", "/approve", {"id": "x"})]:
        code, _, _ = call(server, method, path, body, cookie=pytest.pending_cookie)
        assert code == 403, f"{method} {path} answered {code} to an ordinary member"


def test_blocking_shuts_the_door_on_the_next_request(server):
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("update profiles set status = 'blocked' where roll_no = '24U002'")
    assert call(server, "GET", "/data", cookie=pytest.pending_cookie)[0] == 403
    code, page, _ = call(server, "GET", "/", cookie=pytest.pending_cookie)
    assert code == 200 and "No access" in page


def test_an_upload_records_who_sent_it(server):
    port = server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/upload", method="POST", data=b"%PDF-1.4 fake",
        headers={"X-Filename": "unit1.pdf", "X-Subject": "MC1101",
                 "Cookie": f"{notes.SESSION_COOKIE}={pytest.admin_cookie}"},
    )
    with urllib.request.urlopen(req) as r:
        assert r.status == 200
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        row = conn.execute(
            "select p.name, m.filename from materials m join profiles p "
            "on p.id = m.uploader_id where m.filename = 'unit1.pdf'").fetchone()
    assert row == ("Asha", "unit1.pdf")


# ---------------------------------------------------- the prefilled link
#
# The link goes into a WhatsApp group: a cold tap, no session, one hand. Asha
# is the admin by now, which is who these expect to be named.


def test_the_invite_link_lands_on_the_form_with_the_code_already_in_it(server):
    status, page, _ = call(server, "GET", "/?code=LETMEIN")
    assert status == 200
    assert 'value="LETMEIN"' in page, "the code did not survive the trip"
    assert 'id="ph"' in page and 'id="nm"' in page and 'id="roll"' in page
    assert 'value="Section I" readonly' in page, "one section, shown not asked"
    assert "Invited by Asha" in page, "the admin's name, out of the database"

    # And it is a real join, not just a filled box.
    status, body, cookie = join(server, "Kavya", "24U010", phone="+91 98765 43210")
    assert (status, body["status"]) == (200, "pending") and cookie


def test_typing_the_code_by_hand_still_works(server):
    """Most of the class will arrive this way -- no query string at all."""
    status, page, _ = call(server, "GET", "/")
    assert status == 200 and 'id="code"' in page
    assert 'value=""' in page, "an empty box, not a stale one"
    assert join(server, "Rohit", "24U011")[0] == 200


def test_a_bad_code_in_the_link_fails_exactly_as_a_typed_one_does(server):
    """The form must not become a checker for invite codes -- neither the page
    that carries one nor the answer when it is submitted."""
    _, bad, _ = call(server, "GET", "/?code=NOPE")
    _, good, _ = call(server, "GET", "/?code=LETMEIN")
    assert bad.replace("NOPE", "LETMEIN") == good, "the page reviewed the code"

    from_link = join(server, "Mallory", "24U900", code="NOPE")
    typed = join(server, "Mallory", "24U901", code="ALSO-NOT-IT")
    assert from_link == typed == (403, {"error": "that invite code is wrong, expired or used up"},
                                  None)


def test_a_code_in_the_url_cannot_write_html_into_the_page(server):
    """It is a query parameter: a stranger writes it, and it is reflected."""
    payload = '"><script>alert(1)</script>'
    status, page, _ = call(server, "GET", "/?code=" + urllib.parse.quote(payload))
    assert status == 200
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page, "it must appear, escaped, not vanish"


def test_a_roll_number_can_only_be_registered_once(server):
    """Asha took 24U001 at the top of this file. The second one is a sentence,
    not a stack trace -- and it must not spend a use of the invite."""
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        before = conn.execute("select uses from invites where code = 'LETMEIN'").fetchone()[0]
    status, body, cookie = join(server, "Not Asha", "24U001")
    assert (status, cookie) == (409, None)
    assert body == {"error": "that roll number is already registered"}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute(
            "select uses from invites where code = 'LETMEIN'").fetchone()[0] == before


def test_the_number_is_stored_in_one_form_however_it_was_typed(server):
    assert join(server, "Ishan", "24U012", phone="098765 43211")[0] == 200
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute(
            "select phone from profiles where roll_no = '24U012'").fetchone()[0] \
            == "+919876543211"


@pytest.mark.parametrize("phone", ["", "12345", "+1 202 555 0134"])
def test_a_join_without_a_number_anyone_could_ring_is_refused(server, phone):
    status, body, cookie = join(server, "Ghost", "24U099", phone=phone)
    assert (status, cookie) == (400, None)
    assert "10 digits" in body["error"] or "required" in body["error"]
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert not conn.execute(
            "select count(*) from profiles where roll_no = '24U099'").fetchone()[0]


def test_no_auth_keeps_the_old_single_user_behaviour(tmp_path):
    """The CLI and local testing must not need a database at all."""
    lectures = tmp_path / "MC1101-Mathematics-1" / "lectures"
    lectures.mkdir(parents=True)
    (lectures / "week1.md").write_text("## Summary\nlocal only\n")
    args = notes.argparse.Namespace(
        library=tmp_path, out=tmp_path / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=True, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        assert call(port, "GET", "/data")[0] == 200, "no cookie, no gate"
        assert "local only" in call(port, "GET", "/")[1]
        assert call(port, "POST", "/join", {"name": "x", "roll_no": "y", "code": "z"})[0] == 404
        assert call(port, "GET", "/admin")[0] == 403, "no session means no admin screen"
    finally:
        srv.shutdown()
        srv.server_close()
