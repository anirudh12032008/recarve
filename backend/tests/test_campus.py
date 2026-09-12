"""Clubs, events and the campus map, against the real database.

Three halves, the shape test_announcements.py set. The policies first, on a
transaction that rolls back: who may add a club, who may put an event up, and
what "remove" does to each of the three rows. Then the running server, because
"admins only" is worth exactly what the socket enforces. Then the page, because
a map that cannot draw and a places list that can are one screen, and the state
with no key is the state this app ships in today.
"""

import datetime
import json
import os
import pathlib
import re
import sys
import threading

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL, as_admin_connection, as_user, make_user  # noqa: E402
from test_auth import SECRET, call  # noqa: E402
from test_content import member  # noqa: E402

TODAY = datetime.date.today()
SOON = (TODAY + datetime.timedelta(days=10)).isoformat()
LATER = (TODAY + datetime.timedelta(days=12)).isoformat()
GONE = (TODAY - datetime.timedelta(days=3)).isoformat()


def a_club(db, name="Robotics Club", **kw):
    kw.setdefault("blurb", "Builds robots.")
    kw.setdefault("category", "Technical")
    kw.setdefault("tags", ["robotics", "AI"])
    kw.setdefault("link", None)
    kw.setdefault("contact", None)
    kw.setdefault("hidden", False)
    return notes.db_write_club(db, None, name, kw["blurb"], kw["category"],
                               kw["tags"], kw["link"], kw["contact"], kw["hidden"])


def an_event(db, who, title="Vidyut", starts=SOON, ends=None):
    return notes.db_write_event(db, who, None, title, "Evolve", starts, ends,
                                "MANIT campus", "Electric mobility.", None)


# ------------------------------------------------------------ the policies


def test_an_admin_adds_a_club_and_the_class_reads_it(db):
    boss, student = member(db, admin=True), member(db, role="student")
    as_user(db, boss)
    slug = a_club(db)
    assert slug == "robotics-club", "the slug comes off the name and is readable"

    as_user(db, student)
    got = notes.db_clubs(db)
    assert [c["name"] for c in got] == ["Robotics Club"]
    assert got[0]["tags"] == ["robotics", "AI"] and got[0]["category"] == "Technical"


@pytest.mark.parametrize("role", ["student", "trusted"])
def test_nobody_below_an_admin_can_add_a_club(db, role):
    """No handler decides this. The insert policy does, so a member holding a
    database connection gets no further than one holding a phone."""
    as_user(db, member(db, role=role))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        a_club(db, "Not mine")


def test_a_hidden_club_leaves_the_directory_but_not_the_table(db):
    boss, student = member(db, admin=True), member(db, role="student")
    as_user(db, boss)
    slug = a_club(db)
    notes.db_write_club(db, slug, None, None, None, None, None, None, True)

    as_user(db, student)
    assert notes.db_clubs(db) == [], "a hidden club is gone for the class"
    as_user(db, boss)
    assert [c["hidden"] for c in notes.db_clubs(db)] == [True], \
        "and still there for the admin who can put it back"


def test_a_second_club_of_the_same_name_does_not_overwrite_the_first(db):
    boss = member(db, admin=True)
    as_user(db, boss)
    assert a_club(db) == "robotics-club"
    assert a_club(db) == "robotics-club-2"
    assert len(notes.db_clubs(db)) == 2


def test_a_trusted_member_puts_an_event_up_and_everybody_reads_it(db):
    trusted, student = member(db), member(db, role="student")
    as_user(db, trusted)
    an_event(db, trusted)

    as_user(db, student)
    got = notes.db_events(db, student)
    assert [e["title"] for e in got] == ["Vidyut"]
    assert got[0]["date"] == SOON and got[0]["multi"] is False
    assert got[0]["mine"] is False


def test_a_student_cannot_put_an_event_up(db):
    """An event reaches a hundred and ten people's Coming up list, which is the
    same bar as adding to the library."""
    student = member(db, role="student")
    as_user(db, student)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        an_event(db, student)


def test_nobody_puts_an_event_up_in_somebody_else_s_name(db):
    mine, other = member(db), member(db)
    as_user(db, mine)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        an_event(db, other)


