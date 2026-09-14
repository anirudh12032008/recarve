"""Importing a week: the CSV an AI reads off a photograph of the printed grid.

The owner photographs the institute timetable, hands the photo to a chat, and
pastes back `day,period,subject_code`. Everything that can go wrong with that
goes wrong in the paste, so parse_timetable is a pure function and this is
where it is held to its bargain: forgiving about what a paste mangles, and
unforgiving about anything that would change which room a hundred and ten
people walk into.

Two halves. The parser first, a table of inputs against `(rows, errors)`, no
database in sight -- that is the whole point of it being pure. Then the CLI,
against the real database, because "all or nothing" is worth exactly what the
transaction enforces.
"""

import pathlib
import sys

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

from conftest import DB_URL, make_user  # noqa: E402

KNOWN = ["MC1101", "CY1107", "EE1125"]

# The same two periods, however they arrive.
MONDAY = [(1, 1, "MC1101"), (1, 2, "CY1107")]


# ------------------------------------------------------------- the parser


@pytest.mark.parametrize("text", [
    "day,period,subject_code\nMonday,1,MC1101\nMonday,2,CY1107\n",   # with a header
    "Monday,1,MC1101\nMonday,2,CY1107\n",                            # without one
    "Monday,1,MC1101\r\nMonday,2,CY1107\r\n",                        # CRLF, off Windows
    "\n\nMonday,1,MC1101\n\n\nMonday,2,CY1107\n\n",                  # blank lines
    "  Monday , 1 , MC1101  \nMonday,2,\tCY1107",                    # whitespace, no last newline
    "Monday,1,MC1101,\nMonday,2,CY1107,,\n,,\n",                     # trailing commas, Excel's tail
    '"Monday",1,"MC1101"\n"Monday","2","CY1107"\n',                  # quoted cells
    "“Monday”,1,“MC1101”\n"                      # the quotes Word substitutes
    "‘Monday’,2,‘CY1107’\n",
    "﻿day,period,subject_code\nmonday,1,mc1101\nMONDAY,2,cy1107\n",  # a BOM, and any case
])
def test_the_parser_forgives_what_a_paste_mangles(text):
    """None of these change what the timetable says, so none of them may be an
    error: a rejected paste sends the owner back to the photograph."""
    assert notes.parse_timetable(text, KNOWN) == (MONDAY, [])


@pytest.mark.parametrize("text,bad", [
    ("Monday,1,ZZ9999\n", "unknown subject code 'ZZ9999'"),
    ("Monday,1,BS1111\n", "unknown subject code 'BS1111'"),   # real subject, other set
    ("Monday,0,MC1101\n", "period 0 is not 1 to 8"),
    ("Monday,9,MC1101\n", "period 9 is not 1 to 8"),
    ("Monday,one,MC1101\n", "period 'one' is not a number"),
    ("Funday,1,MC1101\n", "'Funday' is not a day from Monday to Saturday"),
    ("Sunday,1,MC1101\n", "'Sunday' is not a day from Monday to Saturday"),
    ("Mon,1,MC1101\n", "'Mon' is not a day from Monday to Saturday"),
    ("Monday,1\n", "expected day,period,subject_code -- got 'Monday,1'"),
    ("Monday,1,MC1101,CY1107\n",
     "expected day,period,subject_code -- got 'Monday,1,MC1101,CY1107'"),
])
def test_the_parser_refuses_anything_that_changes_the_meaning(text, bad):
    """Every one of these names the line it is about -- a report that says only
    "bad CSV" leaves forty rows to re-read by eye."""
    rows, errors = notes.parse_timetable(text, KNOWN)
    assert rows == [], "a line it could not read must not become a period"
    assert errors == [f"line 1: {bad}"], errors


def test_the_same_period_filled_twice_is_a_conflict_not_a_last_write():
    """Two subjects in one slot means the photograph was misread. Guessing
    which is right would put half the class in the wrong room in silence."""
    rows, errors = notes.parse_timetable(
        "Monday,1,MC1101\nMonday,1,CY1107\n", KNOWN)
    assert errors == ["line 2: Monday period 1 is already MC1101, set on line 1"]
    assert rows == [(1, 1, "MC1101")], "the conflict is reported, not resolved"


def test_every_bad_line_is_reported_at_once():
    """Forty pasted rows should teach the owner all their mistakes in one go,
    not one per attempt."""
    rows, errors = notes.parse_timetable(
        "day,period,subject_code\n"
        "Monday,1,MC1101\n"         # 2: fine
        "Monday,1,CY1107\n"         # 3: conflict
        "Funday,2,MC1101\n"         # 4: no such day
        "Tuesday,9,MC1101\n"        # 5: no such period
        "Tuesday,1,ZZ9999\n"        # 6: no such subject
        "Wednesday,3\n"             # 7: short
        "Thursday,4,EE1125\n",      # 8: fine
        KNOWN)
    assert [e.split(":")[0] for e in errors] == [
        "line 3", "line 4", "line 5", "line 6", "line 7"], errors
    assert rows == [(1, 1, "MC1101"), (4, 4, "EE1125")], \
        "the good rows still come back, so a preview can show them"


