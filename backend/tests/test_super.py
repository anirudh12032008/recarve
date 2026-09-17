"""The super admin surface, against the real server on a real socket.

/super is the one screen in this app that is above the section line: it runs on
the connection that OWNS the tables, where row level security does not apply at
all. Everything 0042 spent a migration enforcing is simply not in force behind
this cookie. That makes the cookie the whole security boundary, so this file is
mostly about the cookie -- what it takes to get one, what happens to a request
without one, and what a student's cookie does when it is pointed here.

Driven over HTTP rather than by calling the handlers, for the reason test_auth
says: a gate is only worth what it does in the server that actually runs.
"""

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

SECRET = b"test-secret-not-the-real-one"
EMAIL = "owner@example.test"
# Not the real one, and nowhere near it. This is the password of a server that
# lives for the length of this module and is thrown away.
PASSWORD = "correct-horse-battery-staple"


# ------------------------------------------------------------------ driving


def call(port, method, path, body=None, cookies=None, headers=None, ip=None):
    """One request, with whatever cookies and headers it is meant to carry."""
    sent = dict(headers or {})
    if body is not None:
        sent.setdefault("Content-Type", "application/json")
    if cookies:
        sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if ip:
        # What Cloudflare writes in front of every request off the tunnel.
        sent["CF-Connecting-IP"] = ip
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=sent)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


