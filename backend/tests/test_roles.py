"""Three roles, and what each one may actually do.

Two halves, for the two places the rule is enforced. The first drives the
database directly: a session holding a psycopg connection is what a leaked
Postgres URL or a bug in the handler looks like, and RLS is what stands there.
The second starts the real server and talks HTTP to it as each role, because a
student with curl is the attacker this was built for -- a hidden button is not
a lock, and every endpoint here is tested from outside the page that hides it.
"""

import json
import pathlib
import sys
import threading
import urllib.error
import urllib.request

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL, as_admin_connection, as_user, make_user  # noqa: E402
from test_content import member  # noqa: E402

SECRET = b"test-secret-not-the-real-one"


# ------------------------------------------------ the column and its backfill


def test_role_is_constrained_to_the_three(db):
    uid = make_user(db)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("insert into profiles (id, name, role) values (%s, 'X', 'root')", (uid,))


def test_a_new_profile_is_a_student(db):
    uid = make_user(db)
    db.execute("insert into profiles (id, name) values (%s, 'New')", (uid,))
    assert db.execute(
        "select role, trusted, is_admin, status from profiles where id = %s", (uid,)
    ).fetchone() == ("student", False, False, "pending")


@pytest.mark.parametrize("role,trusted,admin", [
    ("student", False, False), ("trusted", True, False), ("admin", True, True)])
def test_the_old_booleans_are_role_spelled_the_old_way(db, role, trusted, admin):
    """They are generated columns, so they cannot disagree with role -- and the
    two policies and the trigger that still read them are still right."""
    uid = member(db, role=role)
    assert db.execute(
        "select trusted, is_admin from profiles where id = %s", (uid,)
    ).fetchone() == (trusted, admin)


@pytest.mark.parametrize("column", ["trusted", "is_admin"])
def test_the_old_booleans_cannot_be_written_at_all(db, column):
    """Not by an admin, not by the table owner, not by anyone: the one way to
    change what somebody may do is role, so the two can never drift apart."""
    uid = member(db, role="student")
    as_admin_connection(db)
    with pytest.raises(psycopg.errors.GeneratedAlways), db.transaction():
        db.execute(f"update profiles set {column} = true where id = %s", (uid,))


def test_status_and_role_stay_separate_axes(db):
    """A pending admin is pending. Every helper tests both, and this is why."""
    uid = make_user(db)
    db.execute("insert into profiles (id, name, status, role) "
               "values (%s, 'Boss', 'pending', 'admin')", (uid,))
    as_user(db, uid)
    assert db.execute("select is_admin()").fetchone()[0] is False
    assert db.execute("select is_trusted()").fetchone()[0] is False
    assert db.execute("select is_approved()").fetchone()[0] is False


def test_blocking_outranks_every_role(db):
    uid = member(db, role="admin")
    as_admin_connection(db)
    db.execute("update profiles set status = 'blocked' where id = %s", (uid,))
    as_user(db, uid)
    assert db.execute("select is_admin()").fetchone()[0] is False
    assert db.execute("select is_trusted()").fetchone()[0] is False


# ------------------------------------------------------- what the database allows


def insert_material(db, uid, key):
    db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, "
        "size_bytes) values ('CY1107', %s, %s, %s, 10)", (uid, f"{key}.pdf", key))


def insert_lecture(db, uid, key):
    db.execute("insert into lectures (subject_code, uploader_id, audio_key) "
               "values ('CY1107', %s, %s)", (uid, key))


@pytest.mark.parametrize("write", [insert_material, insert_lecture])
def test_a_student_writes_no_content(db, write):
    me = member(db, role="student")
    as_user(db, me)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        write(db, me, "student-write")


@pytest.mark.parametrize("role", ["trusted", "admin"])
@pytest.mark.parametrize("write", [insert_material, insert_lecture])
def test_trusted_and_admin_write_content(db, role, write):
    me = member(db, role=role)
    as_user(db, me)
    write(db, me, f"{role}-write")


def test_a_student_still_reads_everything_and_votes(db):
    """The product decision, tested so nobody tightens it by accident: nothing
    in this app is behind a role except writing and spending."""
    owner = member(db, role="trusted")
    as_user(db, owner)
    mid = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, "
        "size_bytes) values ('CY1107', %s, 'n.pdf', 'read-k', 10) returning id",
        (owner,)).fetchone()[0]
    insert_lecture(db, owner, "read-a")

    as_admin_connection(db)
    student = member(db, role="student")
    as_user(db, student)
    assert db.execute("select count(*) from materials").fetchone()[0] == 1
    assert db.execute("select count(*) from lectures").fetchone()[0] == 1
    assert db.execute("select count(*) from subjects").fetchone()[0] == 12
    db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, student))
    assert db.execute("select count(*) from votes").fetchone()[0] == 1
    db.execute("insert into timetable (profile_id, day, period, subject_code) "
               "values (%s, 1, 1, 'CY1107')", (student,))


