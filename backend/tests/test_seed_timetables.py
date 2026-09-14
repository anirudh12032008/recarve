"""The ten grids in backend/dev/seed_timetables.sql say what the institute says.

This file reads SQL text rather than a database, the way test_table_grants.py
does, because the seed is never applied to the test database -- it is live data,
and the only place it can be checked before it reaches a hundred and ten phones
is here.

What it checks is the one thing a typo cannot survive: the Scheme page gives
every subject a fixed number of periods a week, and a grid that has been
mistyped almost always has one too many of something and one too few of
something else. Nine of the ten sections match their scheme exactly; Section D
is timetabled a fourth period of Engineering Mechanics against the Scheme's
three, which is the institute's own arithmetic and is named below rather than
rounded away.

Labs count double where the PDF gives each batch its own slot, because both
slots are in the template on purpose -- seed_timetables.sql says why at the top.
"""

import collections
import re
from pathlib import Path

import pytest

import notes

SEED = Path(__file__).resolve().parents[2] / "backend" / "dev" / "seed_timetables.sql"
ROW = re.compile(r"\('([A-J])',(\d),(\d),'(\w+)'\)")

# code -> periods a week, from the Scheme page of the institute timetable.
MT = {"MC1101": 4, "PY1102": 3, "CE1103": 3, "ME1104": 1, "CS1105": 2,
      "HS1106": 1, "CE1121": 2, "PY1122": 2, "ME1123": 2, "CS1124": 2,
      "HS1128": 4, "SA1141": 2, "SA1142": 2}
ST = {"MC1101": 4, "CY1107": 3, "EE1108": 3, "ME1109": 1, "CY1110": 2,
      "BS1111": 2, "HS1112": 2, "EE1125": 4, "CY1126": 4, "ME1127": 2,
      "SA1143": 2}
# Where the printed timetable and the printed scheme disagree with each other.
PRINTED = {("D", "CE1103"): 4}


def grids():
    out = collections.defaultdict(collections.Counter)
    for name, day, period, code in ROW.findall(SEED.read_text()):
        out[name][code] += 1
    return out


def test_the_seed_was_read_at_all():
    """Without this the two tests below pass on an empty file."""
    assert len(grids()) == 10, "ten sections, A to J"


@pytest.mark.parametrize("section", list("ABCDEFGHIJ"))
def test_a_section_gets_the_periods_its_scheme_says(section):
    want = MT if section in "ABCDE" else ST
    got = grids()[section]
    assert set(got) == set(want), "a subject its group is not taught, or one missing"
    for code, periods in want.items():
        assert got[code] == PRINTED.get((section, code), periods), (
            f"Section {section} has {got[code]} periods of {code} a week and the "
            f"scheme says {periods}")


def test_every_code_in_the_seed_has_a_name():
    """A code notes.py has never heard of has no folder to file a recording in
    and nothing to render on the phone but itself."""
    unknown = {c for g in grids().values() for c in g} - set(notes.SUBJECTS)
    assert not unknown, unknown
