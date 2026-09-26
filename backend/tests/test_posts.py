"""The wall: what the section says with its name on it, and what it says without.

The first half drives the policies and the privileges on a transaction that
rolls back. The second starts notes.py's own server and reads it as an
attacker: a second member with a valid session of their own, asking every
endpoint that exists, looking for one uuid and one name. If either appears
anywhere in any payload, the feature is broken no matter what the screen shows.

The single load-bearing fact is that `authenticated` has no select privilege on
posts.author_id at all. Every other assertion here is a second line of defence
behind that one, and the test that proves it is
test_a_member_cannot_select_the_author_column_at_all.
"""

import json
import os
import pathlib
import sys
import threading

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL, as_admin_connection, as_user, make_user  # noqa: E402
from test_auth import SECRET, call  # noqa: E402
from test_content import member  # noqa: E402


# --------------------------------------------------------- the policies


def test_a_feed_post_says_who_wrote_it(db):
    me, reader = member(db), member(db)
    as_user(db, me)
    notes.db_post(db, me, "feed", "Lost a blue water bottle in LT3.")

    as_user(db, reader)
    wall = notes.db_posts(db, reader, "feed")
    assert [(p["body"], p["by"], p["mine"]) for p in wall] == [
        ("Lost a blue water bottle in LT3.", "M", False)]


def test_a_confession_says_nobody(db):
    me, reader = member(db), member(db)
    as_user(db, me)
    notes.db_post(db, me, "confession", "I have never once done the pre-reading.")

    as_user(db, reader)
    [c] = notes.db_posts(db, reader, "confession")
    assert c["by"] is None, "a confession must not carry a name"
    assert c["mine"] is False
    # Not an id, not a hash of one, not anything shaped like one.
    assert me not in json.dumps(c), "the author's id is in the payload"
    assert set(c) == {"id", "body", "by", "at", "mine", "votes", "voted",
                      "photos", "batch"}, \
        "a new key on a confession is a new place for an author to hide"


def test_its_own_author_is_told_nothing_either(db):
    """`mine` is false on your own confession, because the column it would
    come from is byline_id, and byline_id is null for every confession there
    is. Nothing on this screen orders or marks by author."""
    me = member(db)
    as_user(db, me)
    notes.db_post(db, me, "confession", "It was me who broke the projector.")
    assert notes.db_posts(db, me, "confession")[0]["mine"] is False


def test_a_member_cannot_select_the_author_column_at_all(db):
    """The whole feature, in one assertion.

    Not "the query does not ask for it" -- Postgres refuses to answer if it
    does. A second server written later, a handler edited in a hurry, or psql
    holding a member's own claims all hit the same wall.
    """
    me = member(db)
    as_user(db, me)
    notes.db_post(db, me, "confession", "anonymous words")
    with pytest.raises(Exception) as exc:
        db.execute("select author_id from posts").fetchall()
    assert "permission denied" in str(exc.value).lower(), exc.value
    db.rollback()


def test_select_star_is_refused_too(db):
    """The lazy query is the dangerous one, so it must not work either."""
    me = member(db)
    as_user(db, me)
    with pytest.raises(Exception) as exc:
        db.execute("select * from posts").fetchall()
    assert "permission denied" in str(exc.value).lower(), exc.value
    db.rollback()


def test_the_database_still_knows(db):
    """Anonymous to the class, never to the table. An admin has to be able to
    deal with a person and not only with a row."""
    me = member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "confession", "something vile")
    as_admin_connection(db)
    assert str(db.execute("select author_id from posts where id = %s",
                          (pid,)).fetchone()[0]) == me


def test_only_an_admin_walks_the_deliberate_route(db):
    boss, me, nosy = member(db, admin=True), member(db), member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "confession", "something vile")

    as_user(db, nosy)
    with pytest.raises(ValueError):
        notes.db_confession_author(db, pid)

    as_user(db, boss)
    assert notes.db_confession_author(db, pid) == "M"


def test_the_route_refuses_a_feed_post(db):
    """It is not a general "who wrote this" lookup -- a feed post already says
    so on the screen, and a function that answers for both is a function
    somebody points at the wrong table of ids."""
    boss, me = member(db, admin=True), member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "feed", "normal words")
    as_user(db, boss)
    with pytest.raises(ValueError):
        notes.db_confession_author(db, pid)