@pytest.mark.parametrize("role", ["student", "trusted"])
def test_nobody_below_admin_promotes_themselves(db, role):
    me = member(db, role=role)
    as_user(db, me)
    for want in ("admin", "trusted", "student"):
        if want == role:
            continue
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute("update profiles set role = %s where id = %s", (want, me))
    as_admin_connection(db)
    assert db.execute("select role from profiles where id = %s", (me,)).fetchone()[0] == role


@pytest.mark.parametrize("role", ["student", "trusted"])
def test_nobody_below_admin_touches_anybody_else_s_role(db, role):
    """Somebody else's row is not refused so much as invisible: the update
    policy's using-clause never matches it, so nothing is found to change.
    Asserted on the row afterwards rather than on an exception, because "no
    error and no change" is exactly what a silently working attack looks like.
    """
    me = member(db, role=role)
    victim = member(db, role="student")
    as_user(db, me)
    assert db.execute("update profiles set role = 'admin' where id = %s",
                      (victim,)).rowcount == 0
    as_admin_connection(db)
    assert db.execute(
        "select role from profiles where id = %s", (victim,)).fetchone()[0] == "student"


def test_an_admin_changes_roles(db):
    boss = member(db, role="admin")
    someone = member(db, role="student")
    as_user(db, boss)
    db.execute("update profiles set role = 'trusted' where id = %s", (someone,))
    assert db.execute(
        "select role, trusted from profiles where id = %s", (someone,)
    ).fetchone() == ("trusted", True)


def test_a_student_may_still_fix_their_own_name(db):
    """The pin is on status and role, not on the person."""
    me = member(db, role="student")
    as_user(db, me)
    db.execute("update profiles set name = 'Renamed' where id = %s", (me,))
    assert db.execute("select name from profiles where id = %s", (me,)).fetchone()[0] == "Renamed"


def test_approving_an_uploader_promotes_a_student_and_leaves_an_admin_alone(db):
    newbie = member(db, role="student")
    other_admin = member(db, role="admin")
    boss = member(db, role="admin")
    as_user(db, boss)
    db.execute("select approve_uploader(%s)", (newbie,))
    db.execute("select approve_uploader(%s)", (other_admin,))
    as_admin_connection(db)
    assert db.execute("select role from profiles where id = %s", (newbie,)).fetchone()[0] \
        == "trusted"
    assert db.execute("select role from profiles where id = %s", (other_admin,)).fetchone()[0] \
        == "admin", "approving somebody's backlog must not demote them"


def test_db_set_role_refuses_junk_and_self_demotion(db):
    boss = member(db, role="admin")
    someone = member(db, role="student")
    as_user(db, boss)
    with pytest.raises(ValueError):
        notes.db_set_role(db, boss, someone, "root")
    with pytest.raises(ValueError):
        notes.db_set_role(db, boss, boss, "student")
    assert db.execute("select role from profiles where id = %s", (boss,)).fetchone()[0] == "admin"


# ----------------------------------------------------- and what the server allows

@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The real handler on a real socket, with one person in each role.

    Nothing here rolls back -- the server opens its own connections -- so it
    clears the tables it owns on the way in and out, like test_auth's fixture.
    """
    import os
    os.environ["RECARVE_SECRET"] = SECRET.decode()

    lib = tmp_path_factory.mktemp("library")
    lectures = lib / "MC1101-Mathematics-1" / "lectures"
    lectures.mkdir(parents=True)
    (lectures / "week1.md").write_text("## Summary\nlimits and continuity\n")

    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("timetable", "votes", "materials", "lectures", "profiles", "invites"):
            conn.execute(f"delete from {table}")
        for role in ("student", "trusted", "admin"):
            uid = make_user(conn)
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role) "
                "values (%s, %s, %s, 'approved', %s)", (uid, role.title(), role, role))
            people[role] = uid
        # Someone approved but blocked later, to prove role is not the only axis.
        blocked = make_user(conn)
        conn.execute("insert into profiles (id, name, roll_no, status, role) "
                     "values (%s, 'Gone', 'gone', 'blocked', 'admin')", (blocked,))
        people["blocked"] = blocked
        conn.execute("insert into invites (code, expires_at) "
                     "values ('ROLETEST', now() + interval '1 day')")

    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield (srv.server_address[1],
           {r: notes.sign_session(i, SECRET) for r, i in people.items()},
           people)

    srv.shutdown()
    srv.server_close()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("timetable", "votes", "materials", "lectures", "profiles",
                      "invites", "auth.users"):
            conn.execute(f"delete from {table}")


def call(port, method, path, body=None, cookie=None, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers or ({"Content-Type": "application/json"} if body is not None else {}),
    )
    if cookie:
        req.add_header("Cookie", f"{notes.SESSION_COOKIE}={cookie}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# Every endpoint this server has, and the lowest role that may reach it.
# GET and POST alike, in one table, so a row that is missing here is a row
# somebody has to notice.
MATRIX = [
    ("GET", "/data", None, "student"),
    ("GET", "/me", None, "student"),
    ("GET", "/jobs", None, "student"),
    ("GET", "/log", None, "student"),
    ("GET", "/", None, "student"),
    ("POST", "/vote", {"id": "nope"}, "student"),
    ("POST", "/timetable", {"slots": []}, "student"),
    ("POST", "/explain", {"text": "x" * 40}, "trusted"),
    ("POST", "/revise", {"subject": ""}, "trusted"),
    ("POST", "/upload", None, "trusted"),
    ("GET", "/admin", None, "admin"),
    ("GET", "/pending", None, "admin"),
    ("POST", "/approve", {"id": "nope"}, "admin"),
    ("POST", "/role", {"id": "nope", "role": "trusted"}, "admin"),
]

RANK = notes.RANK


@pytest.mark.parametrize("role", ["student", "trusted", "admin"])
@pytest.mark.parametrize("method,path,body,need", MATRIX)
def test_the_matrix(server, role, method, path, body, need):
    """For each role, every endpoint: refused with the required role named, or
    reached. 'Reached' is any answer that is not a 403 -- what the endpoint then
    does with a deliberately bad id is another test's business."""
    port, cookies, _ = server
    headers = {"X-Filename": "a.pdf", "X-Subject": "MC1101"} if path == "/upload" else None
    code, out = call(port, method, path, body, cookies[role], headers)
    if RANK[role] < RANK[need]:
        assert code == 403, f"{role} reached {method} {path} ({code})"
        said = json.loads(out)
        assert said["required"] == need, f"the 403 must name the role: {said}"
        assert said["role"] == role
    else:
        assert code != 403, f"{role} was refused {method} {path}: {out[:200]}"


