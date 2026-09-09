from conftest import as_user, as_admin_connection, make_user


def member(db, trusted=True, admin=False):
    uid = make_user(db)
    db.execute(
        "insert into profiles (id, name, status, trusted, is_admin) "
        "values (%s, 'M', 'approved', %s, %s)", (uid, trusted, admin),
    )
    return uid


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
    as_user(db, me)
    # The client asks for 'visible'; the trigger overrides it.
    db.execute(
        "insert into materials "
        "(subject_code, uploader_id, filename, file_key, size_bytes, status) "
        "values ('CY1107', %s, 'notes.pdf', 'k2', 100, 'visible')", (me,),
    )
    as_admin_connection(db)
    assert db.execute("select status from materials").fetchone()[0] == "pending"


def test_members_cannot_see_pending_uploads_from_others(db):
    newbie = member(db, trusted=False)
    as_user(db, newbie)
    db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'x.pdf', 'k3', 10)", (newbie,),
    )
    as_admin_connection(db)
    other = member(db)
    as_user(db, other)
    assert db.execute("select count(*) from materials").fetchone()[0] == 0


def test_uploader_sees_their_own_pending_upload(db):
    newbie = member(db, trusted=False)
    as_user(db, newbie)
    db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'x.pdf', 'k4', 10)", (newbie,),
    )
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
    as_user(db, owner)
    mid = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'n.pdf', 'k6', 10) returning id", (owner,),
    ).fetchone()[0]
    db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, owner))
    try:
        db.execute("insert into votes (material_id, voter_id) values (%s, %s)", (mid, owner))
    except Exception as e:
        assert "duplicate" in str(e).lower() or "unique" in str(e).lower()
    else:
        raise AssertionError("double voting was allowed")


def test_untrusted_cannot_publish_their_own_material(db):
    """The pending gate is not insert-only: a newcomer cannot flip the switch."""
    me = member(db, trusted=False)
    as_user(db, me)
    mid = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'leak.pdf', 'k7', 10) returning id", (me,),
    ).fetchone()[0]
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
    as_user(db, newbie)
    mid = db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, 'x.pdf', 'k9', 10) returning id", (newbie,),
    ).fetchone()[0]
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
