"""The shared archive: the seniors' 581 documents, filed as columns.

Two things are worth proving here and they pull in opposite directions.

The first is that the constraints refuse the rows that 31 hand-made folders
were happy to hold -- slides with an exam on them, a mini test set for section
"Q", a paper from the year 1025, the same document shelved twice because it sat
in two folders that meant the same thing. Every one of those is a row the old
site could and did contain, and a column is only better than a folder if the
database says no.

The second is that every approved member reads all of it. 0042 scopes
`materials` to one section and is right to, which is the whole reason this
table exists beside it rather than inside it. A suite that only ever reads as
the person who wrote the row would pass just as happily against the broken
design.
"""

import pathlib
import sys

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from conftest import as_admin_connection, as_user, make_user  # noqa: E402
from test_content import member  # noqa: E402

PAPER = dict(
    subject_code="MC1101",
    kind="paper",
    exam="mini",
    year=2025,
    set_for_section="B",
    title="Mini Test 2025-26 Sec B",
    file_key="MC1101/papers/2025-mini-B.pdf",
    size_bytes=383104,
    source="nitbfreshers portal",
)


def shelve(db, **kw):
    """Put one document on the shelf as whoever the transaction currently is."""
    row = {**PAPER, **kw}
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    return db.execute(
        f"insert into archive_documents ({cols}) values ({marks})",
        tuple(row.values()),
    )


# ---------------------------------------------------------------------------
# The rows 31 folders allowed and four columns do not


def test_slides_cannot_carry_an_exam(db):
    """'Detailed Slides' and 'End Term Papers' were sibling folders. Nothing
    stopped a file being dragged into the wrong one; here the row is refused."""
    with pytest.raises(psycopg.errors.CheckViolation):
        shelve(db, kind="slides", exam="end", set_for_section=None)


def test_a_paper_may_have_no_exam_but_notes_may_not(db):
    """An unlabelled paper is ordinary -- plenty arrived as "2023 Sem 1" with
    no word for which sitting. Notes with an exam are not ordinary."""
    shelve(db, exam=None)
    with pytest.raises(psycopg.errors.CheckViolation):
        shelve(db, kind="notes", exam="mid", file_key="MC1101/notes/x.pdf")


def test_an_exam_nobody_sits_is_refused(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        shelve(db, exam="viva")


def test_a_section_the_institute_does_not_have(db):
    """Ten sections, A through J. 0047 is why I is one of them and Q is not."""
    shelve(db, set_for_section="I", file_key="MC1101/papers/2025-mini-I.pdf")
    with pytest.raises(psycopg.errors.CheckViolation):
        shelve(db, set_for_section="Q")


@pytest.mark.parametrize("wrong", [1025, 20255, 99])
def test_a_year_that_is_a_typo_rather_than_a_year(db, wrong):
    with pytest.raises(psycopg.errors.CheckViolation):
        shelve(db, year=wrong)


def test_the_same_paper_in_two_folders_shelves_once(db):
    """The overlap is real: an End Term sits in "2024-2025 PYQs" AND in "End
    Term Previous Year Questions". The importer walks both. Only one row.

    This is what makes the import re-runnable rather than merely careful -- a
    second pass over the same tree is a no-op, not 581 duplicates.
    """
    shelve(db)
    with pytest.raises(psycopg.errors.UniqueViolation):
        shelve(db, title="End Term 2024-25 (again)")


def test_the_same_filename_under_a_different_subject_is_a_different_paper(db):
    """Uniqueness is per subject, not global. Every subject has a file called
    something like "End Term 2023.pdf" and they are not each other."""
    shelve(db)
    shelve(db, subject_code="CY1107")


def test_a_book_is_a_link_with_no_bytes(db):
    """All 30 of the seniors' "Books" entries are .url bookmarks -- they host
    no textbooks. A book row carries an address where a file_key goes and has
    no size, and that is a legal row rather than a broken one."""
    shelve(
        db,
        kind="book",
        exam=None,
        set_for_section=None,
        year=None,
        size_bytes=None,
        title="BS Grewal Mathematics",
        file_key="https://example.org/bs-grewal",
    )


def test_a_subject_the_institute_does_not_teach(db):
    """subject_code is a foreign key, so the archive cannot invent a course.
    This is the constraint that parks Mathematics 2 until it has a real code
    rather than letting the importer make one up."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        shelve(db, subject_code="MC1201")


# ---------------------------------------------------------------------------
# Who reads it, and who writes it


def test_an_approved_member_reads_what_an_admin_shelved(db):
    """The point of the table: the reader is not the writer, and nothing in
    either row says which section they are in, because this table has no
    section to be in. If a section clause ever grows here, this fails."""
    as_admin_connection(db)
    admin = member(db, admin=True)
    reader = member(db)

    as_user(db, admin)
    shelve(db)

    as_user(db, reader)
    got = db.execute(
        "select title from archive_documents where subject_code = 'MC1101'"
    ).fetchall()
    assert [r[0] for r in got] == ["Mini Test 2025-26 Sec B"]


@pytest.mark.parametrize("role", ["student", "trusted"])
def test_nobody_below_an_admin_shelves_a_document(db, role):
    """`materials` lets a trusted member upload. This table does not: the
    archive is a curated import, and a trusted classmate adding to it freehand
    is how 31 folders happened in the first place."""
    as_admin_connection(db)
    who = member(db, role=role)
    as_user(db, who)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        shelve(db)


def test_a_student_cannot_take_a_paper_off_the_shelf(db):
    as_admin_connection(db)
    admin = member(db, admin=True)
    student = member(db, role="student")

    as_user(db, admin)
    shelve(db)

    as_user(db, student)
    db.execute("delete from archive_documents")

    as_admin_connection(db)
    left = db.execute("select count(*) from archive_documents").fetchone()
    assert left[0] == 1


def test_an_unapproved_member_reads_nothing(db):
    """Approval is the door to the archive as it is to everything else."""
    as_admin_connection(db)
    admin = member(db, admin=True)
    as_user(db, admin)
    shelve(db)

    as_admin_connection(db)
    pending = make_user(db)
    db.execute(
        "insert into profiles (id, name, status, role) "
        "values (%s, 'P', 'pending', 'student')", (pending,)
    )
    as_user(db, pending)
    got = db.execute("select count(*) from archive_documents").fetchone()
    assert got[0] == 0


# ---------------------------------------------------------------------------
# The query the table was built for


def test_every_end_term_for_a_subject_is_one_query(db):
    """Four folders on the old site. One where clause here -- that sentence is
    the entire justification for the migration, so it gets a test."""
    as_admin_connection(db)
    admin = member(db, admin=True)
    as_user(db, admin)

    shelve(db, exam="end", year=2023, set_for_section=None,
           title="End Term 2023", file_key="MC1101/papers/2023-end.pdf")
    shelve(db, exam="end", year=2025, set_for_section=None,
           title="End Term 2025-26", file_key="MC1101/papers/2025-end.pdf")
    shelve(db, exam="mid", year=2025, set_for_section=None,
           title="Mid Term 2025-26", file_key="MC1101/papers/2025-mid.pdf")
    shelve(db, kind="notes", exam=None, set_for_section=None,
           title="Vector Calculus", file_key="MC1101/notes/vector.pdf")

    got = db.execute(
        "select title from archive_documents "
        "where subject_code = 'MC1101' and kind = 'paper' and exam = 'end' "
        "order by year desc"
    ).fetchall()
    assert [r[0] for r in got] == ["End Term 2025-26", "End Term 2023"]
