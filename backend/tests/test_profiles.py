from conftest import as_user, as_admin_connection, make_user


def test_twelve_subjects_are_seeded(db):
    assert db.execute("select count(*) from subjects").fetchone()[0] == 12


def test_subject_codes_match_notes_py(db):
    codes = {r[0] for r in db.execute("select code from subjects").fetchall()}
    assert codes == {
        "MC1101", "CY1107", "EE1108", "ME1109", "CY1110", "BS1111",
        "HS1112", "EE1125", "CY1126", "ME1127", "SA1143", "NC1151",
    }


def test_profile_defaults_to_pending_and_untrusted(db):
    uid = make_user(db)
    db.execute("insert into profiles (id, name) values (%s, 'Test Student')", (uid,))
    row = db.execute(
        "select status, trusted, is_admin from profiles where id = %s", (uid,)
    ).fetchone()
    assert row == ("pending", False, False)


def test_status_is_constrained(db):
    uid = make_user(db)
    try:
        db.execute(
            "insert into profiles (id, name, status) values (%s, 'X', 'superuser')", (uid,)
        )
    except Exception as e:
        assert "check" in str(e).lower()
    else:
        raise AssertionError("an invalid status was accepted")
