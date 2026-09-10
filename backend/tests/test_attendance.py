"""Attendance: the number a first-year actually worries about.

75% per subject is what MANIT checks before it lets you sit that subject's
paper, so everything here is per subject and there is no overall figure to be
comforted by. Three things have to hold and this file is all three:

  * The arithmetic is right, and honest at the edges. 74.96% is not 75%, and a
    percentage that rounds up across the threshold would read as safe to
    somebody who is about to be refused an exam.
  * Nothing is ever assumed. An unmarked period is in neither half of the
    fraction, and a cancelled class is in neither half for anybody.
  * It is private. Not "hidden by a where clause" private -- RLS private,
    proven below with two sessions on one connection.

Three parts, the shape the rest of the suite already uses: the pure maths with
no database at all, then the policies on a transaction that rolls back, then
the real server over a real socket, because /data is where the phone actually
gets this and a payload that quietly stops carrying it looks exactly like a
student who never marked anything.
"""

import datetime
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


# ------------------------------------------------------------- the maths
# No database, no server: the fraction, the percentage and the two counts a
# student plans around. Everything else in this file leans on these being right.


@pytest.mark.parametrize("attended,held,pct", [
    (0, 0, None),        # nothing marked: there is no percentage, and None says so
    (0, 1, 0.0),
    (3, 4, 75.0),        # exactly the threshold, exactly
    (9, 12, 75.0),
    (7, 10, 70.0),
    (10, 10, 100.0),
    (1, 3, 33.3),        # 33.33... floored, never 33.4
    (2, 3, 66.6),        # 66.66... floored, never 66.7
    (1874, 2500, 74.9),  # 74.96%, which is NOT 75% and must never print as it
])
def test_the_percentage_is_the_true_one_and_never_rounds_up(attended, held, pct):
    assert notes.attendance_maths(attended, held)["pct"] == pct


def test_no_classes_held_is_not_a_student_at_zero_percent(a=0, h=0):
    """0/0 has no percentage. Printing 0% would say "you are failing" to
    somebody who has simply not marked anything yet."""
    m = notes.attendance_maths(a, h)
    assert m["pct"] is None
    assert m["ok"] is True, "nobody is below a threshold they have no data for"
    assert notes.attendance_note(m) == "No classes marked yet."


@pytest.mark.parametrize("attended,held,can_miss", [
    (3, 4, 0),      # exactly 75%: the very next miss drops you under
    (9, 12, 0),
    (10, 12, 1),    # 83.3% -> 10/13 = 76.9% ok, 10/14 = 71.4% not
    (12, 12, 4),    # 100%  -> 12/16 = 75.0% exactly, 12/17 is under
    (9, 10, 2),     # 90%   -> 9/12 = 75.0% exactly
    (7, 10, 0),     # already under: there is no room at all
    (0, 0, 0),      # and none before anything is marked
])
def test_how_many_in_a_row_can_be_missed(attended, held, can_miss):
    """The largest k with attended / (held + k) >= 75%.

    Stated as a run of consecutive misses on purpose. "You may miss 7 more this
    term" would need a semester total, nobody has told this app when the
    semester ends, and a made-up total is a made-up promise.
    """
    m = notes.attendance_maths(attended, held)
    assert m["can_miss"] == can_miss
    # And the boundary itself: k lands on or above 75%, k + 1 does not. Only
    # meaningful for somebody who is above it to begin with -- below, there is
    # no room at all and can_miss is 0 because nothing works, not because k
    # does.
    if held and m["ok"]:
        assert 4 * attended >= 3 * (held + can_miss)
        assert not 4 * attended >= 3 * (held + can_miss + 1)


@pytest.mark.parametrize("attended,held,must_attend", [
    (3, 4, 0),      # exactly 75% is not below it
    (7, 10, 2),     # 70%   -> 9/12 = 75%
    (8, 11, 1),     # 72.7% -> 9/12 = 75%
    (0, 10, 30),
    (0, 1, 3),
])
def test_how_many_in_a_row_climb_back(attended, held, must_attend):
    """The smallest n with (attended + n) / (held + n) >= 75%. Also computable
    without knowing how long the semester is."""
    m = notes.attendance_maths(attended, held)
    assert m["must_attend"] == must_attend
    assert 4 * (attended + must_attend) >= 3 * (held + must_attend)
    if must_attend:
        assert not 4 * (attended + must_attend - 1) >= 3 * (held + must_attend - 1)


