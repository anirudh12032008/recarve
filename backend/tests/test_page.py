"""The page: the data it is built from, and the script inside it.

PAGE is a Python string, so pytest never parses it. A JavaScript syntax error
blanks the whole UI and still leaves a green suite, and a router that stops
opening notes looks exactly like one that works. node is the only thing on this
machine that can read that string, so these tests hand it over: once to parse
it, once to run the router's decision layer against a fake DATA.

Ceiling, named rather than designed around: openNote/render/busy only write to
the DOM, and the stub below swallows those writes. What is covered here is the
part that decides -- which level a hash means, which note it opens, what each
subject counts as. Anything visual still needs a browser.
"""

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

NODE = shutil.which("node")
SCRIPT = re.search(r"\n<script>\n(.*)\n</script>", notes.PAGE, re.S).group(1)


def make_args(tmp_path):
    return notes.argparse.Namespace(
        library=tmp_path / "library", out=tmp_path / "site" / "index.html",
        host="127.0.0.1", port=0, notes_model="claude-haiku-4-5", max_cost=1.0,
        max_explains=0, no_auth=True, verbose=False,
    )


def baked_data(page):
    return json.loads(re.search(r"^const DATA = (.*);$", page, re.M).group(1))


# ------------------------------------------------------ what gets built


def test_an_empty_library_exports_the_twelve_subjects(tmp_path):
    """Nothing to show is not an error: this page is where the first thing goes.

    It also has to name all twelve. You cannot file a chemistry recording under
    a subject the app never told you was there.
    """
    args = make_args(tmp_path)
    args.library.mkdir()
    notes.export(args)                      # must not raise SystemExit

    data = baked_data(args.out.read_text())
    assert [s["code"] for s in data] == list(notes.SUBJECTS)
    assert all(s["notes"] == [] and s["uploads"] == [] for s in data)


def test_notes_carry_the_kind_that_groups_them(tmp_path):
    """Level 2 splits Lectures from the Revision sheet on `kind`, not on title."""
    folder = tmp_path / "library" / "MC1101-Mathematics-1"
    (folder / "lectures").mkdir(parents=True)
    (folder / "lectures" / "week1.md").write_text("## Summary\nlimits\n")
    (folder / "revision.md").write_text("## Revision\nall of it\n")

    data = notes.build_data(tmp_path / "library", tmp_path)
    mc = next(s for s in data if s["code"] == "MC1101")
    assert {n["title"]: n["kind"] for n in mc["notes"]} == {
        "week1": "lecture", "Revision sheet": "revision"}


def test_serving_makes_the_library_before_it_exports_it(tmp_path):
    """A fresh clone has no library folder, and `serve` used to die on that."""
    args = make_args(tmp_path)
    assert not args.library.exists()
    was = notes.LOG_PATH
    try:
        srv = notes.build_server(args)     # mkdir must come before export()
        srv.server_close()
    finally:
        notes.LOG_PATH = was
    assert args.out.is_file()


# ------------------------------------------------------------ the script

DATA_FIXTURE = [
    {"code": "MC1101", "name": "Mathematics 1", "uploads": [],
     "notes": [{"title": "week1", "kind": "lecture", "md": "# limits"},
               {"title": "Revision sheet", "kind": "revision", "md": "# all"}]},
    {"code": "CY1107", "name": "Chemistry", "notes": [], "uploads": []},
]

# Enough of a browser to let the script load and the router run. Every DOM
# write lands in `any` and is thrown away; hash, history and DATA are real,
# which is all the deciding code reads.
STUB = """
const assert = require('node:assert');
let writes = [];   // every DOM write and call the script makes, in order
const any = new Proxy(function () {}, {
  get: (t, k) => (k === 'value' ? '' : k === Symbol.toPrimitive ? () => '' : any),
  set: (t, k, v) => (writes.push([k, v]), true),
  apply: (t, self, a) => (writes.push(['()'].concat(a)), any),
  construct: () => any,
});
const wrote = pair => writes.some(w => JSON.stringify(w) === JSON.stringify(pair));
const document = any;
const location = {hash: ''};
const history = {
  pushState: (a, b, h) => { location.hash = h; },
  replaceState: (a, b, h) => { location.hash = h; },
  back: () => {},
};
const window = {scrollTo: () => {}, print: () => {}};
const marked = {parse: md => md};
const fetch = () => new Promise(() => {});
const setInterval = () => 0, setTimeout = () => 0, clearInterval = () => {};
"""

CHECKS = """
// LEVEL 3: a two-segment hash opens that note.
location.hash = '#MC1101/week1';
route();
assert.equal(current && current.title, 'week1', 'a note hash must open the note');
assert.equal(view.code, 'MC1101');

// LEVEL 2: one segment is the subject, and nothing is open.
location.hash = '#MC1101';
route();
assert.equal(current, null);
assert.deepEqual([view.code, view.title], ['MC1101', null]);

// LEVEL 1.
location.hash = '#';
route();
assert.equal(view.code, null);

// The shape every link had before the levels existed: '#<note title>'. It has
// to land on the note and rewrite itself into the three-level URL.
location.hash = '#maths-2026-09-08';
DATA[0].notes.push({title: 'maths-2026-09-08', kind: 'lecture', md: '# x'});
route();
assert.equal(location.hash, '#MC1101/maths-2026-09-08', 'old link must be upgraded');
assert.equal(current.title, 'maths-2026-09-08');
DATA[0].notes.pop();

// A hash that means nothing falls back to level 1 instead of throwing.
location.hash = '#nothing-like-this';
route();
assert.equal(view.code, null);

// Back (browser button, Android gesture) climbs a level.
assert.equal(window.onpopstate, route, 'back must re-route');

// What level 1 prints under each subject.
assert.equal(counts(DATA[0]), '1 lecture \\u00b7 revision sheet');
assert.equal(counts(DATA[1]), 'Nothing yet');
assert.equal(hashOf('MC1101', 'week 1'), '#MC1101/week%201');

// + on a subject screen files it under that subject without touching the menu.
location.hash = '#MC1101';
route();
writes = [];
openSheet();
assert.ok(wrote(['value', 'MC1101']), 'the sheet must pre-select the subject');

// Anything slow says so: busy() has to raise the strip, not just fill it.
writes = [];
busy('transcribing');
assert.ok(wrote(['textContent', 'transcribing']));
assert.ok(wrote(['()', 'on']), 'busy() must show the strip');
"""


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_page_script_parses(tmp_path):
    """A stray brace in PAGE ships a blank app past a green suite otherwise."""
    f = tmp_path / "page.js"
    f.write_text(SCRIPT.replace("__DATA__", "[]"))
    r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_router_maps_hashes_to_levels(tmp_path):
    f = tmp_path / "router.js"
    f.write_text(STUB + SCRIPT.replace("__DATA__", json.dumps(DATA_FIXTURE)) + CHECKS)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# The strip is CSS, so this is the one thing here that reads the source rather
# than running it: it used to sit on top of the header and eat the taps meant
# for the back button and the search box.
def test_the_progress_strip_cannot_cover_the_header():
    rule = re.search(r"#busy\{(.*?)\}", notes.PAGE, re.S).group(1)
    assert "pointer-events:none" in rule, "the strip must never take a tap"
    assert "top:" not in rule, "the strip must not be anchored over the header"
    # + works while you read, so it must not be hidden there.
    assert "body.reading #fab" not in notes.PAGE
