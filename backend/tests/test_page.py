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


def test_no_auth_never_reaches_the_wifi_on_its_own(tmp_path):
    """--no-auth is the whole gate switched off: no invite code, no cookie, no
    approval. A verification server left running on *:8000 once served the
    entire library to the wifi, so with no --host it binds loopback only.
    """
    was = notes.LOG_PATH
    try:
        args = make_args(tmp_path)
        args.host = None                       # nobody passed --host
        srv = notes.build_server(args)
        srv.server_close()
        assert srv.server_address[0] == "127.0.0.1"
        assert args.host == "127.0.0.1", "serve() prints the address it bound"

        args = make_args(tmp_path)
        args.host = "0.0.0.0"                  # asked for on purpose: still given
        srv = notes.build_server(args)
        srv.server_close()
        assert srv.server_address[0] == "0.0.0.0"
    finally:
        notes.LOG_PATH = was


# ------------------------------------------------------------ the script

DATA_FIXTURE = [
    {"code": "MC1101", "name": "Mathematics 1", "uploads": [],
     "notes": [{"title": "week1", "kind": "lecture", "md": "# limits",
                "questions": [{"q": "q1", "a": "a1"}, {"q": "q2", "a": "a2"}]},
               {"title": "Revision sheet", "kind": "revision", "md": "# all",
                "questions": [{"q": "q3", "a": "a3"}]}]},
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

// ---- Practice, driven the way a student does it. ----
// The stub swallows an onclick assigned to a proxy, so this calls the handlers
// by name; test_practice_is_wired_up covers the assignments themselves.
const mc = DATA[0];
assert.equal(quizItems(mc, null).length, 3, 'a subject quiz draws from every note');
assert.equal(quizItems(mc, mc.notes[0]).length, 2, 'a note quiz draws from that note');
assert.equal(quizItems(mc, null)[2].from, 'Revision sheet', 'items say where they came from');

// A subject with no questions anywhere must not open an empty quiz.
assert.equal(quizItems(DATA[1], null).length, 0);
qOpen(DATA[1], null);
assert.equal(qz, null, 'an empty quiz must never open');

// One question at a time, with the answer withheld until it is asked for.
writes = [];
qOpen(mc, mc.notes[0]);
assert.ok(wrote(['textContent', '1 of 2']), 'progress through the quiz must be visible');
assert.ok(wrote(['innerHTML', 'q1']), 'the question is on screen');
assert.ok(!wrote(['innerHTML', 'a1']), 'the answer must stay hidden until asked for');
qReveal();
assert.ok(wrote(['innerHTML', 'a1']), 'showing the answer reveals it');

// Self-marked: one right, one wrong, then a score and a retry of just the miss.
qMark(true);
assert.equal(qz.i, 1, 'marking moves on');
assert.ok(wrote(['textContent', '2 of 2']), 'the counter follows');
qMark(false);
assert.ok(wrote(['textContent', '1 of 2 right']), 'the run ends with a score');
qRetry();
assert.deepEqual(qz.order, [1], 'retry runs only what was missed');
writes = [];
qReveal(); qMark(true);
assert.ok(wrote(['textContent', '1 of 1 right']), 'a clean retry scores full marks');

// localStorage is absent here, which is exactly how it behaves in private
// mode: it throws. Nothing above may have noticed.
qAgain();
assert.equal(qz.i, 0, 'the quiz works with no storage at all');
assert.equal(qz.order.length, 2);

// Practice is offered only where there is something to practise: a subject with
// no questions anywhere gets no 'Practice the whole course' row to tap.
writes = [];
renderSubject(DATA[1]);
assert.ok(!wrote(['textContent', 'Practice the whole course']),
          'a subject with nothing to practise must not offer a whole-course quiz');

// ---- Saved progress. Everything above ran without storage; this is with. ----
const store = new Map();
globalThis.localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
};
qOpen(mc, mc.notes[0]);
qReveal(); qMark(true);                       // one answered, one to go
qz = null;                                    // a refresh: new page, same storage
qOpen(mc, mc.notes[0]);
assert.equal(qz.i, 1, 'a half-finished quiz must resume where it stopped');
assert.deepEqual(qz.marks, [1], 'and remember how it was going');

