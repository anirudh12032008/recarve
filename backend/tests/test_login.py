"""Logging back in: roll number and password, against the real server.

Until now a session was the only way in and /join was the only way to get one,
so a member who cleared their browser was locked out for good -- joining again
fails on their own roll number, which is the unique key. This is the way back.

Everything below the unit tests talks HTTP to notes.py's own handler over a
real socket, for the reason test_auth.py gives: a gate is only worth what it
does in the server that actually runs. The forced-password-change wall in
particular is a claim about every route, and the only honest way to test "every
route" is to ask the server for them.
"""

import json
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL, make_user  # noqa: E402

SECRET = b"test-secret-not-the-real-one"
NODE = shutil.which("node")

# The generic refusal. Wrong password and unknown roll number both get it, and
# a test that spelled it twice would let them drift apart.
SAME = "that roll number and password do not match"


# ------------------------------------------------------ the sliding window


def test_the_limiter_forgets_what_falls_out_of_the_window():
    lim = notes.Limiter(window=10)
    for _ in range(3):
        lim.fail("k", now=100)
    assert lim.locked("k", 3, now=105), "three inside the window is three"
    assert not lim.locked("k", 3, now=111), "and none of them count once it passes"


def test_getting_it_right_forgives_what_came_before():
    lim = notes.Limiter(window=10)
    for _ in range(3):
        lim.fail("k", now=100)
    lim.clear("k")
    assert not lim.locked("k", 1, now=100)


def test_two_keys_are_counted_apart():
    """The per-roll count must not be spent by somebody else's guesses."""
    lim = notes.Limiter(window=10)
    lim.fail("roll:I60", now=100)
    assert not lim.locked("roll:I61", 1, now=100)


# ------------------------------------------------------------ the screens


def test_the_example_roll_number_is_not_a_real_one():
    """The placeholder is on the first screen 110 people will see. A real
    classmate's roll number sitting in it is that person's login name, in grey,
    on a page anyone can load."""
    assert 'placeholder="I0"' in notes.LOGIN_BODY
    assert "I60" not in notes.LOGIN_BODY


def test_the_login_form_asks_for_the_two_things_and_offers_the_way_out():
    assert 'id="roll"' in notes.LOGIN_BODY and 'id="pw"' in notes.LOGIN_BODY
    assert 'type="password"' in notes.LOGIN_BODY
    assert 'href="/"' in notes.LOGIN_BODY, "somebody who has not joined is stuck"
    assert 'href="/login"' in notes.join_body(), "and the join screen has no way back"


def test_the_rules_are_on_screen_before_the_box_and_not_after_the_refusal():
    body = notes.SET_PASSWORD_BODY
    assert f"At least {notes.MIN_PASSWORD} characters" in body
    assert "not your roll number" in body
    # The one line the plain-text storage decision requires, in the one place
    # it is any use: above the box, phrased as what to do.
    assert "do not use anywhere else" in body
    assert body.index("At least") < body.index('id="pw"'), \
        "a rule below the box is a rule you read after failing it"


def test_the_hint_line_is_readable_in_both_themes():
    """It carries the minimum length and the reuse advice, so it is not
    decoration -- and --mut is the token the gate already proves at 5.24:1."""
    css = re.search(r"<style>\n(.*?)\n</style>", notes.GATE_PAGE, re.S).group(1)
    assert "var(--mut)" in css.split(".hint{", 1)[1].split("}", 1)[0]


SET_SCRIPT = re.search(r"<script>\n(.*)\n</script>", notes.SET_PASSWORD_BODY, re.S).group(1)

SET_STUB = """
const assert = require('node:assert');
const btn = {disabled: false, textContent: 'Save and open the library'};
const form = {onsubmit: null, querySelector: () => btn};
const err = {textContent: ''};
const boxes = {pw: {value: ''}, pw2: {value: ''}};
const document = {getElementById: id => id === 'f' ? form : id === 'err' ? err : boxes[id]};
let went = [];
const location = {assign: p => went.push(p)};
const posts = [];
const fetch = (path, init) => {
  posts.push(JSON.parse(init.body));
  return Promise.resolve({ok: true, status: 200, json: async () => ({ok: true})});
};
"""

