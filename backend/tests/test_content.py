from conftest import as_user, as_admin_connection, make_user


def member(db, trusted=True, admin=False, role=None):
    """One approved classmate. `role` is the truth; the two old keywords are
    kept because half this suite reads better saying trusted=False."""
    uid = make_user(db)
    role = role or ("admin" if admin else "trusted" if trusted else "student")
    db.execute(
        "insert into profiles (id, name, status, role) "
        "values (%s, 'M', 'approved', %s)", (uid, role),
    )
    return uid


def upload_as_owner(db, uploader, filename, key, status=None):
    """Put a material in on the table owner's connection, RLS bypassed.

    A student cannot insert one any more -- that is the point of the role -- so
    a pending row, which is what a student's upload used to become, now has to
    be planted rather than uploaded. The BEFORE INSERT trigger still runs here,
    so what it decides is still what these tests assert on.
    """
    as_admin_connection(db)
    cols = "subject_code, uploader_id, filename, file_key, size_bytes"
    vals = "'CY1107', %s, %s, %s, 10"
    args = [uploader, filename, key]
    if status:
        cols, vals, args = cols + ", status", vals + ", %s", args + [status]
    return db.execute(
        f"insert into materials ({cols}) values ({vals}) returning id", args
    ).fetchone()[0]


def test_trusted_upload_is_visible_immediately(db):
    me = member(db, trusted=True)
    as_user(db, me)
    db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'notes.pdf', 'k1', 100)", (me,),
    )
    assert db.execute("select status from materials").fetchone()[0] == "visible"


def test_untrusted_upload_is_forced_pending(db):
    me = member(db, trusted=False)
    # The row asks for 'visible'; the trigger overrides it, whoever inserts.
    upload_as_owner(db, me, "notes.pdf", "k2", status="visible")
    as_admin_connection(db)
    assert db.execute("select status from materials").fetchone()[0] == "pending"


def test_a_student_cannot_upload_at_all(db):
    """The role is the gate, and it is the database's, not the handler's."""
    me = member(db, role="student")
    as_user(db, me)
    try:
        db.execute(
            "insert into materials (subject_code, uploader_id, filename, file_key, "
            "size_bytes) values ('CY1107', %s, 'sneak.pdf', 'sk', 10)", (me,),
        )
    except Exception as e:
        assert "policy" in str(e).lower()
    else:
        raise AssertionError("a student inserted a material")


def test_members_cannot_see_pending_uploads_from_others(db):
    newbie = member(db, trusted=False)
    upload_as_owner(db, newbie, "x.pdf", "k3")
    as_admin_connection(db)
    other = member(db)
    as_user(db, other)
    assert db.execute("select count(*) from materials").fetchone()[0] == 0


def test_uploader_sees_their_own_pending_upload(db):
    newbie = member(db, trusted=False)
    upload_as_owner(db, newbie, "x.pdf", "k4")
    as_user(db, newbie)
    assert db.execute("select count(*) from materials").fetchone()[0] == 1


def test_cannot_upload_as_someone_else(db):
    me = member(db)
    victim = member(db)
    as_user(db, me)
    try:
        db.execute(
            "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
            "values ('CY1107', %s, 'forged.pdf', 'k5', 10)", (victim,),
        )
    except Exception as e:
        assert "policy" in str(e).lower()
    else:
        raise AssertionError("impersonation was allowed")


def test_one_vote_per_person_per_material(db):
    owner = member(db)
    voter = member(db)
    as_user(db, owner)
    mid = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'n.pdf', 'k6', 10) returning id", (owner,),
    ).fetchone()[0]
    # Somebody else's vote: your own is refused by "vote as yourself" now, and
    # this test is about the second press, not the first.
    as_user(db, voter)
    db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, voter))
    try:
        db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, voter))
    except Exception as e:
        assert "duplicate" in str(e).lower() or "unique" in str(e).lower()
    else:
        raise AssertionError("double voting was allowed")


def test_untrusted_cannot_publish_their_own_material(db):
    """The pending gate is not insert-only: a newcomer cannot flip the switch."""
    me = member(db, trusted=False)
    mid = upload_as_owner(db, me, "leak.pdf", "k7")
    as_user(db, me)
    try:
        db.execute("update materials set status = 'visible' where id = %s", (mid,))
    except Exception as e:
        assert "policy" in str(e).lower()
    else:
        raise AssertionError("untrusted member published their own material")


def test_trusted_member_can_still_edit_their_material(db):
    me = member(db, trusted=True)
    as_user(db, me)
    mid = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'n.pdf', 'k8', 10) returning id", (me,),
    ).fetchone()[0]
    db.execute("update materials set filename = 'better.pdf' where id = %s", (mid,))
    assert db.execute("select filename from materials").fetchone()[0] == "better.pdf"


def test_admin_can_publish_a_pending_material(db):
    newbie = member(db, trusted=False)
    mid = upload_as_owner(db, newbie, "x.pdf", "k9")
    as_admin_connection(db)
    boss = member(db, admin=True)
    as_user(db, boss)
    db.execute("update materials set status = 'visible' where id = %s", (mid,))
    assert db.execute("select status from materials").fetchone()[0] == "visible"


def test_uploader_can_delete_their_own_lecture(db):
    me = member(db)
    as_user(db, me)
    lid = db.execute(
        "insert into lectures (subject_code, uploader_id, audio_key) "
        "values ('CY1107', %s, 'a1') returning id", (me,),
    ).fetchone()[0]
    assert db.execute("delete from lectures where id = %s", (lid,)).rowcount == 1


def test_cannot_delete_someone_elses_lecture(db):
    owner = member(db)
    as_user(db, owner)
    lid = db.execute(
        "insert into lectures (subject_code, uploader_id, audio_key) "
        "values ('CY1107', %s, 'a2') returning id", (owner,),
    ).fetchone()[0]
    as_admin_connection(db)
    thief = member(db)
    as_user(db, thief)
    assert db.execute("delete from lectures where id = %s", (lid,)).rowcount == 0
