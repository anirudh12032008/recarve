"""The timetable: read by everybody, written by nobody who reads it.

The grid is the registrar's, one per section, and it reaches a student's own
row through the seeding trigger and through nothing else. Students used to
edit their copy of it, which only ever drifted away from the published week --
so there is no editor and no endpoint, and this file holds the two halves of
what is left: the policies that keep one student's week out of another's
hands, and /data still carrying it, because /data is where Home gets it and a
payload that quietly stops carrying it looks exactly like an empty section.
"""

import json
import os
import pathlib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL, as_admin_connection, as_user, make_user  # noqa: E402
from test_auth import SECRET, call  # noqa: E402
from test_content import member  # noqa: E402

import psycopg  # noqa: E402

MONDAY = [{"day": 1, "period": 1, "code": "MC1101"},
          {"day": 1, "period": 3, "code": "CY1107"}]


# --------------------------------------------------------------- the table


def test_an_empty_timetable_is_the_first_state_not_an_error(db):
    """An empty week means nobody has put this section's grid in yet. It is
    not an error and it is never a guessed Monday."""
    me = member(db)
    as_user(db, me)
    assert notes.db_timetable(db, me) == []


def write_week(db, uid, slots):
    """Straight into the table, as the owner. There is no Python that writes a
    student's week any more -- the seeding trigger does it once, from the
    section template -- so a test that needs one in place puts it there."""
    as_admin_connection(db)
    for s in slots:
        db.execute("insert into timetable (profile_id, day, period, subject_code) "
                   "values (%s, %s, %s, %s)", (uid, s["day"], s["period"], s["code"]))


def test_python_holds_no_way_to_write_a_students_week(db):
    """The editor and its endpoint are gone, and so is the function they wrote
    through. Anything that grows one back has to face this test first."""
    import inspect

    assert not hasattr(notes, "db_set_timetable")
    assert "do_timetable" not in inspect.getsource(notes.build_server)


def test_your_timetable_is_yours_alone(db):
    """Section I splits into batches; no two students have the same grid, and
    none of them is anyone else's business."""
    mine, theirs = member(db), member(db)
    write_week(db, mine, MONDAY)

    as_user(db, theirs)
    assert notes.db_timetable(db, mine) == [], "RLS, not a where clause, hides it"

    # And they cannot write into it either.
    as_admin_connection(db)
    assert db.execute("select count(*) from timetable where profile_id = %s",
                      (mine,)).fetchone()[0] == 2
    as_user(db, theirs)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into timetable (profile_id, day, period, subject_code) "
                   "values (%s, 4, 4, 'MC1101')", (mine,))


def test_a_pending_joiner_cannot_write_one(db):
    """Approval is what makes you a member; nothing writes before it -- and the
    policy is what says so, not the missing endpoint."""
    uid = make_user(db)
    db.execute("insert into profiles (id, name, status) values (%s, 'Waiting', 'pending')",
               (uid,))
    as_user(db, uid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into timetable (profile_id, day, period, subject_code) "
                   "values (%s, 1, 1, 'MC1101')", (uid,))


# ---------------------------------------------------- the real server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """notes.py's own server over a real socket, with an admin and a member."""
    os.environ["RECARVE_SECRET"] = SECRET.decode()

    lib = tmp_path_factory.mktemp("library")
    folder = lib / "MC1101-Mathematics-1"
    (folder / "lectures").mkdir(parents=True)
    (folder / "lectures" / "week1.md").write_text("## Summary\nlimits\n")

    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("timetable", "votes", "materials", "lectures", "profiles"):
            conn.execute(f"delete from {table}")
        for name, admin, status in (("Asha", True, "approved"),
                                    ("Bilal", False, "approved"),
                                    ("Chan", False, "pending")):
            uid = make_user(conn)
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role, password) "
                "values (%s, %s, %s, %s, %s, 'a-real-password')",
                (uid, name, name, status, "admin" if admin else "trusted"))
            people[name] = uid

    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield (srv.server_address[1],
           {n: notes.sign_session(i, SECRET) for n, i in people.items()},
           people)

    srv.shutdown()
    srv.server_close()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("timetable", "votes", "materials", "lectures", "profiles",
                      "auth.users"):
            conn.execute(f"delete from {table}")