def said(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def sign_in(port, email=EMAIL, password=PASSWORD, ip="203.0.113.7"):
    """Returns (status, body, the super cookie's value or None)."""
    status, body, headers = call(port, "POST", "/super/login",
                                 {"email": email, "password": password}, ip=ip)
    raw = headers.get("Set-Cookie")
    value = raw.split(";")[0].split("=", 1)[1] if raw else None
    return status, said(body), value


def supercookie(value):
    return {notes.SUPER_COOKIE: value}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """notes.py's own server with a super admin configured, listening.

    Module scoped and sharing one database, exactly as test_auth's is, so it
    cleans the tables it owns on the way in and on the way out. The sections it
    creates are named for this file so a leftover cannot collide with 0040's
    backfilled Section I.
    """
    os.environ["RECARVE_SECRET"] = SECRET.decode()
    os.environ["RECARVE_SUPER_EMAIL"] = EMAIL
    os.environ["RECARVE_SUPER_PASSWORD"] = PASSWORD

    lib = tmp_path_factory.mktemp("library")
    (lib / "MC1101-Mathematics-1" / "lectures").mkdir(parents=True)

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        wipe(conn)
        conn.execute("insert into invites (code, expires_at, section_id) values "
                     "('SUPERTEST', now() + interval '1 day',"
                     " (select id from sections where name = 'I'))")

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
        wipe(conn)
    for key in ("RECARVE_SUPER_EMAIL", "RECARVE_SUPER_PASSWORD"):
        os.environ.pop(key, None)


def wipe(conn):
    # All of it, Section I's included: this file pastes a template week into
    # the section its joiners land in, and a member's own week is seeded from
    # that template -- so a leftover here is four failures in test_timetable.
    conn.execute("delete from section_timetable")
    conn.execute("delete from timetable")
    conn.execute("delete from profiles")
    conn.execute("delete from invites")
    conn.execute("delete from auth.users")
    conn.execute("delete from sections where name like 'S%'")


@pytest.fixture
def cookie(server):
    """A signed-in super admin. Its own limiter key, so a test that spends the
    five tries above cannot make the next test fail to sign in."""
    notes.Limiter  # documents that the limiter is in-process, per server
    status, body, value = sign_in(server, ip="198.51.100.1")
    assert status == 200 and value, body
    return value


# ------------------------------------------------------------- the paint


def test_the_super_screen_is_on_the_apps_own_scale():
    """An admin screen that looks like a different product is an admin screen
    you distrust. test_page enforces this for PAGE; SUPER_PAGE is a second
    document with its own <style>, so it needs saying twice."""
    import re

    style = re.search(r"<style>\n(.*?)\n</style>", notes.SUPER_PAGE, re.S).group(1)
    sizes = set(re.findall(r"font-size:(\d+px)", style))
    assert sizes <= {"11px", "13px", "16px", "20px", "26px"}, \
        f"off the type scale: {sorted(sizes)}"
    weights = set(re.findall(r"font-weight:(\d+)", style))
    assert weights <= {"400", "500", "600", "700"}, f"off the weight scale: {weights}"
    radii = set(re.findall(r"border-radius:(\d+px)(?![\d ])", style))
    assert radii <= {"7px", "11px", "14px", "18px"}, f"a fifth corner: {sorted(radii)}"
    # The same purple, to the digit, light and dark.
    assert "--accent:#6534c9" in style and "--accent:#ac93ff" in style


# ------------------------------------------------- no env vars, no surface


@pytest.mark.parametrize("missing", [
    ("RECARVE_SUPER_EMAIL",),
    ("RECARVE_SUPER_PASSWORD",),
    ("RECARVE_SUPER_EMAIL", "RECARVE_SUPER_PASSWORD"),
])
def test_without_both_env_vars_there_is_no_super_admin_at_all(server, missing, monkeypatch):
    """A 404, not a login form.

    An install that sets one of the two has made a typo, not a decision, and a
    login box tells whoever guessed the path that there is something behind it.
    Every method and every route under the prefix, because the surface is the
    prefix and not the one page.
    """
    for key in missing:
        monkeypatch.delenv(key, raising=False)
    for method, path in [("GET", "/super"), ("GET", "/super/data"),
                         ("GET", "/super/section?id=x"), ("POST", "/super/login"),
                         ("POST", "/super/role"), ("POST", "/super/invite"),
                         ("POST", "/super/timetable"), ("POST", "/super/section")]:
        status, body, _ = call(server, method, path,
                               {"email": EMAIL, "password": PASSWORD}
                               if method == "POST" else None)
        assert status == 404, f"{method} {path} answered {status}"
        assert "recarve super admin" not in body, "a 404 that renders the screen"


def test_no_auth_has_no_super_admin_either(tmp_path, monkeypatch):
    """--no-auth is this laptop with no database and no real cookie key --
    `secret` is b"" there, so a cookie signed under it is one anybody can
    forge. The env vars are set and it is still a 404."""
    monkeypatch.setenv("RECARVE_SUPER_EMAIL", EMAIL)
    monkeypatch.setenv("RECARVE_SUPER_PASSWORD", PASSWORD)
    lib = tmp_path / "lib"
    (lib / "MC1101-Mathematics-1" / "lectures").mkdir(parents=True)
    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=True, verbose=False)
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        assert call(port, "GET", "/super")[0] == 404
        assert sign_in(port)[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_half_configured_install_cannot_be_signed_into(server, monkeypatch):
    """Not just the page -- the password itself stops working."""
    monkeypatch.delenv("RECARVE_SUPER_PASSWORD")
    assert sign_in(server)[0] == 404


# --------------------------------------------------------------- the door


def test_the_login_screen_is_what_a_stranger_gets(server):
    status, body, _ = call(server, "GET", "/super")
    assert status == 200
    assert 'id="f"' in body and "Super admin" in body
    assert "Every section" not in body, "the console leaked to somebody unsigned"
    assert "noindex" in body, "an admin screen must not be in a search index"


def test_a_wrong_password_is_refused(server):
    status, body, value = sign_in(server, password="not-it", ip="203.0.113.10")
    assert status == 403 and value is None
    assert body["error"] == "that email and password do not match"


def test_a_wrong_email_is_refused_in_the_same_words(server):
    """Two sentences would make this form an oracle for the email."""
    _, wrong_email, _ = sign_in(server, email="someone@else.test", ip="203.0.113.11")
    _, wrong_password, _ = sign_in(server, password="not-it", ip="203.0.113.12")
    assert wrong_email["error"] == wrong_password["error"]


def test_the_right_password_signs_in_and_the_cookie_is_locked_down(server):
    status, _, headers = call(server, "POST", "/super/login",
                              {"email": EMAIL, "password": PASSWORD}, ip="203.0.113.13")
    assert status == 200
    set_cookie = headers["Set-Cookie"]
    assert set_cookie.startswith(f"{notes.SUPER_COOKIE}=")
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/super"):
        assert flag in set_cookie, f"the cookie is missing {flag}"
    assert f"Max-Age={notes.SUPER_SESSION}" in set_cookie, "a cookie that never expires"


def test_the_email_is_matched_case_insensitively(server):
    assert sign_in(server, email=EMAIL.upper(), ip="203.0.113.14")[0] == 200


def test_the_password_is_compared_whole(server):
    """A prefix of the right password is not the right password -- and the
    comparison is constant time, which is why it is compare_digest and why
    neither half short circuits the other."""
    import inspect
    source = inspect.getsource(notes.build_server)
    body = source.split("def do_super_login")[1].split("def super_do")[0]
    assert "compare_digest" in body and "==" not in body.split("ok = ")[1].split("\n\n")[0]
    assert sign_in(server, password=PASSWORD[:-1], ip="203.0.113.15")[0] == 403


def test_the_login_is_rate_limited_per_address(server):
    """Five wrong tries from one address, then a wait -- and the address comes
    off CF-Connecting-IP, because behind the tunnel every socket is 127.0.0.1
    and one shared bucket is no bucket at all."""
    grinder = "203.0.113.200"
    for _ in range(notes.SUPER_TRIES):
        assert sign_in(server, password="nope", ip=grinder)[0] == 403
    status, body, _ = sign_in(server, password="nope", ip=grinder)
    assert status == 429 and "per address" in body["error"]
    # Even the right password waits, or the limit is only a limit for people
    # who do not know it.
    assert sign_in(server, ip=grinder)[0] == 429
    # And it is that address that is locked, not the install.
    assert sign_in(server, ip="203.0.113.201")[0] == 200


def test_the_address_is_read_from_the_tunnels_own_header(server):
    """X-Forwarded-For is the fallback, and a client can write it; the tunnel
    overwrites CF-Connecting-IP, so that is the one that is preferred."""
    for _ in range(notes.SUPER_TRIES):
        call(server, "POST", "/super/login", {"email": EMAIL, "password": "no"},
             headers={"CF-Connecting-IP": "203.0.113.90",
                      "X-Forwarded-For": "203.0.113.91"})
    # Locked on the header the tunnel wrote...
    assert sign_in(server, password="no", ip="203.0.113.90")[0] == 429
    # ...and not on the one the client claimed.
    status, _, _ = call(server, "POST", "/super/login",
                        {"email": EMAIL, "password": "no"},
                        headers={"X-Forwarded-For": "203.0.113.91"})
    assert status == 403


# ------------------------------------------------------- the two cookies


def test_nothing_under_super_opens_without_the_cookie(server):
    for method, path in [("GET", "/super/data"), ("GET", "/super/section?id=x"),
                         ("POST", "/super/section"), ("POST", "/super/role"),
                         ("POST", "/super/invite"), ("POST", "/super/timetable")]:
        status, _, _ = call(server, method, path, {} if method == "POST" else None)
        assert status == 403, f"{method} {path} answered {status} to nobody at all"


def test_a_students_cookie_cannot_reach_super(server):
    """The two cookies are signed with different keys, so this is arithmetic
    rather than a check somebody has to remember to write."""
    _, _, student = join(server, "Asha", "24S001")
    assert student
    for name in (notes.SESSION_COOKIE, notes.SUPER_COOKIE):
        status, _, _ = call(server, "GET", "/super/data", cookies={name: student})
        assert status == 403, f"a student session reached /super as {name}"
    # And not because a student cookie happens to be the wrong shape: a cookie
    # this server minted with the STUDENT key, over a payload shaped exactly
    # like a /super deadline, does not verify here either. That is the key
    # derivation and nothing else.
    shaped = notes.sign_session(int(notes.time.time()) + 3600, SECRET)
    assert call(server, "GET", "/super/data", cookies=supercookie(shaped))[0] == 403, \
        "the two cookies are signed with the same key"
    status, body, _ = call(server, "GET", "/super",
                           cookies={notes.SESSION_COOKIE: student})
    assert "Every section" not in body, "the console was served to a student"


def test_a_super_cookie_cannot_reach_a_student_route(server, cookie):
    """And the other direction. The cookie is Path=/super, so a browser never
    sends it here at all -- this is what happens when something else does."""
    for name in (notes.SESSION_COOKIE, notes.SUPER_COOKIE):
        for path in ("/data", "/pending", "/admin"):
            status, _, _ = call(server, "GET", path, cookies={name: cookie})
            assert status == 403, f"/super's cookie opened {path} as {name}"


def test_a_cookie_signed_for_another_email_does_not_verify(server, monkeypatch):
    """Changing RECARVE_SUPER_EMAIL is the log-everyone-out button: the email
    is inside the key, so the old holder's cookie stops verifying."""
    other = notes.super_secret(SECRET, "someone@else.test")
    forged = notes.sign_session(int(notes.time.time()) + 3600, other)
    assert call(server, "GET", "/super/data", cookies=supercookie(forged))[0] == 403


def test_a_cookie_the_browser_kept_too_long_is_over(server):
    """Max-Age is the browser's half and it can be ignored. The deadline is
    inside the signature, which is the half the server keeps."""
    key = notes.super_secret(SECRET, EMAIL)
    stale = notes.sign_session(int(notes.time.time()) - 1, key)
    assert call(server, "GET", "/super/data", cookies=supercookie(stale))[0] == 403
    fresh = notes.sign_session(int(notes.time.time()) + 60, key)
    assert call(server, "GET", "/super/data", cookies=supercookie(fresh))[0] == 200


def test_a_write_from_another_site_is_refused(server, cookie):
    """SameSite=Strict is the wall; this is the second one. A cross-site form
    can post text/plain shaped like JSON, and cannot set a content type."""
    status, _, _ = call(server, "POST", "/super/invite", {"section": "x"},
                        cookies=supercookie(cookie),
                        headers={"Content-Type": "text/plain"})
    assert status == 415


# ------------------------------------------------------------- sections


def test_a_new_section_starts_empty(server, cookie):
    # Somebody has to be in SOME section, or "empty" is true of every query
    # including one with no where clause on it.
    join(server, "Elif", "24S010")
    assert open_section(server, cookie, my_section(server, cookie))["members"]

    status, body, _ = call(server, "POST", "/super/section",
                           {"name": "SX", "grad_year": 2031, "set": set_id(server, cookie)},
                           cookies=supercookie(cookie))
    assert status == 200 and said(body)["label"] == "Section SX '31"

    one = open_section(server, cookie, find(server, cookie, "SX")["id"])
    assert one["members"] == [], "a brand new section came with people in it"
    assert one["timetable"] == [], "and with a week already set"
    assert one["invite"] is None, "and with a door already open"
    assert one["subjects"], "but it has to know which codes it follows"


def test_a_section_is_given_its_own_curriculums_codes_and_no_others(server, cookie):
    """A week is refused in a code the section is not taught.

    Until 0047 this test read the other way round -- Set A shipped empty, so a
    section pointed at it knew no codes at all and every line was unknown. Both
    sets have their real contents now, and the interesting question became the
    one underneath: /super validates a pasted week against the section's OWN
    set, so the same line is good for one section and nonsense for another.
    """
    sets = {s["name"]: s["id"] for s in data(server, cookie)["sets"]}
    call(server, "POST", "/super/section",
         {"name": "SE", "grad_year": 2035, "set": sets["Set A"]},
         cookies=supercookie(cookie))
    one = open_section(server, cookie, find(server, cookie, "SE")["id"])
    codes = {c["code"] for c in one["subjects"]}
    assert "PY1102" in codes and "CY1107" not in codes

    # Physics is Group-MT's, so it writes.
    status, body, _ = call(server, "POST", "/super/timetable",
                           {"section": one["id"], "csv": "Monday,1,PY1102"},
                           cookies=supercookie(cookie))
    assert status == 200 and said(body)["written"] == 1

    # Engineering Chemistry exists, and is not theirs.
    status, body, _ = call(server, "POST", "/super/timetable",
                           {"section": one["id"], "csv": "Monday,1,CY1107"},
                           cookies=supercookie(cookie))
    assert status == 400 and "unknown subject code" in said(body)["lines"][0]


def test_a_section_says_who_the_registrar_lists_and_the_app_has_never_seen(server, cookie):
    """The gap between roll_list and profiles is the list somebody acts on.

    A section screen that says "12 members" and nothing else cannot tell you
    whether that is everybody or a tenth of them.
    """
    sid = my_section(server, cookie)
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("delete from roll_list")
        # One who joined, under a roll number typed with different case and
        # spacing than the registrar wrote it, and two who never turned up.
        joined = conn.execute(
            "select roll_no from profiles where section_id = %s limit 1",
            (sid,)).fetchone()[0]
        conn.execute(
            "insert into roll_list (scholar_no, roll_no, name, section_id) values"
            " ('26111011101', %s, 'Already Here', %s),"
            " ('26111011102', '26X101', 'Absent One', %s),"
            " ('26111011103', '26X102', 'Absent Two', %s)",
            (f"  {joined.lower()} ", sid, sid, sid))

    one = open_section(server, cookie, sid)
    assert [m["roll_no"] for m in one["missing"]] == ["26X101", "26X102"], \
        "the joined one is not missing, folded roll number and all"
    assert one["missing"][0]["name"] == "Absent One"
    assert one["missing"][0]["scholar_no"] == "26111011102"

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("delete from roll_list")
    assert open_section(server, cookie, sid)["missing"] == [], \
        "an empty roll list is a section with nobody to chase, not an error"


def test_the_super_admin_can_shut_a_student_out_of_any_section(server, cookie):
    """Nine of the ten sections have no admin, so without this there is nobody
    at all who can block a bad account in them."""
    status, body, theirs = join(server, "Farhan", "24S077")
    assert status == 200
    me = next(m for m in open_section(server, cookie, my_section(server, cookie))["members"]
              if m["roll_no"] == "24S077")
    assert me["status"] == "pending", "a second joiner waits for an admin"

    status, body, _ = call(server, "POST", "/super/block", {"id": me["id"], "blocked": True},
                           cookies=supercookie(cookie))
    assert (status, said(body)["status"]) == (200, "blocked")
    after = next(m for m in open_section(server, cookie, my_section(server, cookie))["members"]
                 if m["roll_no"] == "24S077")
    assert after["status"] == "blocked"

    # A block keeps the person: reversible with the same control, and their
    # uploads and votes are still theirs. Unblocking lets them in rather than
    # putting them back to pending -- the super admin deciding they may stay is
    # the same decision an admin's Approve makes.
    status, body, _ = call(server, "POST", "/super/block", {"id": me["id"], "blocked": False},
                           cookies=supercookie(cookie))
    assert (status, said(body)["status"]) == (200, "approved")


def test_blocking_refuses_what_is_not_a_member(server, cookie):
    for payload, complaint in [({"id": str(notes.uuid.uuid4())}, "no such member"),
                               ({"id": "not-a-uuid"}, "act on")]:
        status, body, _ = call(server, "POST", "/super/block", payload,
                               cookies=supercookie(cookie))
        assert status == 400 and complaint in said(body)["error"], body


def test_a_section_cannot_be_created_twice_or_with_a_wrong_year(server, cookie):
    sets = set_id(server, cookie)
    call(server, "POST", "/super/section", {"name": "SY", "grad_year": 2032, "set": sets},
         cookies=supercookie(cookie))
    for payload, complaint in [
        ({"name": "SY", "grad_year": 2032, "set": sets}, "already exists"),
        ({"name": "", "grad_year": 2032, "set": sets}, "needs a name"),
        ({"name": "SZ", "grad_year": 20320, "set": sets}, "between 2000 and 2100"),
        ({"name": "SZ", "grad_year": "soon", "set": sets}, "has to be a number"),
        ({"name": "SZ", "grad_year": 2032, "set": str(notes.uuid.uuid4())}, "exists"),
        ({"name": "SZ", "grad_year": 2032, "set": "not-a-uuid"}, "act on"),
    ]:
        status, body, _ = call(server, "POST", "/super/section", payload,
                               cookies=supercookie(cookie))
        assert status == 400 and complaint in said(body)["error"], payload
    assert not find(server, cookie, "SZ"), "a refused section was filed anyway"


def test_an_id_off_the_client_is_not_trusted(server, cookie):
    """Every id this screen sends came off its own list, so this is the
    difference between a 400 and a psycopg DataError halfway through."""
    for path in ("/super/section?id=%27%20or%201=1--", "/super/section?id=", "/super/section"):
        status, body, _ = call(server, "GET", path, cookies=supercookie(cookie))
        assert status == 400 and "act on" in said(body)["error"], path
    status, body, _ = call(server, "GET", f"/super/section?id={notes.uuid.uuid4()}",
                           cookies=supercookie(cookie))
    assert status == 400 and "no such section" in said(body)["error"]


# ------------------------------------------------------------- the door in


def test_a_section_with_no_code_has_no_door_until_one_is_minted(server, cookie):
    """0044 reads the section off the invite, so a section nobody has minted a
    code for is a section nobody can join -- which makes the whole feature
    dead. Minting one is the super admin's, because there is no admin in a
    section nobody has joined yet."""
    sets = set_id(server, cookie)
    call(server, "POST", "/super/section", {"name": "SJ", "grad_year": 2033, "set": sets},
         cookies=supercookie(cookie))
    made = find(server, cookie, "SJ")
    assert made["invite"] is None

    status, body, _ = call(server, "POST", "/super/invite", {"section": made["id"]},
                           cookies=supercookie(cookie))
    assert status == 200
    code = said(body)["code"]
    assert find(server, cookie, "SJ")["invite"] == code, "the list must show it"

    # And it is a real door: somebody who types it lands in THIS section.
    status, joined, _ = join(server, "Bela", "24S900", code=code)
    assert status == 200
    assert len(open_section(server, cookie, made["id"])["members"]) == 1

    status, body, _ = call(server, "POST", "/super/invite",
                           {"section": str(notes.uuid.uuid4())},
                           cookies=supercookie(cookie))
    assert status == 400 and "no such section" in said(body)["error"]


# ---------------------------------------------------------------- roles


def test_granting_cr_through_super_takes_effect(server, cookie):
    """Not just the row -- the next tap. The cached principal is what would
    make a granted role take ten seconds to arrive, and db_set_role forgets
    it for exactly this reason."""
    _, _, student = join(server, "Chandni", "24S002", approved=True)
    me = section_of(server, cookie, "24S002")
    assert me["role"] == "student"
    assert call(server, "POST", "/announce", {"text": "hello"},
                cookies={notes.SESSION_COOKIE: student})[0] == 403

    status, _, _ = call(server, "POST", "/super/role", {"id": me["id"], "role": "cr"},
                        cookies=supercookie(cookie))
    assert status == 200
    assert section_of(server, cookie, "24S002")["role"] == "cr"
    status, _, _ = call(server, "POST", "/announce", {"text": "hello"},
                        cookies={notes.SESSION_COOKIE: student})
    assert status != 403, "the grant did not reach the student's next tap"


def test_a_role_off_the_ladder_is_refused(server, cookie):
    _, _, student = join(server, "Dev", "24S003", approved=True)
    me = section_of(server, cookie, "24S003")
    for role in ("superuser", "", "ADMIN"):
        status, body, _ = call(server, "POST", "/super/role",
                               {"id": me["id"], "role": role},
                               cookies=supercookie(cookie))
        assert status == 400 and "must be one of" in said(body)["error"], role
    status, body, _ = call(server, "POST", "/super/role",
                           {"id": str(notes.uuid.uuid4()), "role": "cr"},
                           cookies=supercookie(cookie))
    assert status == 400 and "no such member" in said(body)["error"]
    assert section_of(server, cookie, "24S003")["role"] == "student"


# ------------------------------------------------------------ timetable


def test_a_good_paste_is_written_and_read_back(server, cookie):
    section = my_section(server, cookie)
    codes = [c["code"] for c in open_section(server, cookie, section)["subjects"]]
    csv = f"day,period,subject_code\nMonday,1,{codes[0]}\nTuesday,3,{codes[1]}\n"
    status, body, _ = call(server, "POST", "/super/timetable",
                           {"section": section, "csv": csv},
                           cookies=supercookie(cookie))
    assert status == 200 and said(body)["written"] == 2
    week = open_section(server, cookie, section)["timetable"]
    assert [(t["day"], t["period"], t["code"]) for t in week] == \
        [(1, 1, codes[0]), (2, 3, codes[1])]


def week_of(roll):
    """One member's own week, straight out of the table as the owner."""
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        return [tuple(r) for r in conn.execute(
            "select t.day, t.period, t.subject_code from timetable t"
            " join profiles p on p.id = t.profile_id"
            " where p.roll_no = %s order by t.day, t.period", (roll,))]


def test_a_written_week_lands_on_everybody_already_in_the_section(server, cookie):
    """The trigger seeds whoever joins next. It cannot reach the people who
    joined last term, and they have no editor to fix their own week with -- so
    a correction that stopped at the template would be one nobody ever sees.
    """
    join(server, "Farah", "24S021", approved=True)
    section = my_section(server, cookie)
    codes = [c["code"] for c in open_section(server, cookie, section)["subjects"]]
    before = week_of("24S021")

    csv = f"day,period,subject_code\nMonday,1,{codes[0]}\nThursday,2,{codes[1]}\n"
    status, body, _ = call(server, "POST", "/super/timetable",
                           {"section": section, "csv": csv},
                           cookies=supercookie(cookie))
    assert status == 200, body
    assert said(body)["written"] == 2
    assert said(body)["members"] >= 1, "the admin is told whose weeks moved"

    after = week_of("24S021")
    assert after == [(1, 1, codes[0]), (4, 2, codes[1])], \
        f"the corrected week did not reach a member who was already here: {after}"
    assert after != before or not before, "this test proves nothing against an equal week"

    # Replaced, not layered onto: a period the new grid does not name is gone.
    status, _, _ = call(server, "POST", "/super/timetable",
                        {"section": section, "csv": f"Monday,1,{codes[0]}"},
                        cookies=supercookie(cookie))
    assert status == 200
    assert week_of("24S021") == [(1, 1, codes[0])], "a dropped period stayed on a week"


def test_another_sections_weeks_are_not_touched_by_it(server, cookie):
    """Both statements name the section. On the owning connection no policy
    would stop one that did not, which is exactly why they say it twice."""
    join(server, "Gita", "24S022", approved=True)
    sets = {s["name"]: s["id"] for s in data(server, cookie)["sets"]}
    call(server, "POST", "/super/section",
         {"name": "SZ", "grad_year": 2036, "set": sets["Set A"]},
         cookies=supercookie(cookie))
    other = find(server, cookie, "SZ")["id"]

    mine = my_section(server, cookie)
    codes = [c["code"] for c in open_section(server, cookie, mine)["subjects"]]
    call(server, "POST", "/super/timetable",
         {"section": mine, "csv": f"Monday,1,{codes[0]}"}, cookies=supercookie(cookie))
    assert week_of("24S022") == [(1, 1, codes[0])]

    status, body, _ = call(server, "POST", "/super/timetable",
                           {"section": other, "csv": "Tuesday,4,PY1102"},
                           cookies=supercookie(cookie))
    assert status == 200 and said(body)["members"] == 0, \
        "an empty section has no weeks to write"
    assert week_of("24S022") == [(1, 1, codes[0])], \
        "SZ's paste reached a member of Section I"


def test_a_bad_paste_writes_nothing_and_names_every_bad_line(server, cookie):
    """All at once. Learning one mistake per attempt is how forty lines take an
    evening -- and a half-written week sends a hundred people to wrong rooms."""
    section = my_section(server, cookie)
    codes = [c["code"] for c in open_section(server, cookie, section)["subjects"]]
    before = open_section(server, cookie, section)["timetable"]
    assert before, "this test is only worth anything against a week that exists"

    csv = "\n".join([
        "day,period,subject_code",
        f"Monday,1,{codes[0]}",          # fine
        f"Funday,2,{codes[0]}",          # not a day
        f"Tuesday,9,{codes[0]}",         # off the grid
        f"Tuesday,two,{codes[0]}",       # not a number
        "Wednesday,1,NOSUCH",            # unknown code
        f"Thursday,1,{codes[0]},extra",  # wrong shape
        f"Monday,1,{codes[1]}",          # the same slot twice
    ])
    status, body, _ = call(server, "POST", "/super/timetable",
                           {"section": section, "csv": csv},
                           cookies=supercookie(cookie))
    assert status == 400
    lines = said(body)["lines"]
    assert len(lines) == 6, lines
    for n, complaint in [(3, "not a day"), (4, "not 1 to 8"), (5, "not a number"),
                         (6, "unknown subject code"), (7, "expected day,period"),
                         (8, "already")]:
        assert any(line.startswith(f"line {n}:") and complaint in line
                   for line in lines), f"line {n} ({complaint}) went unnamed in {lines}"
    assert open_section(server, cookie, section)["timetable"] == before, \
        "a refused paste wrote something"


def test_the_screen_calls_the_same_parser_the_cli_does(server, cookie, monkeypatch):
    """One parser, or the one that disagreed is whichever is not being read."""
    seen = []
    real = notes.parse_timetable
    monkeypatch.setattr(notes, "parse_timetable",
                        lambda text, codes: seen.append(text) or real(text, codes))
    section = my_section(server, cookie)
    call(server, "POST", "/super/timetable", {"section": section, "csv": "Funday,1,X"},
         cookies=supercookie(cookie))
    assert seen == ["Funday,1,X"], "a second parser lives behind /super"


def test_an_empty_paste_clears_nothing_by_accident(server, cookie):
    """parse_timetable calls an empty paste an error rather than an empty week,
    so the delete-then-insert below never runs on nothing."""
    section = my_section(server, cookie)
    before = open_section(server, cookie, section)["timetable"]
    status, body, _ = call(server, "POST", "/super/timetable",
                           {"section": section, "csv": "   \n\n"},
                           cookies=supercookie(cookie))
    assert status == 400 and "no timetable rows found" in said(body)["lines"]
    assert open_section(server, cookie, section)["timetable"] == before


def test_one_sections_week_does_not_touch_another(server, cookie):
    """The connection behind this screen owns the tables, so the where clause
    on that delete is the only thing between one paste and every section's
    Monday. This is the test that says so."""
    sets = set_id(server, cookie)
    call(server, "POST", "/super/section", {"name": "STT", "grad_year": 2034, "set": sets},
         cookies=supercookie(cookie))
    other = find(server, cookie, "STT")["id"]
    section = my_section(server, cookie)
    codes = [c["code"] for c in open_section(server, cookie, section)["subjects"]]
    call(server, "POST", "/super/timetable",
         {"section": section, "csv": f"Monday,1,{codes[0]}\nFriday,4,{codes[1]}"},
         cookies=supercookie(cookie))
    call(server, "POST", "/super/timetable",
         {"section": other, "csv": f"Monday,2,{codes[0]}"},
         cookies=supercookie(cookie))
    assert len(open_section(server, cookie, section)["timetable"]) == 2, \
        "writing one section's week emptied another's"
    assert len(open_section(server, cookie, other)["timetable"]) == 1


# ------------------------------------------------------------- plumbing


def join(port, name, roll, code="SUPERTEST", password="a-real-password",
         approved=False):
    """Join the way a phone does. A phone number per roll number, because the
    column is unique and these people are all in one class."""
    status, body, headers = call(port, "POST", "/join",
                                 {"name": name, "roll_no": roll,
                                  "phone": "98765" + "".join(
                                      c for c in roll if c.isdigit())[-5:].zfill(5),
                                  "code": code, "password": password})
    raw = headers.get("Set-Cookie")
    cookie = raw.split(";")[0].split("=", 1)[1] if raw else None
    if approved:
        # Setup, not the thing under test: everybody after the first joiner
        # waits for an admin, and this file is about /super rather than about
        # the pending queue.
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("update profiles set status = 'approved' where roll_no = %s",
                         (roll,))
    return status, said(body), cookie


def data(port, cookie):
    status, body, _ = call(port, "GET", "/super/data", cookies=supercookie(cookie))
    assert status == 200, body
    return said(body)


def set_id(port, cookie):
    """Any set with subjects in it -- both have them since 0047, and which is
    which is not this file's business. That a section is only ever given its
    own set's codes is, and
    test_a_section_is_given_its_own_curriculums_codes_and_no_others says so.
    """
    return max(data(port, cookie)["sets"], key=lambda s: s["subjects"])["id"]


def find(port, cookie, name):
    return next((s for s in data(port, cookie)["sections"] if s["name"] == name), None)


def my_section(port, cookie):
    """Section I -- the one 0040 backfilled, which is where the joiners land."""
    return find(port, cookie, "I")["id"]


def open_section(port, cookie, section_id):
    status, body, _ = call(port, "GET", f"/super/section?id={section_id}",
                           cookies=supercookie(cookie))
    assert status == 200, body
    return said(body)


def section_of(port, cookie, roll):
    members = open_section(port, cookie, my_section(port, cookie))["members"]
    return next(m for m in members if m["roll_no"] == roll)


# ------------------------------------------------------ who was here (0054)

def test_a_tap_is_stamped_and_a_poll_is_not(server, cookie):
    _, _, student = join(server, "Seen Once", "24S777")
    mine = {notes.SESSION_COOKIE: student}
    notes._seen.clear()
    call(server, "GET", "/jobs", cookies=mine)
    people = said(call(server, "GET", "/super/data", cookies=supercookie(cookie))[1])["people"]
    assert [p["last_seen"] for p in people if p["roll_no"] == "24S777"] == [None], \
        "a background poll counted as somebody being here"

    call(server, "GET", "/classes?x=1", cookies=mine,
         headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0)"})
    call(server, "GET", "/", cookies=mine)  # inside the minute: not a second stamp
    d = said(call(server, "GET", "/super/data", cookies=supercookie(cookie))[1])
    me = next(p for p in d["people"] if p["roll_no"] == "24S777")
    assert me["last_seen"] and me["last_path"] == "/classes" and me["device"] == "iPhone"
    assert (me["days"], me["minutes"]) == (1, 1)
    assert d["people"][0]["roll_no"] == "24S777", "most recently seen comes first"
    u = d["stats"]["users"]
    assert u["now"] >= 1 and u["d1"] >= 1 and u["total"] >= u["ever"] >= 1
    assert len(d["stats"]["days"]) == 14 and d["stats"]["days"][-1]["users"] >= 1
    assert d["stats"]["server"]["disk_total"] > 0 and d["stats"]["db"]["size"] > 0


def test_a_member_cannot_read_or_write_who_was_here(server):
    """RLS with no policy: zero rows to read, and no privilege to write."""
    join(server, "Somebody Seen", "24S778")
    with psycopg.connect(DB_URL) as conn:
        uid = conn.execute("select id from profiles where roll_no = '24S778'").fetchone()[0]
        conn.execute("insert into activity_days (profile_id) values (%s)", (uid,))
        conn.execute("select set_config('role', 'authenticated', true)")
        assert conn.execute("select count(*) from activity_days").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("insert into activity_days (profile_id) values (%s)", (uid,))
