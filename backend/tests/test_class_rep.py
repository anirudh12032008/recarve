"""The class representative: everything trusted may do, plus the notice board.

Read beside test_roles.py, which owns the ladder as a whole. This file owns the
one rung 0045 added and the one question it was added to answer -- and it asks
that question the only way that is worth anything, on real connections under
RLS. A handler that forgot its check is a bug; a policy that lets the wrong
person post is the bug that cannot be fixed by editing notes.py, because a
leaked connection string never goes through notes.py at all.

Every test here has been proved by breaking the thing it covers -- the policy,
the check constraint, the generated column, the route table -- and watching it
fail. None of them is a test that passes because nothing is wired up.
"""

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
from test_content import member  # noqa: E402
from test_roles import SECRET, call  # noqa: E402


def post(db, author, title="Lab moved to Friday"):
    return notes.db_write_announcement(db, author, None, title, "", False, None)


# ------------------------------------------------------ the ladder in Postgres
#
# role_rank() is the only place the order is written down on the database side,
# and three things read it: the check constraint on profiles.role, at_least()
# under every is_*() helper, and the stored generated column profiles.trusted.
# The last of those is the one that can drift silently, so it has its own test
# below rather than being trusted to stay right.


def test_the_ladder_is_the_order_the_app_believes_in(db):
    """Postgres and notes.py must agree about which role outranks which, or the
    handler's 403 and the policy's refusal stop being the same rule."""
    ranks = [db.execute("select role_rank(%s)", (r,)).fetchone()[0] for r in notes.ROLES]
    assert ranks == sorted(ranks), f"the database orders the roles differently: {ranks}"
    assert len(set(ranks)) == len(notes.ROLES), "two roles cannot share a rung"
    assert db.execute("select role_rank('root')").fetchone()[0] is None, \
        "a role that is not on the ladder has no rank, which is what the check uses"


def test_cr_is_a_role_the_column_accepts_and_junk_is_still_not(db):
    uid = make_user(db)
    db.execute("insert into profiles (id, name, role) values (%s, 'Rep', 'cr')", (uid,))
    assert db.execute("select role from profiles where id = %s", (uid,)).fetchone()[0] == "cr"
    other = make_user(db)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("insert into profiles (id, name, role) values (%s, 'X', 'root')", (other,))


@pytest.mark.parametrize("role", notes.ROLES)
def test_the_old_boolean_is_role_rank_spelled_the_old_way(db, role):
    """profiles.trusted is a STORED generated column, so it carries whatever
    role_rank() said at the moment the row was written. Change the ladder in
    role_rank() without re-adding the column and every existing row keeps
    answering with the old one -- silently, and only on the rows nobody
    happened to touch. This is the test that says so: it compares the stored
    value against the function's answer right now, for every role.
    """
    uid = member(db, role=role)
    stored, live = db.execute(
        "select trusted, role_rank(role) >= role_rank('trusted') "
        "from profiles where id = %s", (uid,)).fetchone()
    assert stored == live, \
        f"profiles.trusted for {role} was stored under a different ladder"


@pytest.mark.parametrize("role,trusted,cr,admin", [
    ("student", False, False, False),
    ("trusted", True, False, False),
    ("cr", True, True, False),
    ("admin", True, True, True)])
def test_each_rung_answers_for_itself_and_everything_below(db, role, trusted, cr, admin):
    """The whole point of a ladder rather than a list: a cr satisfies the
    trusted test without anybody having written 'cr' into it."""
    uid = member(db, role=role)
    as_user(db, uid)
    assert db.execute("select is_trusted(), is_cr(), is_admin()").fetchone() \
        == (trusted, cr, admin)


def test_a_blocked_cr_is_nobody(db):
    """status is the other axis and it outranks the ladder entirely -- the same
    rule 0009 wrote for every other role, asked again of the new one."""
    uid = member(db, role="cr")
    as_admin_connection(db)
    db.execute("update profiles set status = 'blocked' where id = %s", (uid,))
    as_user(db, uid)
    assert db.execute("select is_cr(), is_trusted()").fetchone() == (False, False)


# --------------------------------------------------- the notice board, under RLS


