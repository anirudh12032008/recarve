from conftest import as_user, as_admin_connection
from test_content import member


def upload(db, uploader, key):
    return db.execute(
        "insert into materials (subject_code, uploader_id, filename, file_key, size_bytes) "
        "values ('CY1107', %s, %s, %s, 10) returning id", (uploader, f"{key}.pdf", key),
    ).fetchone()[0]


def test_approving_publishes_every_pending_upload(db):
    newbie = member(db, trusted=False)
    as_user(db, newbie)
    for i in range(3):
        upload(db, newbie, f"key{i}")
    # A fourth upload an admin has already taken down. Approval must not
    # resurrect it -- moderation outranks trust.
    removed = upload(db, newbie, "spam")

    as_admin_connection(db)
    db.execute("update materials set status = 'removed' where id = %s", (removed,))
    # Somebody else waiting in the same queue. Approving `newbie` must not
    # empty their backlog too.
    bystander = member(db, trusted=False)
    as_user(db, bystander)
    upload(db, bystander, "theirs")

    as_admin_connection(db)
    admin = member(db, admin=True)
    as_user(db, admin)
    assert db.execute("select approve_uploader(%s)", (newbie,)).fetchone()[0] == 3

    as_admin_connection(db)
    assert db.execute(
        "select count(*) from materials where uploader_id = %s and status = 'visible'",
        (newbie,),
    ).fetchone()[0] == 3
    assert db.execute(
        "select status from materials where id = %s", (removed,)
    ).fetchone()[0] == "removed"
    assert db.execute(
        "select status from materials where uploader_id = %s", (bystander,)
    ).fetchone()[0] == "pending"
    assert db.execute(
        "select trusted from profiles where id = %s", (newbie,)
    ).fetchone()[0] is True
    assert db.execute(
        "select trusted from profiles where id = %s", (bystander,)
    ).fetchone()[0] is False


def test_non_admin_cannot_approve(db):
    newbie = member(db, trusted=False)
    plain = member(db)
    as_user(db, plain)
    try:
        db.execute("select approve_uploader(%s)", (newbie,))
    except Exception as e:
        assert "admin" in str(e).lower() or "permission" in str(e).lower()
    else:
        raise AssertionError("a non-admin approved an uploader")
