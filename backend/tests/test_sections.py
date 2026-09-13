"""One section cannot read another section's anything.

This is the file the sections feature is worth. Everything else about it is a
column and a foreign key; THIS is the part where being wrong does not render
badly, it hands a hundred and ten strangers somebody's confession.

So the test is deliberately not about notes.py. It opens a connection, sets
the same two GUCs notes.act_as sets on every real request -- `role` to
authenticated and request.jwt.claims to the member's id -- and then writes the
plainest SQL there is: `select count(*) from materials`. If row level security
is right, that count is zero for a member of the other section, whatever any
handler does or forgets to do. If it is wrong, no handler can save it.

test_policy_matrix.py's habit is kept: every assertion of "you see nothing"
sits behind an assertion that there was something to see. A test that reads
zero rows from an empty table passes with the whole feature deleted.
"""

import pytest
from conftest import as_admin_connection, as_user, make_user

# The four the spec names by hand, plus the two it does not: the subject room
# is a hundred and ten classmates talking, and votes name who agreed with whom.
SCOPED_READS = ["materials", "lectures", "announcements", "doubts", "posts",
                "messages", "votes", "profiles"]


def a_section(db, name, grad_year=2030):
    """A section, on the owning connection. Set B, which is Section I's, so
    that both sections in these tests are taught the same subject codes and a
    difference in what they can read is never a difference in curriculum."""
    as_admin_connection(db)
    return db.execute(
        "insert into sections (name, grad_year, subject_set_id) "
        "values (%s, %s, (select id from subject_sets where name = 'Set B')) "
        "returning id", (name, grad_year),
    ).fetchone()[0]


def member_of(db, section, role="admin"):
    """One approved person in `section`. Admin by default, because the point of
    these tests is that even the most privileged role in section X is nobody in
    section Y."""
    as_admin_connection(db)
    uid = make_user(db)
    db.execute(
        "insert into profiles (id, name, status, role, section_id) "
        "values (%s, 'M', 'approved', %s, %s)", (uid, role, section),
    )
    return uid


def fill(db, who, voter=None):
    """Everything a section has, written by `who`, under RLS, as themselves.

    Nothing here names a section. That is the second thing this file proves:
    the existing insert statements did not have to learn about sections,
    because section_id defaults to the writer's own.
    """
    as_user(db, who)
    db.execute("insert into materials (subject_code, uploader_id, filename, "
               "file_key, size_bytes) values ('CY1107', %s, 'secret.pdf', "
               "'sec-k', 10)", (who,))
    db.execute("insert into lectures (subject_code, uploader_id, title, "
               "audio_key) values ('CY1107', %s, 'our tuesday', 'sec-a')", (who,))
    db.execute("insert into announcements (author_id, title, body) "
               "values (%s, 'Lab moved', 'to 3pm')", (who,))
    db.execute("insert into doubts (subject_code, author_id, body) "
               "values ('CY1107', %s, 'why is the ratio 2:1?')", (who,))
    db.execute("insert into posts (kind, author_id, body) "
               "values ('confession', %s, 'I have never been to a 9am')", (who,))
    db.execute("insert into messages (subject_code, author_id, body) "
               "values ('CY1107', %s, 'anyone got the sheet')", (who,))
    # By somebody else: 0022's rule is that you cannot upvote your own, and
    # it is enforced whichever section you are in.
    if voter:
        as_user(db, voter)
        db.execute("insert into votes (material_id, voter_id) select id, %s "
                   "from materials limit 1", (voter,))


@pytest.mark.parametrize("table", SCOPED_READS)
def test_a_member_of_one_section_reads_nothing_of_the_others(db, table):
    x, y = a_section(db, 'X'), a_section(db, 'Y')
    theirs, classmate = member_of(db, x), member_of(db, x, role="student")
    mine = member_of(db, y)
    fill(db, theirs, voter=classmate)

    # There is something to miss. Read as the section it belongs to, through
    # the same policies -- not on the owning connection, which would prove only
    # that the insert happened.
    as_user(db, theirs)
    n = db.execute(f"select count(*) from {table}").fetchone()[0]
    assert n > 0, (
        f"setup left {table} empty for the section that owns it, so the "
        f"assertion below would pass with row level security switched off")

    as_user(db, mine)
    leaked = db.execute(f"select count(*) from {table}").fetchone()[0]
    # Their own profile row and nothing else, exactly as test_policy_matrix
    # counts it: "read own profile always" is keyed on their own id and has no
    # section in it to narrow -- it is what lets somebody pending see
    # themselves at all.
    expected = 1 if table == "profiles" else 0
    assert leaked == expected, (
        f"a member of section Y read {leaked} rows of section X's {table}")


def test_a_member_sees_their_own_section_and_their_own_classmates(db):
    """The other half: scoping that hides everything from everybody is not a
    feature, it is an outage. Section Y must still read Section Y."""
    y = a_section(db, 'Y')
    mine, classmate = member_of(db, y), member_of(db, y, role="trusted")
    fill(db, mine, voter=classmate)

    as_user(db, classmate)
    for table in ["materials", "lectures", "announcements", "doubts", "posts",
                  "messages"]:
        n = db.execute(f"select count(*) from {table}").fetchone()[0]
        assert n == 1, f"a classmate could not read their own section's {table}"
    # And the class list is the two of them, not the whole institute.
    assert db.execute("select count(*) from profiles").fetchone()[0] == 2