def test_a_trusted_member_cannot_edit_a_classmate_s_event(db):
    mine, other = member(db), member(db)
    as_user(db, mine)
    eid = an_event(db, mine)
    as_user(db, other)
    with pytest.raises(ValueError):
        notes.db_write_event(db, other, eid, "Hijacked", "", SOON, None, "", "", None)


def test_an_admin_takes_anybody_s_event_down_and_can_put_it_back(db):
    trusted, boss = member(db), member(db, admin=True)
    as_user(db, trusted)
    eid = an_event(db, trusted)

    as_user(db, boss)
    notes.db_write_event(db, boss, eid, None, None, None, None, None, None, True)
    assert [e["deleted"] for e in notes.db_events(db, boss)] == [True]

    student = None
    as_admin_connection(db)
    student = member(db, role="student")
    as_user(db, student)
    assert notes.db_events(db, student) == [], "taken down is gone for the class"

    as_user(db, boss)
    notes.db_write_event(db, boss, eid, None, None, None, None, None, None, False)
    assert [e["deleted"] for e in notes.db_events(db, boss)] == [False]


def test_an_event_that_has_finished_falls_off_by_itself(db):
    """Nobody tidies up. The filter is the day it ends, so yesterday's fest is
    gone this morning and its row is untouched."""
    trusted = member(db)
    as_user(db, trusted)
    an_event(db, trusted, "Last week", GONE)
    an_event(db, trusted, "Still on", GONE, TODAY.isoformat())
    assert [e["title"] for e in notes.db_events(db, trusted)] == ["Still on"]

    as_admin_connection(db)
    assert db.execute("select count(*) from events").fetchone()[0] == 2, \
        "falling off the list is not a delete"


def test_an_event_without_a_date_is_refused(db):
    """A date is the whole point of an event. Maffick with no date announced is
    deliberately not a row."""
    trusted = member(db)
    as_user(db, trusted)
    with pytest.raises(ValueError, match="needs a date"):
        notes.db_write_event(db, trusted, None, "Maffick", "", "", None, "", "", None)
    with pytest.raises(ValueError, match="ends before it starts"):
        notes.db_write_event(db, trusted, None, "Backwards", "", LATER, SOON,
                             "", "", None)


def test_events_reach_home_s_coming_up_list_beside_the_exam_dates(db):
    """One list, two sources. A student keeps one calendar, and each row says
    which kind of thing it is."""
    trusted = member(db)
    as_user(db, trusted)
    an_event(db, trusted, "Tooryanaad", SOON, LATER)

    up = notes.db_attendance(db, trusted)["upcoming"]
    mine = [u for u in up if u["title"] == "Tooryanaad"]
    assert mine, "the event never reached Coming up"
    assert mine[0]["what"] == "campus" and mine[0]["ends"] == LATER
    assert mine[0]["where"] == "Evolve · MANIT campus"
    assert all(u["what"] in ("campus", "academic") for u in up), \
        "every row has to say which kind it is"
    assert any(u["what"] == "academic" for u in up), \
        "the institute's own calendar is still in there"
    assert up == sorted(up, key=lambda u: u["date"]), "soonest first, whichever it is"


def test_a_past_event_is_not_on_home_either(db):
    trusted = member(db)
    as_user(db, trusted)
    an_event(db, trusted, "Long gone", GONE)
    up = notes.db_attendance(db, trusted)["upcoming"]
    assert not [u for u in up if u["title"] == "Long gone"]


# --------------------------------------------------------------- the places


def test_an_admin_adds_a_place_and_every_member_reads_it(db):
    boss, student = member(db, admin=True), member(db, role="student")
    as_user(db, boss)
    slug = notes.db_write_place(db, None, "Lecture Hall 6 (LH6)", "academic",
                                23.2170, 77.4076, None, "Section I's own hall.")
    as_user(db, student)
    got = notes.db_places(db)
    assert [p["slug"] for p in got] == [slug]
    assert got[0]["approx"] is True, \
        "a pin is approximate until somebody has stood on it"


def test_a_student_cannot_add_a_place(db):
    as_user(db, member(db, role="student"))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        notes.db_write_place(db, None, "Fake Gate", "gate", 23.2, 77.4, None, "")


