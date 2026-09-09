"""The timetable: the one thing Home cannot show unless it is told.

Nobody has typed the institute grid into this app and nobody should -- the
source PDF's columns are ambiguous enough that a guessed one would file
lectures under the wrong subject in silence. So it starts empty, per student,
and this is what has to hold: it round-trips, it is private, and a save
replaces the week rather than layering onto it.

Two halves, the same shape as test_votes.py. The first drives the policies on a
transaction that rolls back; the second saves a week over HTTP and reads it back
out of /data, because /data is where Home actually gets it and a payload that
quietly stops carrying it looks exactly like a student who never filled it in.
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


def test_a_timetable_round_trips(db):
    me = member(db)
    as_user(db, me)
    assert notes.db_set_timetable(db, me, MONDAY) == 2
    assert notes.db_timetable(db, me) == MONDAY


def test_an_empty_timetable_is_the_first_state_not_an_error(db):
    me = member(db)
    as_user(db, me)
    assert notes.db_timetable(db, me) == []


def test_saving_replaces_the_whole_week(db):
    """Edited whole, saved whole. A save that only added would make clearing a
    period impossible, which is the edit a student makes most."""
    me = member(db)
    as_user(db, me)
    notes.db_set_timetable(db, me, MONDAY)
    notes.db_set_timetable(db, me, [{"day": 2, "period": 1, "code": "BS1111"}])
    assert notes.db_timetable(db, me) == [{"day": 2, "period": 1, "code": "BS1111"}]
    notes.db_set_timetable(db, me, [])
    assert notes.db_timetable(db, me) == [], "clearing everything must be possible"


def test_the_same_period_twice_in_one_save_is_the_last_one(db):
    """The primary key would raise mid-transaction otherwise, losing the save."""
    me = member(db)
    as_user(db, me)
    assert notes.db_set_timetable(db, me, [
        {"day": 1, "period": 1, "code": "MC1101"},
        {"day": 1, "period": 1, "code": "CY1107"}]) == 1
    assert notes.db_timetable(db, me) == [{"day": 1, "period": 1, "code": "CY1107"}]


@pytest.mark.parametrize("slot", [
    {"day": 0, "period": 1, "code": "MC1101"},      # Sunday has no periods
    {"day": 7, "period": 1, "code": "MC1101"},
    {"day": 1, "period": 0, "code": "MC1101"},
    {"day": 1, "period": 99, "code": "MC1101"},
    {"day": 1, "period": 1, "code": "ZZ9999"},      # not a subject of this course
    {"day": 1, "period": 1, "code": None},
    {"day": 1, "code": "MC1101"},
])
def test_a_nonsense_slot_is_refused_before_it_reaches_the_database(db, slot):
    """The check constraints and the foreign key would refuse these anyway --
    as a 500. One guard, in the one function every caller routes through."""
    me = member(db)
    as_user(db, me)
    with pytest.raises(ValueError):
        notes.db_set_timetable(db, me, [slot])


def test_a_refused_save_leaves_the_old_week_alone(db):
    me = member(db)
    as_user(db, me)
    notes.db_set_timetable(db, me, MONDAY)
    with pytest.raises(ValueError):
        notes.db_set_timetable(db, me, [{"day": 9, "period": 1, "code": "MC1101"}])
    assert notes.db_timetable(db, me) == MONDAY, "a bad save must not wipe the week"


def test_your_timetable_is_yours_alone(db):
    """Section I splits into batches; no two students have the same grid, and
    none of them is anyone else's business."""
    mine, theirs = member(db), member(db)
    as_user(db, mine)
    notes.db_set_timetable(db, mine, MONDAY)

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


def test_a_pending_joiner_cannot_save_one(db):
    """Approval is what makes you a member; nothing writes before it."""
    uid = make_user(db)
    db.execute("insert into profiles (id, name, status) values (%s, 'Waiting', 'pending')",
               (uid,))
    as_user(db, uid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        notes.db_set_timetable(db, uid, MONDAY)


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
                "insert into profiles (id, name, roll_no, status, trusted, is_admin) "
                "values (%s, %s, %s, %s, true, %s)", (uid, name, name, status, admin))
            people[name] = uid

    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield (srv.server_address[1],
           {n: notes.sign_session(i, SECRET) for n, i in people.items()})

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
    port, cookies = server
    assert data(port, cookies["Bilal"])["timetable"] == []

    status, body, _ = call(port, "POST", "/timetable", {"slots": MONDAY},
                           cookie=cookies["Bilal"])
    assert status == 200, body
    assert json.loads(body) == {"saved": 2}
    assert data(port, cookies["Bilal"])["timetable"] == MONDAY

    # And it is one student's, not the class's.
    assert data(port, cookies["Asha"])["timetable"] == []


def test_the_clock_home_compares_against_is_the_servers(server):
    """"New since you last looked" compares mtimes from this machine against a
    stamp from this machine. The phone's own clock is not in it."""
    port, cookies = server
    d = data(port, cookies["Bilal"])
    note = next(s for s in d["subjects"] if s["code"] == "MC1101")["notes"][0]
    assert isinstance(d["now"], int)
    assert note["at"] <= d["now"], "a note cannot arrive after the page was drawn"


def test_only_the_admin_is_told_about_the_queue(server):
    """Needs-you shows the queue to whoever can act on it, and to nobody else."""
    port, cookies = server
    assert data(port, cookies["Asha"])["pending"] == 1       # Chan is waiting
    assert "pending" not in data(port, cookies["Bilal"])


def test_a_nonsense_week_is_refused_with_a_reason(server):
    port, cookies = server
    for slots in ([{"day": 9, "period": 1, "code": "MC1101"}],
                  [{"day": 1, "period": 1, "code": "ZZ9999"}]):
        status, body, _ = call(port, "POST", "/timetable", {"slots": slots},
                               cookie=cookies["Bilal"])
        assert status == 400, body
        assert json.loads(body)["error"], "the phone has to be able to say why"
    status, body, _ = call(port, "POST", "/timetable", {"slots": "monday"},
                           cookie=cookies["Bilal"])
    assert status == 400, body
    assert data(port, cookies["Bilal"])["timetable"] == MONDAY, "and nothing was lost"


def test_a_stranger_has_no_timetable_to_save(server):
    """/timetable is not in PUBLIC_PATHS, so the gate refuses it before any
    handler sees it -- and a pending joiner is a stranger too."""
    port, cookies = server
    assert call(port, "POST", "/timetable", {"slots": MONDAY})[0] == 403
    assert call(port, "POST", "/timetable", {"slots": MONDAY}, cookie="garbage")[0] == 403
    assert call(port, "POST", "/timetable", {"slots": MONDAY},
                cookie=cookies["Chan"])[0] == 403
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute("select count(distinct profile_id) from timetable") \
                   .fetchone()[0] == 1