def data(port, cookie):
    status, body, _ = call(port, "GET", "/data", cookie=cookie)
    assert status == 200, body
    return json.loads(body)


def test_home_gets_the_timetable_on_the_request_it_already_makes(server):
    """No second round trip for Home: /data carries it, so the tab paints on
    what the page fetched on the way in."""
    port, cookies, people = server
    assert data(port, cookies["Bilal"])["timetable"] == []

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for s in MONDAY:
            conn.execute("insert into timetable (profile_id, day, period, subject_code)"
                         " values (%s, %s, %s, %s)",
                         (people["Bilal"], s["day"], s["period"], s["code"]))
    assert data(port, cookies["Bilal"])["timetable"] == MONDAY

    # And it is one student's, not the class's.
    assert data(port, cookies["Asha"])["timetable"] == []


def test_the_clock_home_compares_against_is_the_servers(server):
    """"New since you last looked" compares mtimes from this machine against a
    stamp from this machine. The phone's own clock is not in it."""
    port, cookies, _ = server
    d = data(port, cookies["Bilal"])
    note = next(s for s in d["subjects"] if s["code"] == "MC1101")["notes"][0]
    assert isinstance(d["now"], int)
    assert note["at"] <= d["now"], "a note cannot arrive after the page was drawn"


def test_only_the_admin_is_told_about_the_queue(server):
    """Needs-you shows the queue to whoever can act on it, and to nobody else."""
    port, cookies, _ = server
    assert data(port, cookies["Asha"])["pending"] == 1       # Chan is waiting
    assert "pending" not in data(port, cookies["Bilal"])


def test_nobody_can_post_a_week_at_all(server):
    """The route is gone, so an approved member gets a 404 from the dispatcher
    and a stranger never reaches it: the gate refuses anything not in
    PUBLIC_PATHS first. A pending joiner is a stranger too."""
    port, cookies, people = server
    assert call(port, "POST", "/timetable", {"slots": MONDAY},
                cookie=cookies["Bilal"])[0] == 404
    assert call(port, "POST", "/timetable", {"slots": MONDAY})[0] == 403
    assert call(port, "POST", "/timetable", {"slots": MONDAY}, cookie="garbage")[0] == 403
    assert call(port, "POST", "/timetable", {"slots": MONDAY},
                cookie=cookies["Chan"])[0] == 403
    # And the week on the server is the one that was put there directly.
    assert data(port, cookies["Bilal"])["timetable"] == MONDAY


def test_a_missing_timetable_cannot_take_the_library_with_it(server, monkeypatch):
    """Home's extras are extras.

    This table is newer than the database some machine is running, and the read
    used to kill the handler mid-reply: not a 500, no reply at all. The phone
    reads a dropped connection as "no server" and falls back to the baked
    snapshot -- stale notes, no uploads, no attribution, no vote buttons, no
    job polling. The library has to outlive its Today section.
    """
    port, cookies, _ = server

    def gone(conn, user_id):
        raise psycopg.errors.UndefinedTable('relation "timetable" does not exist')

    monkeypatch.setattr(notes, "db_timetable", gone)
    d = data(port, cookies["Bilal"])
    assert d["subjects"][0]["code"] == "MC1101", "the library is still served"
    assert "timetable" not in d, "the page already reads a missing timetable as none"
    assert data(port, cookies["Asha"])["subjects"], "and the admin's extras fail the same way"


def test_only_one_students_week_was_ever_written(server):
    """Nothing in the app writes these rows now, so the only profile with a
    week is the one this file put one on."""
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute("select count(distinct profile_id) from timetable") \
                   .fetchone()[0] == 1