def test_a_student_cannot_remove_one_either(db):
    """The delete policy refuses it; the row is simply not theirs to reach, so
    the delete matches nothing and this reads as "no such place"."""
    boss, student = member(db, admin=True), member(db, role="student")
    as_user(db, boss)
    notes.db_write_place(db, None, "Main Gate", "gate", 23.2141, 77.4053, None, "")
    as_user(db, student)
    with pytest.raises(ValueError):
        notes.db_remove_place(db, "main-gate")
    as_user(db, boss)
    assert [p["slug"] for p in notes.db_places(db)] == ["main-gate"], "still there"


def test_a_place_off_the_planet_is_refused_before_it_reaches_a_map(db):
    boss = member(db, admin=True)
    as_user(db, boss)
    with pytest.raises(ValueError, match="not on Earth"):
        notes.db_write_place(db, None, "Nowhere", "other", 991, 77.4, None, "")
    with pytest.raises(ValueError, match="not a number"):
        notes.db_write_place(db, None, "Nowhere", "other", "north-ish", 77.4, None, "")
    with pytest.raises(ValueError, match="is not one of"):
        notes.db_write_place(db, None, "Nowhere", "moon", 23.2, 77.4, None, "")


def test_a_place_really_is_removed_unlike_everything_else_here(db):
    boss = member(db, admin=True)
    as_user(db, boss)
    slug = notes.db_write_place(db, None, "Typo Hall", "academic", 23.2, 77.4,
                                None, "")
    notes.db_remove_place(db, slug)
    assert notes.db_places(db) == []


# ----------------------------------------------------------------- the key


def test_no_key_is_a_state_and_not_a_crash():
    cfg = notes.maps_config({})
    assert cfg["key"] is None and cfg["provider"] is None
    assert cfg["bounds"]["north"] > cfg["bounds"]["south"]
    assert cfg["bounds"]["east"] > cfg["bounds"]["west"]


def test_the_key_comes_from_the_environment_and_names_its_provider():
    cfg = notes.maps_config({"NEXT_PUBLIC_GOOGLE_MAPS_API_KEY": "k-123",
                             "RECARVE_MAPS_PROVIDER": "Jio"})
    assert cfg["key"] == "k-123" and cfg["provider"] == "jio"
    assert notes.maps_config({"NEXT_PUBLIC_GOOGLE_MAPS_API_KEY": "k"})["provider"] == "google"


def test_no_key_is_ever_baked_into_a_static_export(tmp_path, monkeypatch):
    """`notes.py export` writes PAGE with no server behind it. A key in that
    file is a key in the repository, so the page can only ever be handed one at
    runtime -- it never reads the environment itself."""
    monkeypatch.setenv("NEXT_PUBLIC_GOOGLE_MAPS_API_KEY", "sk-do-not-ship-me")
    assert notes.maps_config()["key"] == "sk-do-not-ship-me", "it is set"
    lib = tmp_path / "library"
    lib.mkdir()
    out = tmp_path / "site" / "index.html"
    notes.export(notes.argparse.Namespace(library=lib, out=out))
    assert "sk-do-not-ship-me" not in out.read_text()
    assert "maps_config" not in notes.PAGE, "the page never reads it for itself"


def test_the_campus_bounds_hold_the_seeded_places():
    """The box is what stops a dragged finger wandering across Bhopal. If it
    does not contain the pins, it is the wrong box."""
    seed = (pathlib.Path(__file__).resolve().parents[1] / "dev" / "seed_campus.sql"
            ).read_text()
    pins = re.findall(r"'(?:academic|hostel|food|sport|admin|gate|health|other)',"
                      r"\s*(-?\d+\.\d+),\s*(-?\d+\.\d+)", seed)
    assert len(pins) >= 15, "the seed file's pins have moved out of reach"
    b = notes.CAMPUS_BOUNDS
    for lat, lng in pins:
        assert b["south"] <= float(lat) <= b["north"], f"{lat} is outside the box"
        assert b["west"] <= float(lng) <= b["east"], f"{lng} is outside the box"