def test_three_confessions_a_day_and_no_more(db):
    me = member(db)
    as_user(db, me)
    for i in range(3):
        notes.db_post(db, me, "confession", f"number {i}")
    with pytest.raises(psycopg.errors.RaiseException) as exc:
        notes.db_post(db, me, "confession", "number four")
    assert "tomorrow" in str(exc.value)
    db.rollback()


def test_the_limit_is_per_person(db):
    a, b = member(db), member(db)
    as_user(db, a)
    for i in range(3):
        notes.db_post(db, a, "confession", f"a{i}")
    as_user(db, b)
    notes.db_post(db, b, "confession", "b0")     # must not be refused
    assert len(notes.db_posts(db, b, "confession")) == 4


def test_the_limit_does_not_touch_the_named_feed(db):
    """A confession is rationed because it is unattributable. Words with your
    name on them are not, and rationing them would be a different feature."""
    me = member(db)
    as_user(db, me)
    for i in range(6):
        notes.db_post(db, me, "feed", f"post {i}")
    assert len(notes.db_posts(db, me, "feed")) == 6


def test_a_confession_cannot_carry_a_photo(db):
    """A photo is the one thing that names somebody without naming them."""
    me = member(db)
    as_user(db, me)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "insert into posts (kind, author_id, body, batch_id) values "
            "('confession', %s, 'look at this', gen_random_uuid())", (me,))
    db.rollback()


def test_the_handler_drops_a_batch_from_a_confession_rather_than_failing(db):
    """The check above is the referee; this is the page never reaching it."""
    me = member(db)
    as_user(db, me)
    notes.db_post(db, me, "confession", "words",
                  batch="11111111-1111-1111-1111-111111111111")
    assert notes.db_posts(db, me, "confession")[0]["batch"] is None


def test_nobody_posts_in_somebody_elses_name(db):
    me, other = member(db), member(db)
    as_user(db, me)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("insert into posts (kind, author_id, body) "
                   "values ('feed', %s, 'not mine')", (other,))
    db.rollback()


def test_a_pending_joiner_reads_and_writes_nothing(db):
    """is_approved(), which is the gate every other feature here uses."""
    me = member(db)
    as_user(db, me)
    notes.db_post(db, me, "feed", "members only")

    pending = make_user(db)
    as_admin_connection(db)
    db.execute("insert into profiles (id, name, status, role) "
               "values (%s, 'P', 'pending', 'student')", (pending,))
    as_user(db, pending)
    assert notes.db_posts(db, pending, "feed") == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        notes.db_post(db, pending, "feed", "let me in")
    db.rollback()


def test_taking_your_own_words_back(db):
    me, reader = member(db), member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "feed", "wrong room, sorry")
    notes.db_hide_post(db, pid)
    as_user(db, reader)
    assert notes.db_posts(db, reader, "feed") == []
    as_admin_connection(db)
    assert db.execute("select count(*) from posts").fetchone()[0] == 1, \
        "hidden, not deleted -- a hundred people may have read it"


def test_a_second_member_cannot_take_down_what_you_wrote(db):
    me, other = member(db), member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "feed", "mine")
    as_user(db, other)
    with pytest.raises(ValueError):
        notes.db_hide_post(db, pid)
    assert len(notes.db_posts(db, other, "feed")) == 1


def test_an_admin_takes_down_a_confession_in_one_tap(db):
    boss, me = member(db, admin=True), member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "confession", "something vile")
    as_user(db, boss)
    notes.db_hide_post(db, pid)
    assert notes.db_posts(db, boss, "confession") == []


def test_a_post_is_not_editable(db):
    """Words a hundred people have read must not become different words."""
    me = member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "feed", "as written")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("update posts set body = 'something else' where id = %s", (pid,))
    db.rollback()


def test_nothing_reachable_from_a_session_destroys_a_post(db):
    boss, me = member(db, admin=True), member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "feed", "on the record")
    as_user(db, boss)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("delete from posts where id = %s", (pid,))
    db.rollback()


def test_the_class_upvotes_a_post_on_the_same_votes_table(db):
    me, voter = member(db), member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "feed", "the lab is moved to Thursday")
    as_user(db, voter)
    assert notes.db_vote(db, pid, voter, True, "post_id") == {"votes": 1, "voted": True}
    assert notes.db_posts(db, voter, "feed")[0]["votes"] == 1
    as_admin_connection(db)
    assert db.execute(
        "select count(*) from votes where post_id = %s", (pid,)).fetchone()[0] == 1