def test_a_cr_posts_an_announcement(db):
    """The one power the role exists for, taken straight at Postgres. No
    handler is involved: this is what a cr with a psycopg connection can do,
    which is the only version of "can" that means anything."""
    rep, student = member(db, role="cr"), member(db, role="student")
    as_user(db, rep)
    post(db, rep, "Chemistry lab moved")

    as_user(db, student)
    got = notes.db_announcements(db, student)
    assert [a["title"] for a in got] == ["Chemistry lab moved"]
    assert got[0]["mine"] is False, "the board says who wrote it, and it was not them"


def test_a_trusted_member_still_cannot_post_one(db):
    """The rung above trusted exists precisely so that this stays refused.
    Trusted is what spends the API budget and adds to the library; telling a
    hundred and ten people something at once is a different thing, and if this
    test ever passes the new role has bought nothing."""
    trusted = member(db, role="trusted")
    as_user(db, trusted)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        post(db, trusted)


def test_a_cr_cannot_post_in_somebody_elses_name(db):
    """0023's rule, which the widened policy had to keep: the row carries the
    author's name to a hundred and ten phones, so author_id is pinned to
    whoever is asking however the insert is spelled."""
    rep, boss = member(db, role="cr"), member(db, role="admin")
    as_user(db, rep)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("insert into announcements (author_id, title) values (%s, 'Not mine')",
                   (boss,))


def test_a_cr_edits_their_own_notice_and_not_the_admin_s(db):
    """"Their own, and only their own" was true of two admins and has to stay
    true across rungs -- a cr editing the admin's notice would leave the wrong
    name on the changed words."""
    rep, boss = member(db, role="cr"), member(db, role="admin")
    as_user(db, boss)
    theirs = post(db, boss, "From the admin")
    as_user(db, rep)
    mine = post(db, rep, "From the CR")

    db.execute("update announcements set title = 'Edited' where id = %s", (mine,))
    assert db.execute("update announcements set title = 'Hijacked' where id = %s",
                      (theirs,)).rowcount == 0
    as_admin_connection(db)
    assert db.execute("select title from announcements where id = %s",
                      (theirs,)).fetchone()[0] == "From the admin"


def test_an_admin_can_still_post(db):
    """Admin is above cr on the ladder, so widening the policy downward must not
    have moved the top of it."""
    boss = member(db, role="admin")
    as_user(db, boss)
    post(db, boss, "From the admin")


# ------------------------------------------------ and everything trusted could do


@pytest.mark.parametrize("write", ["material", "lecture"])
def test_a_cr_can_still_upload(db, write):
    """A cr is a trusted member plus one thing, so the insert policies that say
    is_trusted() have to let one through without ever naming the role."""
    rep = member(db, role="cr")
    as_user(db, rep)
    if write == "material":
        db.execute("insert into materials (subject_code, uploader_id, filename, "
                   "file_key, size_bytes) values ('CY1107', %s, 'n.pdf', 'cr-k', 10)",
                   (rep,))
    else:
        db.execute("insert into lectures (subject_code, uploader_id, audio_key) "
                   "values ('CY1107', %s, 'cr-a')", (rep,))


def test_a_crs_upload_is_published_and_not_held_for_approval(db):
    """The quiet half of "can still upload". force_pending_for_untrusted() reads
    profiles.trusted, not is_trusted(), so a cr whose generated column was left
    on the old ladder would upload successfully and then watch every file sit
    invisible at 'pending' with nothing to explain why."""
    rep = member(db, role="cr")
    as_user(db, rep)
    status = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, "
        "size_bytes) values ('CY1107', %s, 'n.pdf', 'cr-pub', 10) returning status",
        (rep,)).fetchone()[0]
    assert status == "visible", "a cr's upload must not be held back for approval"


def test_a_cr_runs_no_part_of_the_class(db):
    """The reason the role exists at all is that the admin's powers were too
    much to hand over for one notice board. So: not an admin."""
    rep, victim = member(db, role="cr"), member(db, role="student")
    as_user(db, rep)
    assert db.execute("update profiles set role = 'admin' where id = %s",
                      (victim,)).rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("update profiles set role = 'admin' where id = %s", (rep,))