def test_the_seed_is_data_and_the_schema_is_the_migration():
    """The timetable does exactly this split. Rows in a migration would arrive
    in every test database and every test would have to filter around them."""
    mig = (pathlib.Path(__file__).resolve().parents[2] / "supabase" / "migrations")
    text = (mig / "0034_clubs_and_events.sql").read_text()
    assert "create table clubs" in text and "create table events" in text
    assert "Robotics Club" not in text, "the societies are data, not schema"
    seed = (pathlib.Path(__file__).resolve().parents[1] / "dev" / "seed_campus.sql"
            ).read_text()
    assert "create table" not in seed, "the seed file holds no schema"
    # And the fests with no announced date are deliberately absent as rows.
    assert "insert into events" in seed
    assert "'TechnoSearch'," not in seed, \
        "an event with no date must not be invented into a row"


# ---------------------------------------------------- the real server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """notes.py's own server on a real socket, with one person in each role."""
    os.environ["RECARVE_SECRET"] = SECRET.decode()
    os.environ.pop("NEXT_PUBLIC_GOOGLE_MAPS_API_KEY", None)
    lib = tmp_path_factory.mktemp("library")

    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("events", "clubs", "places", "announcement_reads",
                      "announcements", "votes", "materials", "lectures", "profiles"):
            conn.execute(f"delete from {table}")
        for name, role in (("Asha", "admin"), ("Bilal", "trusted"),
                           ("Chan", "student")):
            uid = make_user(conn)
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role, password) "
                "values (%s, %s, %s, 'approved', %s, 'a-real-password')",
                (uid, name, name, role))
            people[name] = uid

    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], {n: notes.sign_session(i, SECRET)
                                  for n, i in people.items()}

    srv.shutdown()
    srv.server_close()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("events", "clubs", "places", "profiles", "auth.users"):
            conn.execute(f"delete from {table}")


def campus(port, cookie):
    status, body, _ = call(port, "GET", "/campus", cookie=cookie)
    assert status == 200, body
    return json.loads(body)


def test_the_whole_tab_arrives_on_one_request(server):
    """Campus is opened once, on mobile data, between classes. Three lists is
    one request, not three."""
    port, cookies = server
    got = campus(port, cookies["Chan"])
    assert set(got) >= {"clubs", "events", "places", "maps"}


def test_only_an_admin_may_write_the_directory_or_the_map_over_the_socket(server):
    port, cookies = server
    for path, payload in (("/club", {"name": "IEEE MANIT Student Branch"}),
                          ("/place", {"name": "Main Gate", "kind": "gate",
                                      "lat": 23.2141, "lng": 77.4053})):
        for who in ("Chan", "Bilal"):
            status, body, _ = call(port, "POST", path, payload, cookie=cookies[who])
            assert status == 403, f"{who} wrote {path}: {body}"
            assert json.loads(body)["required"] == "admin"
        assert call(port, "POST", path, payload, cookie=cookies["Asha"])[0] == 200
    got = campus(port, cookies["Chan"])
    assert [c["name"] for c in got["clubs"]] == ["IEEE MANIT Student Branch"]
    assert [p["name"] for p in got["places"]] == ["Main Gate"]


def test_a_trusted_member_adds_an_event_and_a_student_cannot(server):
    port, cookies = server
    payload = {"title": "E-Summit", "society": "E-Cell", "date": SOON,
               "ends": LATER, "venue": "MANIT campus", "blurb": "Startups."}
    status, body, _ = call(port, "POST", "/event", payload, cookie=cookies["Chan"])
    assert status == 403, body
    assert json.loads(body)["required"] == "trusted"

    status, body, _ = call(port, "POST", "/event", payload, cookie=cookies["Bilal"])
    assert status == 200, body
    got = [e for e in json.loads(body)["events"] if e["title"] == "E-Summit"]
    assert got and got[0]["multi"] is True and got[0]["mine"] is True


def test_a_dateless_event_is_refused_over_the_socket(server):
    port, cookies = server
    status, body, _ = call(port, "POST", "/event", {"title": "Maffick"},
                           cookie=cookies["Bilal"])
    assert status == 400 and "date" in json.loads(body)["error"]


def test_nobody_signed_out_reaches_any_of_it(server):
    port, _ = server
    for path in ("/campus", "/club", "/event", "/place"):
        assert call(port, "GET" if path == "/campus" else "POST", path,
                    None if path == "/campus" else {}, cookie="garbage")[0] == 403