def test_a_student_is_refused_by_the_database_too(server):
    """Not the handler's 403 this time. The same person, the same role, going
    straight at Postgres the way a leaked connection string would."""
    _, _, people = server
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        notes.act_as(conn, people["student"])
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "insert into materials (subject_code, uploader_id, filename, file_key, "
                "size_bytes) values ('CY1107', %s, 'curl.pdf', 'ck', 10)",
                (people["student"],))


def test_blocked_outranks_admin_at_the_gate(server):
    port, cookies, _ = server
    for method, path in [("GET", "/data"), ("GET", "/admin"), ("POST", "/role")]:
        assert call(port, method, path, cookie=cookies["blocked"])[0] == 403


def test_a_student_cannot_upload_a_file_that_reaches_the_disk(server):
    """The 403 has to arrive before the body is read, or a student can still
    fill the Mac's disk and queue a transcription with it."""
    port, cookies, _ = server
    code, out = call(port, "POST", "/upload", None, cookies["student"],
                     {"X-Filename": "sneak.pdf", "X-Subject": "MC1101"})
    assert code == 403 and json.loads(out)["required"] == "trusted"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute(
            "select count(*) from materials where filename = 'sneak.pdf'"
        ).fetchone()[0] == 0


def test_the_admin_moves_somebody_between_roles(server):
    port, cookies, people = server
    student = people["student"]
    assert call(port, "POST", "/role", {"id": student, "role": "trusted"},
                cookies["admin"])[0] == 200
    # The role took effect on the next request, not on the next login.
    code, out = call(port, "POST", "/explain", {"text": "x" * 40}, cookies["student"])
    assert code != 403, out[:200]
    assert call(port, "POST", "/role", {"id": student, "role": "student"},
                cookies["admin"])[0] == 200
    code, out = call(port, "POST", "/explain", {"text": "x" * 40}, cookies["student"])
    assert code == 403 and json.loads(out)["required"] == "trusted"


def test_the_admin_cannot_demote_themselves_out_of_the_class(server):
    port, cookies, people = server
    code, out = call(port, "POST", "/role", {"id": people["admin"], "role": "student"},
                     cookies["admin"])
    assert code == 400 and "your own role" in json.loads(out)["error"]
    assert call(port, "GET", "/pending", cookie=cookies["admin"])[0] == 200


def test_a_junk_role_is_refused(server):
    port, cookies, people = server
    code, out = call(port, "POST", "/role", {"id": people["student"], "role": "root"},
                     cookies["admin"])
    assert code == 400
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute("select role from profiles where id = %s",
                            (people["student"],)).fetchone()[0] == "student"


def test_every_session_is_told_its_own_role_and_nobody_else_s(server):
    port, cookies, _ = server
    for role in ("student", "trusted", "admin"):
        assert json.loads(call(port, "GET", "/data", cookie=cookies[role])[1])["role"] == role
        me = json.loads(call(port, "GET", "/me", cookie=cookies[role])[1])
        assert me["role"] == role and me["admin"] == (role == "admin")
        # The invite code is the class's front door; only the admin is told it.
        assert (me["invite"] is not None) == (role == "admin")


def test_a_role_is_not_a_reading_lock(server):
    """A student reads the library, the notes and the class's votes. Points are
    a thank-you, not a key, and this is the test that says so."""
    port, cookies, _ = server
    code, page = call(port, "GET", "/", cookie=cookies["student"])
    assert code == 200 and "limits and continuity" in page
    data = json.loads(call(port, "GET", "/data", cookie=cookies["student"])[1])
    assert data["subjects"][0]["notes"][0]["title"] == "week1"