def test_you_cannot_upvote_your_own(db):
    me = member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "feed", "vote for me")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        notes.db_vote(db, pid, me, True, "post_id")
    db.rollback()


def test_a_vote_names_exactly_one_thing(db):
    """The three-way check in 0035: a row may not be a vote for a post and an
    upload at once, and it may not be a vote for nothing."""
    me = member(db)
    as_user(db, me)
    pid = notes.db_post(db, me, "feed", "x")
    as_admin_connection(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into votes (voter_id) values (%s)", (me,))
    db.rollback()


def test_photos_ride_on_the_upload_path_that_already_exists(db):
    """No post_photos table: a batch id names materials rows the upload
    handler wrote, exactly as a grouped upload already does."""
    me, reader = member(db), member(db)
    batch = "22222222-2222-2222-2222-222222222222"
    as_admin_connection(db)
    for n in ("board-1.jpg", "board-2.jpg"):
        db.execute(
            "insert into materials (subject_code, uploader_id, filename, "
            "file_key, size_bytes, batch_id) values "
            "('CY1107', %s, %s, %s, 10, %s)",
            (me, n, "/lib/CY1107-Chemistry/uploads/" + n, batch))
    as_user(db, me)
    notes.db_post(db, me, "feed", "the board from today", batch=batch)

    as_user(db, reader)
    [p] = notes.db_posts(db, reader, "feed", relative_to="/lib")
    assert [x["name"] for x in p["photos"]] == ["board-1.jpg", "board-2.jpg"]
    assert p["photos"][0]["path"] == "CY1107-Chemistry/uploads/board-1.jpg"


def test_an_empty_post_is_refused(db):
    me = member(db)
    as_user(db, me)
    with pytest.raises(ValueError):
        notes.db_post(db, me, "feed", "   \n ")


def test_a_made_up_wall_is_refused(db):
    me = member(db)
    as_user(db, me)
    with pytest.raises(ValueError):
        notes.db_post(db, me, "shouting", "hello")


# ---------------------------------------------------- the real server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """notes.py's own server on a real socket, with one person in each role."""
    os.environ["RECARVE_SECRET"] = SECRET.decode()
    lib = tmp_path_factory.mktemp("library")

    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("votes", "posts", "doubts", "materials", "lectures",
                      "profiles"):
            conn.execute(f"delete from {table}")
        for name, role in (("Asha", "admin"), ("Bilal", "trusted"),
                           ("Qasim", "student"), ("Dia", "student")):
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
    yield srv.server_address[1], people, {n: notes.sign_session(i, SECRET)
                                          for n, i in people.items()}

    srv.shutdown()
    srv.server_close()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("votes", "posts", "doubts", "lectures", "profiles",
                      "auth.users"):
            conn.execute(f"delete from {table}")


@pytest.fixture(autouse=True)
def _clean_wall(request):
    """Every server test starts with an empty wall: these share one database
    and a confession left behind is another test's fourth one today."""
    if "server" in request.fixturenames:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("delete from votes where post_id is not null")
            conn.execute("delete from posts")
    yield


def post(port, cookie, **payload):
    return call(port, "POST", "/posts", payload, cookie=cookie)


def wall(port, cookie, kind="feed"):
    status, body, _ = call(port, "GET", f"/posts?kind={kind}", cookie=cookie)
    assert status == 200, body
    return json.loads(body)["posts"]


def test_any_approved_member_posts_to_the_feed(server):
    port, _, who = server
    status, body, _ = post(port, who["Qasim"], kind="feed",
                           body="Anybody found a black umbrella?")
    assert status == 200, body
    assert [(p["by"], p["body"]) for p in json.loads(body)["posts"]] == [
        ("Qasim", "Anybody found a black umbrella?")]


def test_a_second_member_learns_nothing_about_a_confession(server):
    """The attacker's test. One member confesses; another, with a real session
    of their own, asks everything this server answers and looks for the
    author's name and id in the bytes that come back."""
    port, ids, who = server
    status, body, _ = post(port, who["Qasim"], kind="confession",
                           body="I sleep through every 8am.")
    assert status == 200, body
    [c] = json.loads(body)["posts"]

    for path in ("/posts?kind=confession", "/posts?kind=feed", "/data",
                 "/campus", "/standings", f"/doubts?subject=CY1107",
                 f"/confession-author?id={c['id']}"):
        status, seen, _ = call(port, "GET", path, cookie=who["Dia"])
        assert ids["Qasim"] not in seen, f"{path} leaked the author's id"
        assert "Qasim" not in seen, f"{path} leaked the author's name"

    # And the write paths, which answer with the wall as well.
    for payload in ({"post": c["id"], "on": True}, {"post": c["id"], "on": False}):
        _, seen, _ = call(port, "POST", "/vote", payload, cookie=who["Dia"])
        assert ids["Qasim"] not in seen and "Qasim" not in seen
    _, seen, _ = post(port, who["Dia"], kind="confession", body="me too")
    assert ids["Qasim"] not in seen and "Qasim" not in seen


def test_a_member_asking_who_wrote_it_is_refused_by_the_route(server):
    port, _, who = server
    _, body, _ = post(port, who["Qasim"], kind="confession", body="secret")
    cid = json.loads(body)["posts"][0]["id"]
    for name in ("Dia", "Bilal"):
        status, seen, _ = call(port, "GET", f"/confession-author?id={cid}",
                               cookie=who[name])
        assert status == 403, f"{name} got {status}: {seen}"


def test_an_admin_can_find_out_who_wrote_one(server):
    """Because somebody will post something vile, and an admin has to be able
    to deal with the person and not only the row."""
    port, _, who = server
    _, body, _ = post(port, who["Qasim"], kind="confession", body="something vile")
    cid = json.loads(body)["posts"][0]["id"]
    status, seen, _ = call(port, "GET", f"/confession-author?id={cid}",
                           cookie=who["Asha"])
    assert status == 200 and json.loads(seen)["by"] == "Qasim", seen


def test_the_wall_itself_never_tells_the_admin_either(server):
    """Only the deliberate route. An admin reading the screen everybody reads
    sees what everybody sees, so a shoulder in a corridor learns nothing."""
    port, ids, who = server
    post(port, who["Qasim"], kind="confession", body="quiet words")
    _, seen, _ = call(port, "GET", "/posts?kind=confession", cookie=who["Asha"])
    assert "Qasim" not in seen and ids["Qasim"] not in seen


def test_a_fourth_confession_today_is_refused_over_http(server):
    port, _, who = server
    for i in range(3):
        assert post(port, who["Dia"], kind="confession", body=f"n{i}")[0] == 200
    status, body, _ = post(port, who["Dia"], kind="confession", body="n3")
    assert status == 429, body
    assert "tomorrow" in json.loads(body)["error"]


def test_an_admin_takes_a_confession_down_over_http(server):
    port, _, who = server
    _, body, _ = post(port, who["Qasim"], kind="confession", body="take me down")
    cid = json.loads(body)["posts"][0]["id"]
    status, body, _ = post(port, who["Asha"], kind="confession", id=cid, delete=True)
    assert status == 200 and json.loads(body)["posts"] == [], body


def test_a_bystander_cannot_take_a_post_down_over_http(server):
    port, _, who = server
    _, body, _ = post(port, who["Qasim"], kind="feed", body="not yours")
    pid = json.loads(body)["posts"][0]["id"]
    status, _, _ = post(port, who["Dia"], kind="feed", id=pid, delete=True)
    assert status == 400
    assert len(wall(port, who["Dia"])) == 1


def test_a_signed_out_caller_gets_nowhere(server):
    port, _, _ = server
    for method, path in (("GET", "/posts"), ("GET", "/confession-author?id=x"),
                         ("POST", "/posts")):
        assert call(port, method, path,
                    {} if method == "POST" else None)[0] in (302, 401, 403)


def test_the_page_puts_a_confession_on_the_screen_as_typing(server):
    """The one screen where somebody deliberately tries a tag. Asserted on the
    served page rather than in a browser: the rule is that this text reaches
    the DOM through textContent, and innerHTML anywhere near it is the bug."""
    port, _, who = server
    status, page, _ = call(port, "GET", "/", cookie=who["Dia"])
    assert status == 200
    fn = page[page.index("function postCard("):]
    fn = fn[:fn.index("\nfunction ")]
    assert "innerHTML" not in fn, "a post must never be parsed as markup"
    assert "textContent" in fn