def test_with_no_key_the_server_still_answers_with_the_places(server):
    """The state this app ships in today: no key, and a screen that is still
    worth opening."""
    port, cookies = server
    got = campus(port, cookies["Chan"])
    assert got["maps"]["key"] is None and got["maps"]["provider"] is None
    assert got["places"], "the list is the useful half and does not need a key"


# ------------------------------------------------------------- the screen


def test_the_page_asks_for_campus_once_and_only_on_that_tab():
    """Home already has the events it needs, folded into Coming up by the
    server. The tab must not cost Home a request."""
    assert "fetch('/campus')" in notes.PAGE
    assert notes.PAGE.count("fetch('/campus')") == 1
    assert "if (CAMPUS || campusAsked || !live) return;" in notes.PAGE


def test_the_missing_key_is_a_screen_and_not_a_broken_map():
    """No key must read as a state somebody can act on, with the list still
    under it."""
    section = re.search(r"function mapSection\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "} else if (!cfg.key) {" in section
    assert "NEXT_PUBLIC_GOOGLE_MAPS_API_KEY" in section, "it has to say what would fix it"
    assert section.index("!cfg.key") < section.index("placesSection()"), \
        "the places list is drawn whether or not there is a key"


def test_the_provider_is_one_adapter_and_nothing_else_knows_about_it():
    """Two were named and neither key exists. The seam has to be in one
    object, or filling it in later is a search through the whole page."""
    block = re.search(r"const MAP_PROVIDERS = \{.*?\n\};", notes.PAGE, re.S).group(0)
    assert "google:" in block and "jio:" in block
    assert "strictBounds: true" in block, "the map must not wander off campus"
    # Nothing outside the adapter reaches for a provider's SDK.
    outside = notes.PAGE.replace(block, "")
    assert "google.maps" not in outside
    assert "maps.googleapis.com" not in outside


def test_nothing_is_fetched_from_a_provider_without_a_key():
    """With no key the app is still an offline app: no script tag, no request,
    nothing that phones home."""
    draw = re.search(r"function drawMap\(prov, cfg, box\) \{.*?\n\}",
                     notes.PAGE, re.S).group(0)
    assert "prov.src(cfg.key)" in draw
    section = re.search(r"function mapSection\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert section.index("!cfg.key") < section.index("drawMap("), \
        "drawMap is only ever reached with a key in hand"
    assert "<script src=\"https://maps." not in notes.PAGE


def test_directions_need_no_key_and_hand_off_to_the_phone():
    fn = re.search(r"function directionsFor\(p\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "maps/dir/?api=1&destination=" in fn
    assert "key" not in fn, "a directions link must not carry one"


def test_an_approximate_pin_says_so_on_its_own_row():
    """An approximate pin honestly labelled beats a confident wrong one, and
    this is the line that labels it."""
    card = re.search(r"function placeCard\(p\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "'Approximate pin'" in card and "'No pin yet'" in card


def test_the_screen_never_invents_a_number_nobody_is_behind():
    """No RSVP here, so no "N going". A count with nothing behind it on a
    screen a hundred and ten people read is worse than a blank space."""
    campus_js = notes.PAGE[notes.PAGE.index("function eventCard(e)"):
                           notes.PAGE.index("async function renderCampus()")]
    for invented in ("going", "interested", "attending", "members"):
        assert invented not in campus_js.lower(), \
            f"the campus screen counts {invented}, and nothing is behind it"


def test_home_s_coming_up_says_which_kind_of_thing_each_row_is():
    block = re.search(r"function countdownBlock\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "'Campus event'" in block and "'Academic calendar'" in block


def test_a_club_and_an_event_are_never_interpolated_into_html():
    """These are words other people typed, drawn on a hundred and ten phones."""
    for fn in ("eventCard", "clubCard", "placeCard"):
        body = re.search(rf"function {fn}\((\w+)\) \{{.*?\n\}}",
                         notes.PAGE, re.S).group(0)
        assert "innerHTML" not in body, f"{fn} builds HTML out of typed words"
        assert "textContent" in body