// The note was re-recorded and now has a third question: the old run is meaningless.
qz = null;
qOpen(mc, {title: 'week1', kind: 'lecture', md: '# x',
           questions: mc.notes[0].questions.concat([{q: 'q9', a: 'a9'}])});
assert.equal(qz.i, 0, 'a run against a different set of questions must be dropped');

// One saved run per quiz, not one for the whole app: the subject run and the
// note run inside it must not overwrite each other.
qz = null; qOpen(mc, null);
const subjectKey = qz.key;
qz = null; qOpen(mc, mc.notes[0]);
assert.ok(qz.key !== subjectKey, 'a note quiz and its subject quiz keep separate runs');
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


def test_practice_is_wired_up():
    """The node stub cannot see an onclick assigned to a proxy, so the checks
    above drive the handlers by name. This is the other half: that the buttons
    are actually attached to them."""
    for wiring in ("qshow.onclick = qReveal", "qright.onclick = () => qMark(true)",
                   "qwrong.onclick = () => qMark(false)", "qretry.onclick = qRetry",
                   "qagain.onclick = qAgain", "practice.onclick = ", "b.onclick = () => qOpen(s, null)",
                   # The overlay is not a URL, so the close button is one of only
                   # two ways out of it. Break it and the student is trapped.
                   "document.getElementById('qexit').onclick = () => { quiz.hidden = true; }",
                   # Offered only where there is something to practise, and gone
                   # again when the note it belongs to is closed.
                   "practice.hidden = !questionsOf(n).length",
                   "practice.hidden = true",
                   # The dock's one accent follows the primary action.
                   "classList.toggle('primary', practice.hidden)"):
        assert wiring in notes.PAGE, f"practice button not wired: {wiring}"


def test_back_closes_practice_first():
    """The other way out of the overlay, and this one is the router's. Without
    it the Android back gesture leaves the app from underneath an open quiz."""
    assert "if (quiz.hidden === false) quiz.hidden = true;" in notes.PAGE


def test_a_note_ships_the_questions_it_already_contains(tmp_path):
    """Quiz mode costs no API call: the questions are parsed out of the note on
    the way to the phone, and a note without any offers no practice."""
    folder = tmp_path / "library" / "MC1101-Mathematics-1"
    (folder / "lectures").mkdir(parents=True)
    (folder / "lectures" / "week1.md").write_text(
        "## Questions\n\n**1. Define a limit.**\n\n"
        "<details><summary>Answer</summary>\n\nWhat $f(x)$ approaches.\n\n</details>\n\n"
        "<details><summary>Full transcript</summary>\n\n```\n[00:00] hi\n```\n\n</details>\n")
    (folder / "lectures" / "week2.md").write_text("## Summary\n\nno questions here\n")

    data = notes.build_data(tmp_path / "library", tmp_path)
    got = {n["title"]: n["questions"] for n in
           next(s for s in data if s["code"] == "MC1101")["notes"]}
    assert got["week1"] == [{"q": "Define a limit.", "a": "What $f(x)$ approaches."}]
    assert got["week2"] == [], "a note with no questions must offer no practice"


# The strip is CSS, so this is the one thing here that reads the source rather
# than running it: it used to sit on top of the header and eat the taps meant
# for the back button and the search box.
def test_the_progress_strip_cannot_cover_the_header():
    rule = re.search(r"#busy\{(.*?)\}", notes.PAGE, re.S).group(1)
    assert "pointer-events:none" in rule, "the strip must never take a tap"
    assert "top:" not in rule, "the strip must not be anchored over the header"
    # + works while you read, so it must not be hidden there.
    assert "body.reading #fab" not in notes.PAGE