@pytest.mark.parametrize("attended,held,ok", [
    (3, 4, True),        # exactly 75% clears the bar
    (299, 400, False),   # 74.75%
    (301, 400, True),
])
def test_exactly_seventy_five_percent_is_not_below_it(attended, held, ok):
    assert notes.attendance_maths(attended, held)["ok"] is ok


def test_the_sentence_says_the_next_step_and_never_a_semester_total():
    """The consequence is in words on the server, so the phone and this file
    read the same one and no screen can invent a cheerier version."""
    said = [notes.attendance_note(notes.attendance_maths(a, h))
            for a, h in [(0, 0), (3, 4), (9, 10), (10, 12), (7, 10), (8, 11)]]
    assert said == [
        "No classes marked yet.",
        "Miss the next class and you drop below 75%.",
        "You can miss the next 2 classes and stay at 75%.",
        "You can miss one more class and stay at 75%.",
        "Attend the next 2 classes in a row to get back to 75%.",
        "Attend the next class to get back to 75%.",
    ]
    for line in said:
        assert "semester" not in line and "term" not in line, (
            "nobody has told this app when the semester ends, so it may not "
            "say anything that assumes a total"
        )


# --------------------------------------------------------- the policies

MONDAY = [{"day": 1, "period": 1, "code": "MC1101"},
          {"day": 1, "period": 2, "code": "CY1107"},
          {"day": 1, "period": 3, "code": "MC1101"}]


def last_monday(db):
    """A real past Monday inside the window, from the database's own clock --
    the same clock the policy's `current_date` and the handler both read."""
    today = db.execute("select current_date").fetchone()[0]
    back = (today.isoweekday() - 1) % 7 or 7        # never today, always past
    return today - datetime.timedelta(days=back)


def student_with_a_monday(db, role="student"):
    # Back to the owner first: a test that has already acted as somebody cannot
    # create the next person, because the profiles policy is doing its job.
    as_admin_connection(db)
    uid = member(db, role=role)
    as_user(db, uid)
    notes.db_set_timetable(db, uid, MONDAY)
    return uid


def totals(db, uid, code):
    for s in notes.db_attendance(db, uid)["subjects"]:
        if s["code"] == code:
            return s["attended"], s["held"]
    return None


def test_present_absent_and_changing_your_mind(db):
    me = student_with_a_monday(db)
    day = last_monday(db).isoformat()

    notes.db_mark_attendance(db, me, [{"date": day, "period": 1, "state": "present"},
                                      {"date": day, "period": 3, "state": "absent"}])
    assert totals(db, me, "MC1101") == (1, 2)

    # Changing a mark rewrites it rather than adding a second one.
    notes.db_mark_attendance(db, me, [{"date": day, "period": 3, "state": "present"}])
    assert totals(db, me, "MC1101") == (2, 2)

    # And clearing it goes back to not-yet-marked, which is a real third state.
    notes.db_mark_attendance(db, me, [{"date": day, "period": 3, "state": "clear"}])
    assert totals(db, me, "MC1101") == (1, 1)


def test_an_unmarked_period_is_in_neither_half_of_the_fraction(db):
    """The whole reason there are three states. A default of present would hand
    a student a percentage that is wrong in the direction that costs the exam;
    a default of absent is worse still."""
    me = student_with_a_monday(db)
    day = last_monday(db).isoformat()
    assert totals(db, me, "MC1101") == (0, 0), "nothing marked is nothing held"

    notes.db_mark_attendance(db, me, [{"date": day, "period": 1, "state": "present"}])
    # Period 3 that same Monday is also MC1101 and is still unmarked.
    assert totals(db, me, "MC1101") == (1, 1), "the unmarked period is not held"