@pytest.mark.parametrize("text", ["", "   \n\n", "day,period,subject_code\n", ",,\n"])
def test_a_paste_with_no_rows_in_it_is_an_error(text):
    """Otherwise an empty textarea reads as "clear the week", and the CLI below
    would faithfully delete a hundred and ten people's Monday."""
    assert notes.parse_timetable(text, KNOWN) == ([], ["no timetable rows found"])


def test_the_known_codes_are_the_callers_to_choose():
    """The seam sections will use: the parser never learns what a section is,
    the caller narrows the list to that section's subject set."""
    assert notes.parse_timetable("Monday,1,BS1111\n", ["BS1111"]) == \
        ([(1, 1, "BS1111")], [])


def test_the_parser_touches_nothing_outside_itself():
    """Pure, so the tests above need no database and the web layer can call it
    on a paste nobody has agreed to save yet."""
    assert notes.parse_timetable("Monday,1,MC1101", KNOWN)[0] == [(1, 1, "MC1101")]
    assert "conn" not in notes.parse_timetable.__code__.co_varnames


# ---------------------------------------------------------------- the CLI


@pytest.fixture
def template():
    """The section template, emptied before and after.

    notes.py's CLI opens its own autocommit connection -- that is what the
    owner runs -- so this cannot ride on the rolling-back `db` fixture.
    """
    def clear():
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("delete from section_timetable")

    def written():
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            return sorted(conn.execute(
                "select day, period, subject_code from section_timetable"))

    clear()
    yield written
    clear()


def run(tmp_path, text, **kw):
    csv_path = tmp_path / "week.csv"
    csv_path.write_text(text)
    # Sections arrived in 0040-0044, so a database under test always has them
    # and the command always wants to be told which week it is replacing.
    # Section I is the one 0040 backfills the existing rows into.
    fields = {"csv": csv_path, "section": "I", "grad_year": None, "dry_run": False}
    return notes.import_timetable(notes.argparse.Namespace(**{**fields, **kw}))


def test_the_cli_writes_the_week_and_then_replaces_it(template, tmp_path, capsys):
    run(tmp_path, "day,period,subject_code\nMonday,1,MC1101\nMonday,2,CY1107\n")
    assert template() == [(1, 1, "MC1101"), (1, 2, "CY1107")]

    # Replaced whole, never merged: clearing a period is the same
    # operation as setting one, and there is no half-applied grid to reason about.
    run(tmp_path, "Tuesday,1,EE1125\n")
    assert template() == [(2, 1, "EE1125")]

    out = capsys.readouterr().out
    assert "Monday" in out and "Tuesday" in out, "it has to say what it changed"


def test_the_cli_puts_the_week_it_writes_on_the_section(template, tmp_path, capsys):
    """The other way a template is written, and it has to reach people for the
    same reason /super does: nobody can edit their own week any more, so a
    template nobody was seeded from is a correction nobody ever sees."""
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("delete from timetable")
        uid = make_user(conn)
        conn.execute(
            "insert into profiles (id, name, roll_no, status, section_id) values"
            " (%s, 'Imported', '24T001', 'approved',"
            "  (select id from sections where name = 'I'))", (uid,))
    try:
        run(tmp_path, "day,period,subject_code\nMonday,1,MC1101\nMonday,2,CY1107\n")
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            assert sorted(conn.execute(
                "select day, period, subject_code from timetable where profile_id = %s",
                (uid,))) == [(1, 1, "MC1101"), (1, 2, "CY1107")]
        assert "onto 1 week" in capsys.readouterr().out, \
            "and it has to say how many people it just rewrote"
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("delete from profiles where id = %s", (uid,))
            conn.execute("delete from auth.users where id = %s", (uid,))


def test_one_bad_line_writes_nothing_at_all(template, tmp_path, capsys):
    run(tmp_path, "Monday,1,MC1101\n")
    with pytest.raises(SystemExit):
        run(tmp_path, "Tuesday,1,EE1125\nTuesday,2,ZZ9999\n")
    assert template() == [(1, 1, "MC1101")], "a refused import must not touch the week"
    assert "ZZ9999" in capsys.readouterr().err, "and it must say which line was wrong"


def test_a_dry_run_says_what_would_change_and_changes_nothing(template, tmp_path, capsys):
    run(tmp_path, "Monday,1,MC1101\n")
    run(tmp_path, "Tuesday,1,EE1125\n", dry_run=True)
    assert template() == [(1, 1, "MC1101")]
    out = capsys.readouterr().out
    assert "Tuesday" in out and "nothing was written" in out


def test_a_database_with_sections_insists_on_being_told_which(template, tmp_path):
    """Sections landed in 0040-0044, so section_timetable is one week per
    section now. Writing without naming one would replace every section's
    Monday at once -- exactly the silent damage the parser refuses to do, so
    the command refuses too rather than guessing."""
    with pytest.raises(SystemExit):
        run(tmp_path, "Monday,1,MC1101\n", section=None)
    assert template() == [], "a refused import must not touch anybody's week"


def test_a_section_that_is_not_there_is_refused(template, tmp_path):
    """A typo in --section must not quietly match nothing and write nothing
    while saying it worked."""
    with pytest.raises(SystemExit):
        run(tmp_path, "Monday,1,MC1101\n", section="Nope")
    assert template() == []
