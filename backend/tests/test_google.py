"""The second door: a Google account, and the section list the registrar wrote.

0049's bet is that a section is better read off the institute's own list than
off an invite code somebody forwarded. What has to be true for that to be safe
is here: the address has to be a student's, the scholar number has to be on the
list, and the section has to be the list's answer rather than anything the
request said.

The OAuth round trip itself is not tested. What Google returns is Google's, and
every line this file could exercise around it would be a test of a stub. What
is tested is everything that happens after the token is in hand -- which is
where the section, the profile and the approval are decided.
"""

import pathlib
import sys

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import as_admin_connection, as_user, make_user  # noqa: E402


def a_section(db, name, grad_year=2030):
    return db.execute(
        "insert into sections (name, grad_year, subject_set_id) "
        "values (%s, %s, (select id from subject_sets where name = 'Set B')) "
        "returning id", (name, grad_year),
    ).fetchone()[0]


def listed(db, scholar, roll, section, name="A Student"):
    db.execute("insert into roll_list (scholar_no, roll_no, name, section_id) "
               "values (%s, %s, %s, %s)", (scholar, roll, name, section))
    return scholar


# ---------------------------------------------------------------- the address

@pytest.mark.parametrize("email,want", [
    ("26112011201@stu.manit.ac.in", "26112011201"),
    ("  26112011201@STU.MANIT.AC.IN  ", "26112011201"),
    ("26112011201@gmail.com", None),          # right person, wrong mailbox
    ("anirudh.sahu@stu.manit.ac.in", None),   # right domain, not a scholar no
    ("2611201120@stu.manit.ac.in", None),     # ten digits
    ("261120112012@stu.manit.ac.in", None),   # twelve
    ("", None),
    (None, None),
])
def test_only_an_institute_scholar_address_carries_a_scholar_number(email, want):
    assert notes.scholar_from_email(email, "stu.manit.ac.in") == want


def test_the_claims_of_a_token_survive_missing_padding():
    # Google strips base64 padding. A decoder that does not put it back throws
    # on some tokens and not others, which is the worst kind of broken.
    import base64
    import json

    payload = {"email": "26112011201@stu.manit.ac.in", "hd": "stu.manit.ac.in"}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    assert notes.google_claims("header." + raw.decode() + ".sig") == payload
    assert notes.google_claims("") == {}


# ------------------------------------------------------------------- the door

def test_the_list_decides_the_section_not_the_request(db):
    a, b = a_section(db, 'GA'), a_section(db, 'GB')
    listed(db, "26112011201", "26A001", a)
    listed(db, "26112011202", "26B001", b)

    for scholar, want in (("26112011201", a), ("26112011202", b)):
        uid = make_user(db)
        as_user(db, uid)
        assert db.execute("select join_with_google(%s, %s)",
                          (scholar, f"{scholar}@stu.manit.ac.in")).fetchone()[0]
        as_admin_connection(db)
        got = db.execute("select section_id, roll_no, status from profiles "
                         "where id = %s", (uid,)).fetchone()
        assert got[0] == want, "the section came from somewhere other than the list"
        assert got[2] == "approved", "a student on the registrar's list still waits"


def test_a_scholar_number_nobody_published_opens_nothing(db):
    uid = make_user(db)
    as_user(db, uid)
    assert db.execute("select join_with_google(%s, %s)",
                      ("26999011201", "26999011201@stu.manit.ac.in")
                      ).fetchone()[0] is False
    as_admin_connection(db)
    assert db.execute("select count(*) from profiles where id = %s",
                      (uid,)).fetchone()[0] == 0


def test_a_google_account_has_no_password_anyone_could_guess(db):
    # Null is the state 0013 calls claimable: whoever types the roll number
    # first becomes that person. A Google profile must never be left in it.
    section = a_section(db, 'GC')
    listed(db, "26112011201", "26A001", section)
    uid = make_user(db)
    as_user(db, uid)
    db.execute("select join_with_google(%s, %s)",
               ("26112011201", "26112011201@stu.manit.ac.in"))
    as_admin_connection(db)
    stored, roll_login = db.execute(
        "select password, roll_login from profiles where id = %s", (uid,)).fetchone()
    assert stored and len(stored) >= 32 and not roll_login


def test_the_class_list_is_not_readable_by_the_class(db):
    section = a_section(db, 'GD')
    listed(db, "26112011201", "26A001", section)
    uid = make_user(db)
    db.execute("insert into profiles (id, name, status, section_id) "
               "values (%s, 'M', 'approved', %s)", (uid, section))
    as_user(db, uid)
    # 1054 names with their sections attached is the whole first year's list.
    # There is a row in it and the member is granted the table, so this is the
    # missing policy refusing every one of them and nothing else.
    assert db.execute("select count(*) from roll_list").fetchone()[0] == 0


# ------------------------------------------------------- the three ways in

def test_the_second_sign_in_is_the_same_person(db):
    section = a_section(db, 'GE')
    listed(db, "26112011201", "26A001", section)
    email = "26112011201@stu.manit.ac.in"
    as_admin_connection(db)
    first = notes.db_google(db, "26112011201", email)
    again = notes.db_google(db, "26112011201", email)
    assert first[0] == again[0]
    assert db.execute("select count(*) from profiles").fetchone()[0] == 1


def test_somebody_who_joined_by_invite_keeps_the_profile_they_have(db):
    # The invite door ran for a year before this one existed. A member signing
    # in with the address the institute gave them must land in the account that
    # holds their uploads, not be refused for owning it.
    section = a_section(db, 'GF')
    listed(db, "26112011201", "26A001", section)
    uid = make_user(db)
    db.execute("insert into profiles (id, name, roll_no, status, role, section_id) "
               "values (%s, 'Old Hand', '26A001', 'approved', 'trusted', %s)",
               (uid, section))
    got = notes.db_google(db, "26112011201", "26112011201@stu.manit.ac.in")
    assert got[0] == str(uid)
    assert db.execute("select count(*) from profiles").fetchone()[0] == 1
    assert db.execute("select role from profiles where id = %s",
                      (uid,)).fetchone()[0] == "trusted", "adoption reset the role"


def test_a_seat_already_taken_under_another_address_is_not_handed_over(db):
    section = a_section(db, 'GG')
    listed(db, "26112011201", "26A001", section)
    uid = make_user(db)
    db.execute("insert into profiles (id, name, roll_no, status, section_id, email) "
               "values (%s, 'First', '26A001', 'approved', %s, %s)",
               (uid, section, "someone.else@stu.manit.ac.in"))
    assert notes.db_google(db, "26112011201",
                           "26112011201@stu.manit.ac.in") is None