def test_a_cancelled_class_counts_for_nobody(db):
    """Class-wide, so one trusted member setting it changes the denominator for
    everyone -- including the person who had already marked themselves present
    for it."""
    trusted = member(db, trusted=True)
    mine = student_with_a_monday(db)
    theirs = student_with_a_monday(db)
    day = last_monday(db).isoformat()

    for who in (mine, theirs):
        as_user(db, who)
        notes.db_mark_attendance(db, who, [
            {"date": day, "period": 1, "state": "present"},
            {"date": day, "period": 3, "state": "absent"}])
        assert totals(db, who, "MC1101") == (1, 2)

    as_user(db, trusted)
    notes.db_set_cancelled(db, trusted, day, 1, "MC1101", True, "lab shifted")

    for who in (mine, theirs):
        as_user(db, who)
        assert totals(db, who, "MC1101") == (0, 1), (
            "a class that did not happen leaves both halves, for everybody")

    # And putting it back restores the mark underneath: the cancellation hid
    # the row, it never destroyed what the student said.
    as_user(db, trusted)
    notes.db_set_cancelled(db, trusted, day, 1, "MC1101", False)
    as_user(db, mine)
    assert totals(db, mine, "MC1101") == (1, 2)


def test_only_a_trusted_member_may_call_a_class_off(db):
    """It changes 110 denominators, so it is the same bar as adding to the
    library -- and the database is what says so, not the handler."""
    me = student_with_a_monday(db, role="student")
    day = last_monday(db).isoformat()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        notes.db_set_cancelled(db, me, day, 1, "MC1101", True)


def test_retroactive_marking_works_and_an_empty_period_is_refused(db):
    """People forget. A screen that only marked today would be filled in by
    nobody -- and a date with no class on it is a typo, not a class."""
    me = student_with_a_monday(db)
    monday = last_monday(db)

    assert notes.db_mark_attendance(
        db, me, [{"date": monday.isoformat(), "period": 1, "state": "present"}]) == 1

    # Period 4 is free that Monday; Tuesday has nothing at all.
    with pytest.raises(ValueError, match="no class"):
        notes.db_mark_attendance(
            db, me, [{"date": monday.isoformat(), "period": 4, "state": "present"}])
    tuesday = monday + datetime.timedelta(days=1)
    with pytest.raises(ValueError, match="no class"):
        notes.db_mark_attendance(
            db, me, [{"date": tuesday.isoformat(), "period": 1, "state": "present"}])
    assert totals(db, me, "MC1101") == (1, 1), "and neither refusal wrote anything"


def test_tomorrow_has_not_happened_yet(db):
    me = student_with_a_monday(db)
    today = db.execute("select current_date").fetchone()[0]
    ahead = (today + datetime.timedelta(days=7)).isoformat()   # the next Monday
    with pytest.raises(ValueError, match="has not happened"):
        notes.db_mark_attendance(db, me, [{"date": ahead, "period": 1,
                                           "state": "present"}])


@pytest.mark.parametrize("mark", [
    {"date": "not-a-date", "period": 1, "state": "present"},
    {"date": None, "period": 1, "state": "present"},
    {"period": 1, "state": "present"},
    {"date": "2026-09-07", "period": 0, "state": "present"},
    {"date": "2026-09-07", "period": 99, "state": "present"},
    {"date": "2026-09-07", "state": "present"},
    {"date": "2026-09-07", "period": 1, "state": "maybe"},
    {"date": "2026-09-07", "period": 1},
])
def test_a_nonsense_mark_is_refused_before_it_reaches_the_database(db, mark):
    me = student_with_a_monday(db)
    with pytest.raises(ValueError):
        notes.db_mark_attendance(db, me, [mark])


def test_the_subject_comes_from_the_timetable_and_never_from_the_request(db):
    """Otherwise a mistyped -- or invented -- code would file a class under a
    subject the student was not sitting in, and quietly move a percentage."""
    me = student_with_a_monday(db)
    day = last_monday(db).isoformat()
    notes.db_mark_attendance(db, me, [{"date": day, "period": 2,
                                       "state": "present", "code": "BS1111"}])
    assert totals(db, me, "CY1107") == (1, 1), "period 2 on a Monday is Chemistry"
    assert totals(db, me, "BS1111") is None