SET_CHECKS = """
(async () => {
  // A typo in one of the two boxes is an account whose owner is locked out of
  // it a minute after setting it. The server sees one password and cannot tell
  // a typo from a choice, so this screen is the only place that can.
  boxes.pw.value = 'correct-horse';
  boxes.pw2.value = 'correct-hoase';
  await form.onsubmit({preventDefault() {}});
  assert.equal(posts.length, 0, 'a mismatch must not be sent');
  assert.match(err.textContent, /do not match/);

  boxes.pw2.value = 'correct-horse';
  await form.onsubmit({preventDefault() {}});
  assert.deepEqual(posts, [{password: 'correct-horse'}]);
  assert.deepEqual(went, ['/'], 'and the gate decides where that lands, not this');
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_two_boxes_have_to_agree_before_anything_is_saved(tmp_path):
    f = tmp_path / "setpw.js"
    f.write_text(SET_STUB + SET_SCRIPT + SET_CHECKS)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ------------------------------------------------------- the real server


PEOPLE = [
    # name,     roll,  role,      status,     password
    ("Admin",   "A1",  "admin",   "approved", "admin-password"),
    ("Member",  "M1",  "student", "approved", "member-password"),
    ("Fresh",   "F1",  "student", "approved", None),
    ("Walled",  "F2",  "trusted", "approved", None),
    # Stands in for the two accounts that predate the join form asking for a
    # password: null, and marked roll_login by the fixture below.
    ("Legacy",  "L1",  "student", "approved", None),
    # A roll number long enough to clear the minimum on its own, so the test
    # below is refused for being the roll number and not for being short.
    ("Longroll", "LONGROLL2024", "student", "approved", None),
    ("Resettee", "R0", "student", "approved", "resettee-password"),
    ("Ratelim", "R1",  "student", "approved", "ratelim-password"),
    ("Cleared", "R2",  "student", "approved", "cleared-password"),
    ("Waiting", "P1",  "student", "pending",  "waiting-password"),
    ("Gone",    "B1",  "student", "blocked",  "gone-password"),
]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The real handler on a real socket, with one person per case.

    Nothing here rolls back -- the server opens its own connections -- so the
    fixture clears the tables it owns on the way in and out, as test_auth's
    does. recarve_test is disposable by construction.
    """
    import os
    os.environ["RECARVE_SECRET"] = SECRET.decode()

    lib = tmp_path_factory.mktemp("library")
    lectures = lib / "MC1101-Mathematics-1" / "lectures"
    lectures.mkdir(parents=True)
    (lectures / "week1.md").write_text("## Summary\nlimits and continuity\n")

    ids = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("timetable", "votes", "materials", "lectures", "profiles",
                      "invites"):
            conn.execute(f"delete from {table}")
        # The door itself. The class is not empty below, so somebody coming
        # through this code lands as pending rather than being elected admin.
        conn.execute("insert into invites (code, expires_at, max_uses) values "
                     "('LETMEIN', now() + interval '1 day', 200)")
        for name, roll, role, status, password in PEOPLE:
            uid = make_user(conn)
            # roll_login mirrors what the database can actually hold: the only
            # rows without a password are the ones an admin reset (or that
            # predate the join form asking for one), and those are exactly the
            # rows allowed to sign in with a roll number. Joining leaves neither.
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role, password, "
                "roll_login) values (%s, %s, %s, %s, %s, %s, %s)",
                (uid, name, roll, status, role, password, password is None))
            ids[roll] = uid

    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], ids

    srv.shutdown()
    srv.server_close()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("timetable", "votes", "materials", "lectures", "profiles",
                      "invites", "auth.users"):
            conn.execute(f"delete from {table}")