def test_the_section_you_are_in_is_the_only_one_you_can_name(db):
    x, y = a_section(db, 'X'), a_section(db, 'Y')
    mine = member_of(db, y)
    as_user(db, mine)
    rows = db.execute("select id from sections").fetchall()
    assert [r[0] for r in rows] == [y], "a member read a section they are not in"
    assert db.execute("select my_section()").fetchone()[0] == y


def test_a_member_cannot_walk_into_another_section(db):
    """The pinning clause on "edit own name only". Without it the boundary is
    one UPDATE wide: change your own name and your own section in the same
    statement and you are reading their library a moment later."""
    x, y = a_section(db, 'X'), a_section(db, 'Y')
    mine = member_of(db, y, role="student")
    as_user(db, mine)
    with pytest.raises(Exception) as exc:
        db.execute("update profiles set section_id = %s where id = %s", (x, mine))
    assert "policy" in str(exc.value).lower(), exc.value
    db.rollback()


def test_an_admin_of_one_section_cannot_promote_out_of_another(db):
    """approve_uploader runs as the table owner, so 0042's policies never see
    it. It has to say the section itself."""
    x, y = a_section(db, 'X'), a_section(db, 'Y')
    theirs = member_of(db, x, role="student")
    as_admin_connection(db)
    db.execute("insert into materials (subject_code, uploader_id, filename, "
               "file_key, size_bytes, status, section_id) values ('CY1107', %s, "
               "'p.pdf', 'p-k', 10, 'pending', %s)", (theirs, x))

    mine = member_of(db, y, role="admin")
    as_user(db, mine)
    assert db.execute("select approve_uploader(%s)", (theirs,)).fetchone()[0] == 0

    as_admin_connection(db)
    assert db.execute("select role from profiles where id = %s",
                      (theirs,)).fetchone()[0] == "student"
    assert db.execute("select status from materials").fetchone()[0] == "pending"


def test_a_confession_names_its_author_only_to_its_own_admin(db):
    """The single most private read in the database, and it takes a bare uuid."""
    x, y = a_section(db, 'X'), a_section(db, 'Y')
    theirs, mine = member_of(db, x), member_of(db, y)
    as_user(db, theirs)
    pid = db.execute("insert into posts (kind, author_id, body) values "
                     "('confession', %s, 'not for you') returning id",
                     (theirs,)).fetchone()[0]

    as_user(db, theirs)
    assert db.execute("select confession_author(%s)", (pid,)).fetchone()[0] == "M"
    as_user(db, mine)
    assert db.execute("select confession_author(%s)", (pid,)).fetchone()[0] is None


def test_a_new_member_lands_in_the_section_whose_code_they_typed(db):
    """The door. An invite belongs to a section and the joiner follows it."""
    y = a_section(db, 'Y')
    as_admin_connection(db)
    db.execute("insert into invites (code, expires_at, section_id) values "
               "('SECTION-Y', now() + interval '1 day', %s)", (y,))
    uid = make_user(db)
    as_user(db, uid)
    assert db.execute(
        "select join_with_invite('SECTION-Y', 'New', 'ROLL-Y', null, 'pw')"
    ).fetchone()[0] is True
    as_admin_connection(db)
    assert db.execute("select section_id from profiles where id = %s",
                      (uid,)).fetchone()[0] == y


def test_a_section_is_seeded_from_its_own_timetable_template(db):
    """Two templates now. A new member must get theirs, not the other one's."""
    x, y = a_section(db, 'X'), a_section(db, 'Y')
    as_admin_connection(db)
    db.execute("insert into section_timetable (section_id, day, period, "
               "subject_code) values (%s, 1, 1, 'CY1107'), (%s, 1, 2, 'MC1101')",
               (x, x))
    db.execute("insert into section_timetable (section_id, day, period, "
               "subject_code) values (%s, 1, 1, 'ME1109')", (y,))

    mine = member_of(db, y, role="student")
    rows = db.execute(
        "select day, period, subject_code, section_id from timetable "
        "where profile_id = %s", (mine,)).fetchall()
    assert rows == [(1, 1, 'ME1109', y)]


def test_the_subject_list_is_the_one_your_section_follows(db):
    """Subject sets are what makes the semester swap a pointer rather than a
    migration -- and they are decorative unless the list is filtered by them.

    Set A ships empty (0040 says why at length), so the way to prove the lens
    is to move one subject into it and watch it leave the other section's list.
    """
    as_admin_connection(db)
    set_a = db.execute(
        "select id from subject_sets where name = 'Set A'").fetchone()[0]
    db.execute("update subjects set set_id = %s where code = 'NC1151'", (set_a,))

    theirs = db.execute(
        "insert into sections (name, grad_year, subject_set_id) "
        "values ('A-side', 2030, %s) returning id", (set_a,)).fetchone()[0]
    a_side = member_of(db, theirs, role="student")
    b_side = member_of(db, a_section(db, 'B-side'), role="student")

    as_user(db, a_side)
    assert [r[0] for r in db.execute("select code from subjects").fetchall()] \
        == ['NC1151']

    as_user(db, b_side)
    codes = [r[0] for r in db.execute("select code from subjects").fetchall()]
    assert len(codes) == 11 and 'NC1151' not in codes and 'CY1107' in codes