def test_every_subject_on_the_timetable_appears_even_at_nothing_held(db):
    """A subject with no marks has to be able to say "nothing marked yet"
    rather than be missing from the screen entirely."""
    me = student_with_a_monday(db)
    out = notes.db_attendance(db, me)
    assert [s["code"] for s in out["subjects"]] == ["MC1101", "CY1107"]
    assert all(s["held"] == 0 and s["pct"] is None for s in out["subjects"])
    assert all(s["note"] == "No classes marked yet." for s in out["subjects"])


def test_your_attendance_is_yours_alone(db):
    """Two sessions on one connection. RLS is what hides it -- not a where
    clause in a handler that somebody can forget to write.

    This is the rule the whole feature stands on: attendance is not on the
    board, not in the admin panel, and not in anybody else's /data.
    """
    mine = student_with_a_monday(db)
    theirs = student_with_a_monday(db)
    day = last_monday(db).isoformat()

    as_user(db, mine)
    notes.db_mark_attendance(db, mine, [{"date": day, "period": 1, "state": "absent"}])

    as_user(db, theirs)
    assert db.execute("select count(*) from attendance").fetchone()[0] == 0, (
        "another student's marks are not readable at all")
    assert totals(db, theirs, "MC1101") == (0, 0)

    # Nor writable: not a new row in somebody else's name. Inside a savepoint,
    # because a refusal aborts the transaction and the rest of this test is the
    # half that matters.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction():
            db.execute("insert into attendance (profile_id, on_date, period, "
                       "subject_code, state) values (%s, %s, 2, 'CY1107', "
                       "'present')", (mine, day))

    # ...nor an edit of one that already exists. An update that matches no
    # visible row changes nothing, which is the same refusal by another name.
    db.execute("update attendance set state = 'present' where profile_id = %s",
               (mine,))
    db.execute("delete from attendance where profile_id = %s", (mine,))
    as_admin_connection(db)
    assert db.execute("select state from attendance where profile_id = %s",
                      (mine,)).fetchall() == [("absent",)], (
        "neither the update nor the delete may have reached it")


def test_a_pending_joiner_has_nothing_to_mark(db):
    """Approval is what makes you a member, and nothing writes before it."""
    uid = make_user(db)
    db.execute("insert into profiles (id, name, status) values (%s, 'Waiting', 'pending')",
               (uid,))
    as_user(db, uid)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into attendance (profile_id, on_date, period, "
                   "subject_code, state) values (%s, current_date, 1, 'MC1101', "
                   "'present')", (uid,))