# ------------------------------------------------------ and through the handler


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The real handler on a real socket, with an admin, a trusted member and a
    student. There is deliberately no cr here at the start: the point of the
    test below is that an admin makes one through the route the admin panel
    actually posts to.
    """
    os.environ["RECARVE_SECRET"] = SECRET.decode()
    lib = tmp_path_factory.mktemp("library")

    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for table in ("announcement_reads", "announcements", "votes", "materials",
                      "lectures", "profiles"):
            conn.execute(f"delete from {table}")
        for role in ("student", "trusted", "admin"):
            uid = make_user(conn)
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role, password) "
                "values (%s, %s, %s, 'approved', %s, 'a-real-password')",
                (uid, role.title(), "cr-" + role, role))
            people[role] = uid

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
        for table in ("announcement_reads", "announcements", "votes", "materials",
                      "lectures", "profiles", "auth.users"):
            conn.execute(f"delete from {table}")


def test_an_admin_grants_cr_and_the_board_opens_on_the_next_request(server):
    """The whole feature end to end, through the handler the admin panel posts
    to. A trusted member is refused the notice board, an admin makes them a cr
    over /role, and the same cookie posts a notice on its next request -- no
    new login, because the role is read per request and not carried in the
    session.
    """
    port, cookies, people = server
    them = people["trusted"]

    code, out = call(port, "POST", "/announce", {"title": "Too soon"}, cookies["trusted"])
    assert code == 403 and json.loads(out)["required"] == "cr", out[:200]

    code, out = call(port, "POST", "/role", {"id": them, "role": "cr"}, cookies["admin"])
    assert code == 200 and json.loads(out)["role"] == "cr", out[:200]
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute("select role from profiles where id = %s",
                            (them,)).fetchone()[0] == "cr"

    code, out = call(port, "POST", "/announce", {"title": "Lab moved to Friday"},
                     cookies["trusted"])
    assert code == 200, out[:200]
    assert [a["title"] for a in json.loads(out)["announcements"]] == ["Lab moved to Friday"]

    # And back down again, because a role an admin cannot take away is not a
    # role an admin granted.
    assert call(port, "POST", "/role", {"id": them, "role": "trusted"},
                cookies["admin"])[0] == 200
    code, out = call(port, "POST", "/announce", {"title": "Not any more"},
                     cookies["trusted"])
    assert code == 403 and json.loads(out)["required"] == "cr"


def test_a_student_is_still_refused_the_board_and_leaves_nothing_behind(server):
    port, cookies, _ = server
    code, out = call(port, "POST", "/announce", {"title": "Sneak"}, cookies["student"])
    assert code == 403 and json.loads(out)["required"] == "cr"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        assert conn.execute("select count(*) from announcements where title = 'Sneak'"
                            ).fetchone()[0] == 0


def test_the_admin_panel_offers_every_rung_it_can_grant(server):
    """A role nobody can be given is a role that does not exist. The dropdown is
    built from ROLES rather than from a list typed into the page, and this is
    what says the page really was handed the server's own tuple."""
    port, cookies, _ = server
    code, page = call(port, "GET", "/admin", cookie=cookies["admin"])
    assert code == 200
    assert json.dumps(list(notes.ROLES)) in page, "the panel was not handed the ladder"
    # And it has to actually build the dropdown out of what it was handed --
    # the ladder sitting unused at the top of the script would satisfy the line
    # above while the select still offered three roles.
    sel = re.search(r"function roleSelect\(p\) \{.*?\n\}", page, re.S).group(0)
    assert "for (const r of ROLES)" in sel, "the select is not built from the ladder"


def test_the_page_and_the_server_agree_about_the_ladder():
    """The app's own JavaScript carries the ladder too -- it has to, because the
    page decides which controls to draw before it can ask anybody. Two copies,
    one order, and this is the test the comment above each of them points at."""
    found = re.search(r"const ROLES = (\[[^\]]*\]);", notes.PAGE).group(1)
    assert json.loads(found.replace("'", '"')) == list(notes.ROLES)
