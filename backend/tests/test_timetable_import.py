"""Importing a week: the CSV an AI reads off a photograph of the printed grid.

The owner photographs the institute timetable, hands the photo to a chat, and
pastes back `day,period,subject_code`. Everything that can go wrong with that
goes wrong in the paste, so parse_timetable is a pure function and this is
where it is held to its bargain: forgiving about what a paste mangles, and
unforgiving about anything that would change which room a hundred and ten
people walk into.

A table of inputs against `(rows, errors)`, with no database in sight -- that
is the whole point of the parser being pure.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

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