# ---------------------------------------------------- the real server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """notes.py's own server over a real socket, with two students and a
    trusted member -- because "one student cannot see another's" is a claim
    about two sessions and cannot be made with one."""
    os.environ["RECARVE_SECRET"] = SECRET.decode()

    lib = tmp_path_factory.mktemp("library")
    folder = lib / "MC1101-Mathematics-1"
    (folder / "lectures").mkdir(parents=True)
    (folder / "lectures" / "week1.md").write_text("## Summary\nlimits\n")

    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("attendance", "cancelled_classes", "timetable", "votes",
                      "materials", "lectures", "profiles"):
            conn.execute(f"delete from {table}")
        for name, role in (("Asha", "student"), ("Bilal", "student"),
                           ("Cy", "trusted")):
            uid = make_user(conn)
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role, password) "
                "values (%s, %s, %s, 'approved', %s, 'a-real-password')",
                (uid, name, name, role))
            people[name] = uid
            for day, period, code in [(d["day"], d["period"], d["code"])
                                      for d in MONDAY]:
                conn.execute(
                    "insert into timetable (profile_id, day, period, subject_code) "
                    "values (%s, %s, %s, %s)", (uid, day, period, code))

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
        for table in ("attendance", "cancelled_classes", "timetable", "votes",
                      "materials", "lectures", "profiles", "auth.users"):
            conn.execute(f"delete from {table}")


def att(port, cookie):
    status, body, _ = call(port, "GET", "/data", cookie=cookie)
    assert status == 200, body
    return json.loads(body)["attendance"]


def monday(port, cookie):
    today = datetime.date.fromisoformat(att(port, cookie)["today"])
    return (today - datetime.timedelta(days=(today.isoweekday() - 1) % 7 or 7)
            ).isoformat()


def mark(port, cookie, day, period, state):
    return call(port, "POST", "/attendance",
                {"marks": [{"date": day, "period": period, "state": state}]},
                cookie=cookie)


def test_attendance_rides_on_the_request_home_already_makes(server):
    """Home marks today's classes and the subject screen prints the number;
    neither may cost a round trip of its own on mobile data in a corridor."""
    port, cookies = server
    a = att(port, cookies["Asha"])
    assert [s["code"] for s in a["subjects"]] == ["MC1101", "CY1107"]
    assert a["marks"] == [] and a["off"] == []
    # The date comes from the server, because the marks are stamped with it.
    assert datetime.date.fromisoformat(a["today"])


def test_marking_answers_with_the_whole_picture(server):
    """One place does this arithmetic and it is the server. A phone that
    recomputed the percentage itself is a second place for it to be wrong."""
    port, cookies = server
    day = monday(port, cookies["Asha"])

    status, body, _ = mark(port, cookies["Asha"], day, 1, "present")
    assert status == 200, body
    out = json.loads(body)
    mc = next(s for s in out["subjects"] if s["code"] == "MC1101")
    assert (mc["attended"], mc["held"], mc["pct"]) == (1, 1, 100.0)
    # One of one is 100%, and one miss would make it 50%. The sentence says
    # exactly that rather than a soothing "you are fine".
    assert mc["note"] == "Miss the next class and you drop below 75%."
    assert out["marks"] == [{"date": day, "period": 1, "code": "MC1101",
                             "state": "present"}]

    # Changed, then cleared.
    assert mark(port, cookies["Asha"], day, 1, "absent")[0] == 200
    mc = next(s for s in att(port, cookies["Asha"])["subjects"]
              if s["code"] == "MC1101")
    assert (mc["attended"], mc["held"]) == (0, 1)
    assert mark(port, cookies["Asha"], day, 1, "clear")[0] == 200
    assert att(port, cookies["Asha"])["marks"] == []


def test_a_whole_day_in_one_request(server):
    """A student catching up on a week they forgot must not need one round trip
    per period."""
    port, cookies = server
    day = monday(port, cookies["Bilal"])
    status, body, _ = call(port, "POST", "/attendance", {"marks": [
        {"date": day, "period": 1, "state": "present"},
        {"date": day, "period": 2, "state": "present"},
        {"date": day, "period": 3, "state": "absent"}]}, cookie=cookies["Bilal"])
    assert status == 200, body
    out = json.loads(body)
    assert {s["code"]: (s["attended"], s["held"]) for s in out["subjects"]} == {
        "MC1101": (1, 2), "CY1107": (1, 1)}
    for m in out["marks"]:
        call(port, "POST", "/attendance",
             {"marks": [{"date": m["date"], "period": m["period"], "state": "clear"}]},
             cookie=cookies["Bilal"])


def test_a_date_with_no_class_on_it_is_refused_with_a_reason(server):
    port, cookies = server
    day = monday(port, cookies["Asha"])
    status, body, _ = mark(port, cookies["Asha"], day, 5, "present")
    assert status == 400, body
    assert "no class" in json.loads(body)["error"]
    for bad in [("2026-13-40", 1, "present"), (day, 1, "maybe"), (day, 0, "present")]:
        assert mark(port, cookies["Asha"], *bad)[0] == 400
    assert call(port, "POST", "/attendance", {"marks": "monday"},
                cookie=cookies["Asha"])[0] == 400
    assert att(port, cookies["Asha"])["marks"] == [], "and nothing was written"


def test_one_student_never_sees_another(server):
    """The rule the whole feature stands on, over the wire and with two real
    sessions: attendance is private to the person it is about."""
    port, cookies = server
    day = monday(port, cookies["Asha"])
    assert mark(port, cookies["Asha"], day, 1, "absent")[0] == 200

    theirs = att(port, cookies["Bilal"])
    assert theirs["marks"] == [], "Asha's mark is not in Bilal's payload"
    assert all(s["held"] == 0 for s in theirs["subjects"])

    # Nor anywhere else the app hands a student. /me is their own contributions
    # and /standings is the class board; neither may carry a soul's attendance.
    for path in ("/me", "/standings"):
        status, body, _ = call(port, "GET", path, cookie=cookies["Bilal"])
        assert status == 200, body
        assert "attendance" not in body and "absent" not in body

    assert mark(port, cookies["Asha"], day, 1, "clear")[0] == 200


def test_calling_a_class_off_is_trusted_and_lands_on_everybody(server):
    port, cookies = server
    day = monday(port, cookies["Cy"])
    assert mark(port, cookies["Asha"], day, 2, "absent")[0] == 200
    cy = next(s for s in att(port, cookies["Asha"])["subjects"]
              if s["code"] == "CY1107")
    assert (cy["attended"], cy["held"]) == (0, 1)

    # A student is refused before the handler is ever reached: /cancelled is in
    # ROLE_REQUIRED, so curl gets the same answer the app's screens give.
    status, body, _ = call(port, "POST", "/cancelled",
                           {"date": day, "period": 2, "code": "CY1107", "off": True},
                           cookie=cookies["Asha"])
    assert status == 403, body
    assert json.loads(body)["required"] == "trusted"

    status, body, _ = call(port, "POST", "/cancelled",
                           {"date": day, "period": 2, "code": "CY1107", "off": True},
                           cookie=cookies["Cy"])
    assert status == 200, body
    for who in ("Asha", "Bilal", "Cy"):
        a = att(port, cookies[who])
        assert a["off"] == [{"date": day, "period": 2, "code": "CY1107", "reason": ""}]
        cy = next(s for s in a["subjects"] if s["code"] == "CY1107")
        assert cy["held"] == 0, "a class that did not happen counts for nobody"

    assert call(port, "POST", "/cancelled",
                {"date": day, "period": 2, "code": "CY1107", "off": False},
                cookie=cookies["Cy"])[0] == 200
    assert mark(port, cookies["Asha"], day, 2, "clear")[0] == 200


def test_a_stranger_has_no_attendance_to_mark(server):
    """Neither route is in PUBLIC_PATHS, so the gate refuses both before any
    handler sees them."""
    port, cookies = server
    day = monday(port, cookies["Asha"])
    for cookie in (None, "garbage"):
        assert call(port, "POST", "/attendance",
                    {"marks": [{"date": day, "period": 1, "state": "present"}]},
                    cookie=cookie)[0] == 403
        assert call(port, "POST", "/cancelled",
                    {"date": day, "period": 1, "code": "MC1101", "off": True},
                    cookie=cookie)[0] == 403
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute("select count(*) from attendance").fetchone()[0] == 0
        assert conn.execute("select count(*) from cancelled_classes") \
                   .fetchone()[0] == 0


def test_missing_attendance_cannot_take_the_library_with_it(server, monkeypatch):
    """Home's extras are extras. This table is newer than the database some
    machine is running, and a read that raises mid-reply reads to the phone as
    "no server" -- stale notes, no uploads, no votes."""
    port, cookies = server

    def gone(conn, user_id, window=None):
        raise psycopg.errors.UndefinedTable('relation "attendance" does not exist')

    monkeypatch.setattr(notes, "db_attendance", gone)
    status, body, _ = call(port, "GET", "/data", cookie=cookies["Asha"])
    assert status == 200, body
    d = json.loads(body)
    assert d["subjects"][0]["code"] == "MC1101", "the library is still served"
    assert "attendance" not in d, "the page reads a missing payload as none"