def call(port, method, path, body=None, cookie=None, client="10.0.0.1"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    # Behind the tunnel every socket is 127.0.0.1, so this is what tells two
    # phones apart -- and giving each test its own keeps one test's wrong
    # guesses out of the next one's per-device budget.
    req.add_header("X-Forwarded-For", client)
    if cookie:
        req.add_header("Cookie", f"{notes.SESSION_COOKIE}={cookie}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


def login(port, roll, password, client="10.0.0.1"):
    """(status, body, cookie) the way a phone gets one."""
    status, body, headers = call(port, "POST", "/login",
                                 {"roll_no": roll, "password": password},
                                 client=client)
    cookie = None
    if headers.get("Set-Cookie"):
        cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    return status, json.loads(body), cookie


def password_of(roll):
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        return conn.execute("select password from profiles where roll_no = %s",
                            (roll,)).fetchone()[0]


def row_of(roll):
    """(password, roll_login), or None if nobody holds that roll number."""
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        return conn.execute(
            "select password, roll_login from profiles where roll_no = %s",
            (roll,)).fetchone()


def join(port, roll, password, name="Joiner", code="LETMEIN",
         phone="9876543210", client="10.1.0.1"):
    """Through the front door the way a phone goes, password and all."""
    status, body, headers = call(
        port, "POST", "/join",
        {"name": name, "roll_no": roll, "phone": phone, "code": code,
         "password": password}, client=client)
    cookie = None
    if headers.get("Set-Cookie"):
        cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    return status, json.loads(body), cookie


# ------------------------------------------------------------------ in


def test_the_login_screen_is_reachable_without_a_session(server):
    port, _ = server
    status, page, _ = call(port, "GET", "/login")
    assert status == 200 and 'id="roll"' in page and 'id="pw"' in page
    assert "limits and continuity" not in page


def test_the_right_password_opens_the_library(server):
    port, _ = server
    status, body, cookie = login(port, "M1", "member-password")
    assert (status, body) == (200, {"status": "approved", "set_password": False})
    assert cookie, "no cookie means the login did nothing"
    code, data, _ = call(port, "GET", "/data", cookie=cookie)
    assert code == 200 and json.loads(data)["subjects"][0]["code"] == "MC1101"


def test_the_cookie_is_the_same_one_join_mints(server):
    """Everything downstream of a session is unchanged only if it is the same
    session -- signed the same way, verifying against the same secret."""
    port, ids = server
    _, _, cookie = login(port, "M1", "member-password")
    assert notes.unsign_session(cookie, SECRET) == ids["M1"]


def test_a_roll_number_typed_in_lower_case_still_gets_in(server):
    """It is typed on a phone keyboard by somebody who has just cleared their
    browser. Refusing 'm1' teaches them their password is wrong."""
    port, _ = server
    assert login(port, "m1", "member-password", client="10.0.0.2")[0] == 200


def test_a_wrong_password_and_an_unknown_roll_are_the_same_refusal(server):
    """Two sentences here would make this form a way to read the class list off
    a page anyone can load: type a roll number, see which answer comes back."""
    port, _ = server
    wrong = login(port, "M1", "not-the-password", client="10.0.0.3")
    unknown = login(port, "ZZ999", "not-the-password", client="10.0.0.3")
    assert wrong == unknown == (403, {"error": SAME}, None)


def test_an_empty_box_is_refused_like_everything_else(server):
    port, _ = server
    assert login(port, "M1", "", client="10.0.0.4")[1] == {"error": SAME}
    assert login(port, "", "member-password", client="10.0.0.4")[1] == {"error": SAME}


# -------------------------------------------------- the door is not the library


def test_a_pending_member_logs_in_and_lands_on_the_waiting_screen(server):
    port, _ = server
    status, body, cookie = login(port, "P1", "waiting-password")
    assert (status, body["status"]) == (200, "pending"), "the password was right"
    code, page, _ = call(port, "GET", "/", cookie=cookie)
    assert code == 200 and "Almost in" in page
    assert "limits and continuity" not in page
    assert call(port, "GET", "/data", cookie=cookie)[0] == 403


def test_a_blocked_member_logs_in_and_lands_on_the_blocked_screen(server):
    port, _ = server
    status, body, cookie = login(port, "B1", "gone-password")
    assert (status, body["status"]) == (200, "blocked")
    code, page, _ = call(port, "GET", "/", cookie=cookie)
    assert code == 200 and "No access" in page
    assert call(port, "GET", "/data", cookie=cookie)[0] == 403


# ------------------------------------------------------------ first login


def test_a_member_with_no_password_signs_in_with_their_roll_number(server):
    port, _ = server
    status, body, cookie = login(port, "F1", "F1")
    assert (status, body) == (200, {"status": "approved", "set_password": True})
    # And it opens nothing: the wall is the point of saying set_password.
    assert call(port, "GET", "/data", cookie=cookie)[0] == 403
    code, page, _ = call(port, "GET", "/", cookie=cookie)
    assert code == 200 and "Pick a password" in page
    assert "limits and continuity" not in page


def test_the_roll_number_stops_working_once_a_real_password_is_set(server):
    """The default is a password everybody in the institute can look up. If it
    survives the change, the change was theatre."""
    port, _ = server
    _, _, cookie = login(port, "F1", "F1")
    assert call(port, "POST", "/password", {"password": "kettle-badger-9"},
                cookie=cookie)[0] == 200

    assert login(port, "F1", "F1", client="10.0.0.5")[0] == 403, "the old default still works"
    status, body, fresh = login(port, "F1", "kettle-badger-9", client="10.0.0.5")
    assert (status, body) == (200, {"status": "approved", "set_password": False})
    assert call(port, "GET", "/data", cookie=fresh)[0] == 200, "and now the library opens"


def test_a_password_that_is_the_roll_number_is_refused(server):
    """The roll number is what they just signed in with, and everybody can look
    it up. A forced change that accepts the thing it replaces is theatre."""
    port, _ = server
    roll = "LONGROLL2024"
    _, _, cookie = login(port, roll, roll)
    for attempt in (roll, roll.lower(), f"  {roll}  "):
        code, body, _ = call(port, "POST", "/password", {"password": attempt},
                             cookie=cookie)
        assert code == 400, f"{attempt!r} was accepted as a new password"
        assert "roll number" in json.loads(body)["error"]
    assert password_of(roll) is None, "and nothing was written"

    # A short roll number -- which is what they actually look like here -- is
    # refused by the length rule first. Either sentence is a refusal; what must
    # never happen is it going through.
    _, _, short = login(port, "F2", "f2")
    assert call(port, "POST", "/password", {"password": "F2"}, cookie=short)[0] == 400
    assert password_of("F2") is None


def test_a_password_shorter_than_the_minimum_is_refused_with_the_number(server):
    port, _ = server
    _, _, cookie = login(port, "F2", "F2")
    code, body, _ = call(port, "POST", "/password", {"password": "x" * (notes.MIN_PASSWORD - 1)},
                         cookie=cookie)
    assert code == 400
    assert str(notes.MIN_PASSWORD) in json.loads(body)["error"]


PROTECTED = [
    ("GET", "/data", None),
    ("GET", "/me", None),
    ("GET", "/jobs", None),
    ("GET", "/log", None),
    ("GET", "/admin", None),
    ("GET", "/pending", None),
    ("GET", "/MC1101-Mathematics-1/lectures/week1.md", None),
    ("POST", "/upload", None),
    ("POST", "/explain", {"text": "what is a limit"}),
    ("POST", "/revise", {"subject": "MC1101"}),
    ("POST", "/vote", {"id": "x", "on": True}),
    ("POST", "/timetable", {"slots": []}),
    ("POST", "/profile", {"name": "Renamed"}),
    ("POST", "/approve", {"id": "x"}),
    ("POST", "/role", {"id": "x", "role": "admin"}),
    ("POST", "/block", {"id": "x"}),
    ("POST", "/remove", {"id": "x"}),
    ("POST", "/reset", {"id": "x"}),
]


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_a_member_who_has_not_picked_a_password_reaches_nothing(server, method, path, body):
    """The screen hiding the library is not the enforcement. This is: a valid
    cookie, a direct request, and no app in between.

    F2 is trusted, so /upload, /explain and /revise are refused here by the
    password wall rather than by their role -- which is the case a wall drawn
    only on the screen would let straight through.
    """
    port, _ = server
    _, _, cookie = login(port, "F2", "F2", client="10.0.0.6")
    code, said, _ = call(port, method, path, body, cookie=cookie, client="10.0.0.6")
    assert code == 403, f"{method} {path} answered {code}"
    assert json.loads(said).get("set_password") is True, \
        f"{method} {path} refused them for some other reason"


def test_the_wall_comes_down_the_moment_a_password_is_set(server):
    port, _ = server
    _, _, cookie = login(port, "F2", "F2", client="10.0.0.6")
    assert call(port, "GET", "/data", cookie=cookie)[0] == 403
    assert call(port, "POST", "/password", {"password": "otter-lantern-3"},
                cookie=cookie)[0] == 200
    # Same cookie as before. Only the row changed, and it is re-read per request.
    assert call(port, "GET", "/data", cookie=cookie)[0] == 200
    assert password_of("F2") == "otter-lantern-3"


# ---------------------------------------------------------- rate limiting


def test_guessing_is_shut_off_after_the_limit_and_says_what_it_is(server):
    port, _ = server
    for n in range(notes.LOGIN_TRIES):
        code, body, _ = login(port, "R1", f"guess-{n}", client="10.0.0.7")
        assert (code, body) == (403, {"error": SAME}), f"guess {n} was not refused"

    # Even the right one, which is what makes it a lockout rather than a hint.
    code, body, cookie = login(port, "R1", "ratelim-password", client="10.0.0.7")
    assert (code, cookie) == (429, None)
    assert str(notes.LOGIN_TRIES) in body["error"], "say the number, not 'try later'"
    assert str(notes.LOGIN_WINDOW // 60) in body["error"], "and how long the wait is"


def test_the_lockout_follows_the_roll_number_and_not_the_screen(server):
    """R1 is locked out from the test above. A second device does not get a
    fresh five guesses at the same account."""
    port, _ = server
    assert login(port, "R1", "ratelim-password", client="10.0.0.8")[0] == 429
    # And it is that roll number that is locked, not everybody on that device.
    assert login(port, "R2", "cleared-password", client="10.0.0.7")[0] == 200


def test_getting_in_forgives_the_wrong_guesses_before_it(server):
    """A classmate who mistyped four times and then remembered must not be one
    slip away from a lockout for the rest of the window."""
    port, _ = server
    for n in range(notes.LOGIN_TRIES - 1):
        assert login(port, "R2", f"guess-{n}", client="10.0.0.9")[0] == 403
    assert login(port, "R2", "cleared-password", client="10.0.0.9")[0] == 200

    for n in range(notes.LOGIN_TRIES - 1):
        code, body, _ = login(port, "R2", f"again-{n}", client="10.0.0.9")
        assert (code, body) == (403, {"error": SAME}), "the count was not cleared"


def test_one_device_cannot_walk_the_class_list(server):
    """Per-roll alone lets one machine try five each, all the way down a list
    of 110 roll numbers. This is the other half of the limit."""
    port, _ = server
    seen = set()
    for n in range(notes.LOGIN_TRIES_PER_CLIENT + 1):
        seen.add(login(port, f"NOBODY{n}", "guess", client="10.9.9.9")[0])
    assert 429 in seen, "the per-device count never tripped"


# ------------------------------------------------------------ admin reset


def test_an_admin_reset_puts_somebody_back_through_the_change(server):
    port, ids = server
    _, _, admin = login(port, "A1", "admin-password", client="10.0.0.10")

    # They are ordinary before it.
    assert login(port, "R0", "resettee-password", client="10.0.0.10")[1]["set_password"] is False

    assert call(port, "POST", "/reset", {"id": ids["R0"]}, cookie=admin,
                client="10.0.0.10")[0] == 200
    assert password_of("R0") is None

    # Their own password is gone, and the roll number is the way back in --
    # straight into the wall.
    assert login(port, "R0", "resettee-password", client="10.0.0.10")[0] == 403
    status, body, cookie = login(port, "R0", "R0", client="10.0.0.10")
    assert (status, body["set_password"]) == (200, True)
    assert call(port, "GET", "/data", cookie=cookie)[0] == 403


def test_a_member_cannot_reset_anybody(server):
    port, ids = server
    _, _, member = login(port, "M1", "member-password", client="10.0.0.11")
    code, body, _ = call(port, "POST", "/reset", {"id": ids["A1"]}, cookie=member,
                         client="10.0.0.11")
    assert code == 403 and json.loads(body)["required"] == "admin"
    assert password_of("A1") == "admin-password", "the admin's password survived"


def test_a_stranger_cannot_reset_anybody(server):
    port, ids = server
    assert call(port, "POST", "/reset", {"id": ids["A1"]}, client="10.0.0.12")[0] == 403
    assert password_of("A1") == "admin-password"


def test_resetting_somebody_who_does_not_exist_is_a_404_not_a_500(server):
    port, _ = server
    _, _, admin = login(port, "A1", "admin-password", client="10.0.0.13")
    for target in ("00000000-0000-0000-0000-000000000000", "not-a-uuid"):
        code, _, _ = call(port, "POST", "/reset", {"id": target}, cookie=admin,
                          client="10.0.0.13")
        assert code == 404, f"{target} answered {code}"


def test_the_admin_screen_is_given_the_passwords_it_shows(server):
    """They are stored as typed, and this screen is where that is any use: a
    classmate rings, and the admin reads it back rather than resetting an
    account they cannot see into."""
    port, _ = server
    _, _, admin = login(port, "A1", "admin-password", client="10.0.0.14")
    body = json.loads(call(port, "GET", "/pending", cookie=admin, client="10.0.0.14")[1])
    by_roll = {m["roll_no"]: m["password"] for m in body["members"]}
    assert by_roll["M1"] == "member-password"
    assert by_roll["R0"] is None, "and says plainly when there is not one yet"


# --------------------------------------------------- the password is picked
# --------------------------------------------------- at the door, not after it


def test_joining_picks_the_password_that_logs_them_in_afterwards(server):
    port, _ = server
    status, body, cookie = join(port, "J1", "corridor-lamp-7")
    assert (status, body["status"]) == (200, "pending")
    assert cookie, "no cookie means the join did nothing"
    assert row_of("J1") == ("corridor-lamp-7", False), \
        "the password is written by the same statement that creates the row"

    # And it is the way back in, from a browser that has never seen this class.
    code, said, fresh = login(port, "J1", "corridor-lamp-7", client="10.1.0.2")
    assert (code, said["set_password"]) == (200, False)
    assert fresh, "the password a joiner chose has to be the one that signs them in"


def test_a_joiner_is_never_reachable_with_their_own_roll_number(server):
    """The whole bug. A roll number is public -- it is on every list in the
    institute -- so if it is ever a password, the first classmate to type it
    becomes that person and locks the real one out for good."""
    port, _ = server
    assert join(port, "J2", "brass-kettle-4")[0] == 200
    for guess in ("J2", "j2", " J2 "):
        code, said, cookie = login(port, "J2", guess, client="10.1.0.3")
        assert (code, cookie) == (403, None), f"{guess!r} got in as J2"
        assert said == {"error": SAME}
    assert row_of("J2") == ("brass-kettle-4", False), "and nothing was taken from them"


def test_joining_with_no_password_is_refused_and_says_which_field(server):
    port, _ = server
    for blank in (None, "", "   "):
        code, said, cookie = join(port, "J3", blank)
        assert (code, cookie) == (400, None)
        assert "password" in said["error"], said["error"]
    assert row_of("J3") is None, "a refused join must not leave half an account"


def test_joining_with_the_roll_number_as_the_password_is_refused(server):
    """It is the one password every classmate already knows."""
    port, _ = server
    roll = "JOINROLL2024"     # long enough to clear the minimum on its own
    for attempt in (roll, roll.lower(), f"  {roll}  "):
        code, said, _ = join(port, roll, attempt)
        assert code == 400, f"{attempt!r} was accepted at the door"
        assert "roll number" in said["error"], said["error"]
    assert row_of(roll) is None


def test_joining_with_a_short_password_is_refused_with_the_number(server):
    port, _ = server
    code, said, _ = join(port, "J4", "x" * (notes.MIN_PASSWORD - 1))
    assert code == 400
    assert str(notes.MIN_PASSWORD) in said["error"], "say the number, not 'too short'"
    assert row_of("J4") is None


def test_a_refused_password_does_not_spend_the_invite_code(server):
    """A code has a limited number of uses and comes from somebody they had to
    ask. Burning one on a password that was never accepted is a second thing
    gone wrong for the same mistake."""
    port, _ = server

    def uses():
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            return conn.execute(
                "select uses from invites where code = 'LETMEIN'").fetchone()[0]

    before = uses()
    assert join(port, "J5", "short")[0] == 400
    assert uses() == before
    assert join(port, "J5", "harbour-pencil-2")[0] == 200
    assert uses() == before + 1


def test_the_invite_code_is_still_required_and_still_checked(server):
    port, _ = server
    code, said, _ = join(port, "J6", "meadow-socket-5", code="")
    assert code == 400 and "invite code" in said["error"]

    code, said, cookie = join(port, "J6", "meadow-socket-5", code="NOTACODE")
    assert (code, cookie) == (403, None)
    assert "invite code" in said["error"]
    assert row_of("J6") is None, "a wrong code must not leave an account behind"


# ------------------------------------------- the two accounts that predate it


def test_only_a_row_marked_roll_login_may_sign_in_with_a_roll_number(server):
    """What the migration did for the two live accounts, and the wall around
    it. L1 has no password and predates the door, so their roll number is the
    way in -- once, into the screen that replaces it. Nobody who joins can be
    put in that state, and this is the pair that says so."""
    port, _ = server
    # The legacy account: null password, roll_login true, and it opens nothing.
    assert row_of("L1") == (None, True)
    status, body, cookie = login(port, "L1", "L1", client="10.1.0.4")
    assert (status, body["set_password"]) == (200, True)
    assert call(port, "GET", "/data", cookie=cookie)[0] == 403

    # The new account: a password, roll_login false, and no way to the above.
    assert join(port, "J7", "granite-window-8")[0] == 200
    assert row_of("J7") == ("granite-window-8", False)
    assert login(port, "J7", "J7", client="10.1.0.5")[0] == 403


def test_setting_a_password_spends_the_roll_number_door_for_good(server):
    port, _ = server
    _, _, cookie = login(port, "Longroll2024".upper(), "LONGROLL2024",
                         client="10.1.0.6")
    assert call(port, "POST", "/password", {"password": "harbour-thistle-1"},
                cookie=cookie)[0] == 200
    assert row_of("LONGROLL2024") == ("harbour-thistle-1", False)
    assert login(port, "LONGROLL2024", "LONGROLL2024", client="10.1.0.7")[0] == 403


def test_an_admin_reset_is_the_only_thing_that_opens_that_door_again(server):
    """Reset has to keep working -- it is the whole recovery path -- and this
    is the shape of it: back to the roll number, back through the forced
    change, and out the other side with a password of their own."""
    port, ids = server
    _, _, admin = login(port, "A1", "admin-password", client="10.1.0.8")
    assert join(port, "J8", "cinder-parcel-6")[0] == 200
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        target = str(conn.execute(
            "select id from profiles where roll_no = 'J8'").fetchone()[0])
    # Approved first: the forced-change screen is behind the same gate as the
    # library, so somebody still at the door never reaches it.
    assert call(port, "POST", "/approve", {"id": target}, cookie=admin,
                client="10.1.0.8")[0] == 200

    assert call(port, "POST", "/reset", {"id": str(target)}, cookie=admin,
                client="10.1.0.8")[0] == 200
    assert row_of("J8") == (None, True), "reset is what marks the row, not the null"

    assert login(port, "J8", "cinder-parcel-6", client="10.1.0.9")[0] == 403
    status, body, cookie = login(port, "J8", "J8", client="10.1.0.9")
    assert (status, body["set_password"]) == (200, True)
    assert call(port, "GET", "/data", cookie=cookie)[0] == 403
    assert call(port, "POST", "/password", {"password": "lantern-copper-3"},
                cookie=cookie)[0] == 200
    assert row_of("J8") == ("lantern-copper-3", False)
    assert login(port, "J8", "J8", client="10.1.0.10")[0] == 403
    assert login(port, "J8", "lantern-copper-3", client="10.1.0.10")[0] == 200


# ------------------------------------------------------ the join screen says so


def test_the_join_form_states_the_rules_above_the_box(server):
    """It is the first screen 110 people see, and a rule you learn from an
    error message is a rule you learn twice."""
    port, _ = server
    page = call(port, "GET", "/")[1]
    assert 'id="pw"' in page and 'autocomplete="new-password"' in page
    assert f"At least {notes.MIN_PASSWORD} characters" in page
    assert "not your roll number" in page
    assert "do not use anywhere else" in page
    assert page.index("At least") < page.index('id="pw"'), \
        "a rule below the box is a rule you read after failing it"
