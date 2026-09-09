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
  get: (t, k) => (k === 'value' ? qvalue : k === Symbol.toPrimitive ? () => '' : any),
  set: (t, k, v) => (writes.push([k, v]), true),
  apply: (t, self, a) => (writes.push(['()'].concat(a)), any),
  construct: () => any,
});
const wrote = pair => writes.some(w => JSON.stringify(w) === JSON.stringify(pair));
// #ask is the one element whose class a check has to tell apart from every
// other 'on', so it is a real object rather than the proxy.
const askCls = [];
const askEl = {style: {}, classList: {
  add: c => askCls.push('+' + c), remove: c => askCls.push('-' + c),
  contains: () => false,
}};
const document = new Proxy(function () {}, {
  get: (t, k) => (k === 'getElementById' ? (id => (id === 'ask' ? askEl : any)) : any),
  set: (t, k, v) => (writes.push([k, v]), true),
  apply: (t, self, a) => (writes.push(['()'].concat(a)), any),
});
let qvalue = '';   // whatever is in the search box, so its scope can be tested
// The page is loaded the way the hard case arrives: a deep link, one history
// entry, straight into a note. What seedHistory() does about that on the way
// in is the first thing CHECKS looks at.
const location = {hash: '#classes/MC1101/week1'};
let hist = ['#classes/MC1101/week1'];   // every entry pushed, so back can be reasoned about
const history = {
  pushState: (a, b, h) => { hist.push(h); location.hash = h; },
  replaceState: (a, b, h) => { hist[hist.length - 1] = h; location.hash = h; },
  back: () => {},
};
const window = {scrollTo: () => {}, print: () => {}};
const marked = {parse: md => md};
const fetch = () => new Promise(() => {});
const setInterval = () => 0, setTimeout = () => 0, clearInterval = () => {};
"""

CHECKS = """
// Loading was the test: the script called seedHistory() on its way in, so the
// deep link that arrived as a single entry already has every step above it
// under it. Delete that call and back leaves the app in one press.
assert.deepStrictEqual(hist,
  ['#home', '#classes', '#classes/MC1101', '#classes/MC1101/week1'],
  'loading a deep link must seed the steps above it');
hist = [];

// LEVEL 3, inside the Classes tab: three segments open that note.
location.hash = '#classes/MC1101/week1';
route();
assert.equal(current && current.title, 'week1', 'a note hash must open the note');
assert.deepStrictEqual([view.tab, view.code], ['classes', 'MC1101']);

// LEVEL 2: the subject, and nothing open.
location.hash = '#classes/MC1101';
route();
assert.equal(current, null);
assert.deepStrictEqual([view.tab, view.code, view.title], ['classes', 'MC1101', null]);

// LEVEL 1: the tab's own root.
location.hash = '#classes';
route();
assert.deepStrictEqual([view.tab, view.code], ['classes', null]);

// The other three tabs are levels of their own, none of them is a subject, and
// tapping one while reading has to put the note away -- with the class that
// hid the tab bar, and with the Explain button that was floating over the note.
for (const t of ['home', 'campus', 'me']) {
  location.hash = '#classes/MC1101/week1';
  route();
  assert.ok(current, 'a note is open before the tab is tapped');
  writes = []; askCls.length = 0;
  location.hash = '#' + t;
  route();
  assert.deepStrictEqual([view.tab, view.code, current], [t, null, null], t + ' is a tab');
  assert.ok(wrote(['()', 'reading']),
            t + ': leaving a note must drop body.reading, or the tab bar never comes back');
  assert.ok(askCls.includes('-on'),
            t + ': leaving a note must take the Explain button with it');
}

// Every link that predates the tabs still has to land, and upgrade in place.
location.hash = '#MC1101/week1';
route();
assert.equal(location.hash, '#classes/MC1101/week1', 'an old note link must be upgraded');
assert.equal(current.title, 'week1');

location.hash = '#MC1101';
route();
assert.equal(location.hash, '#classes/MC1101', 'an old subject link must be upgraded');

// The shape every link had before the levels existed: '#<note title>'.
location.hash = '#maths-2026-09-08';
DATA[0].notes.push({title: 'maths-2026-09-08', kind: 'lecture', md: '# x'});
route();
assert.equal(location.hash, '#classes/MC1101/maths-2026-09-08', 'oldest link must be upgraded');
assert.equal(current.title, 'maths-2026-09-08');
DATA[0].notes.pop();

// A hash that means nothing falls back to the first tab instead of throwing.
location.hash = '#nothing-like-this';
route();
assert.deepStrictEqual([view.tab, view.code], ['home', null]);
location.hash = '#classes/NOPE';
route();
assert.deepStrictEqual([view.tab, view.code], ['classes', null], 'no such subject');

// Back (browser button, Android gesture) climbs a step.
assert.equal(window.onpopstate, route, 'back must re-route');

// Landing deep must never take one press to leave the app: every step above
// the link is seeded under it first.
hist = ['#classes/MC1101/week1'];
location.hash = hist[0];
seedHistory();
assert.deepStrictEqual(hist, ['#home', '#classes', '#classes/MC1101', '#classes/MC1101/week1'],
                 'a deep link must be reachable by walking back out of it');
hist = ['#me'];
location.hash = '#me';
seedHistory();
assert.deepStrictEqual(hist, ['#home', '#me'], 'a tab is one step above the root');
hist = [];
location.hash = '';
seedHistory();
assert.deepStrictEqual(hist, [], 'the root seeds nothing');

// Tapping the tab you are on must not stack history entries to walk back out of.
location.hash = '#campus';
hist = [];
go('campus');
assert.deepStrictEqual(hist, [], 'the tab you are already on is not a new entry');
go('classes');
assert.deepStrictEqual(hist, ['#classes']);

// What level 1 prints under each subject.
assert.equal(counts(DATA[0]), '1 lecture \\u00b7 revision sheet');
assert.equal(counts(DATA[1]), 'Nothing yet');
assert.equal(hashOf('classes', 'MC1101', 'week 1'), '#classes/MC1101/week%201');

// + on a subject screen files it under that subject without touching the menu.
location.hash = '#classes/MC1101';
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
assert.deepStrictEqual(qz.order, [1], 'retry runs only what was missed');
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
assert.deepStrictEqual(qz.marks, [1], 'and remember how it was going');

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

// ---- Attribution and votes. ----
// Only a file the database actually holds a row for can be voted on: an id is
// the page's evidence that there is something to vote against.
writes = [];
fileRow({name: 'slides.pdf', path: 'p', by: 'Asha', id: 'm1', votes: 3, voted: false}, mc);
assert.ok(wrote(['textContent', 'added by Asha']), 'a file says who added it');
assert.ok(wrote(['className', 'vote']), 'a registered file gets a vote control');
assert.ok(wrote(['textContent', 3]), 'the count rides inside the control');

writes = [];
fileRow({name: 'orphan.pdf', path: 'p'}, mc);
assert.ok(!wrote(['className', 'vote']), 'no row behind it, no vote button');

writes = [];
fileRow({name: 'mine.pdf', path: 'p', id: 'm2', votes: 1, voted: true}, mc);
assert.ok(wrote(['className', 'vote on']), 'your own vote reads as pressed');
assert.ok(wrote(['()', 'aria-pressed', 'true']), 'and says so to a screen reader');

// A lecture is not voted on -- it is the record of a class -- but it still
// says who recorded it.
writes = [];
noteRow({title: 'week1', kind: 'lecture', md: '', by: 'Bilal'}, mc);
assert.ok(wrote(['textContent', 'recorded by Bilal']));
writes = [];
noteRow({title: 'week2', kind: 'lecture', md: ''}, mc);
assert.ok(wrote(['textContent', '']), 'an unattributed note says nothing, not "by nobody"');

// Contributions is the Me tab, so back climbs out of it rather than leaving
// the app.
location.hash = '#me';
route();
assert.equal(view.tab, 'me');
assert.equal(current, null, 'the contributions screen is not a note');
writes = [];
render();
assert.ok(wrote(['textContent', 'Your contributions']), 'and that header must name it');

// Which chrome each tab carries. The brand is Home's and the subject header is
// everyone else's, because #lback -- the only back button at this depth --
// lives inside it; the search box belongs to Classes and the activity log to
// Me; and #lback itself only appears where there is a level above to climb to.
// render() writes .hidden in one order: brand, shead, lback, scode, q, tools.
const chrome = h => {
  location.hash = h; route(); writes = []; render();
  return writes.filter(w => w[0] === 'hidden').map(w => w[1]).slice(0, 6);
};
assert.deepStrictEqual(chrome('#home'), [false, true, true, true, true, true],
                       'Home keeps the brand and nothing else');
assert.deepStrictEqual(chrome('#classes'), [true, false, true, true, false, true],
                       'the search box is the Classes tab\\'s');
assert.deepStrictEqual(chrome('#classes/MC1101'), [true, false, false, false, false, true],
                       'a subject is the one level with a way back up');
assert.deepStrictEqual(chrome('#campus'), [true, false, true, true, true, true],
                       'Campus has no search box and no log');
assert.deepStrictEqual(chrome('#me'), [true, false, true, true, true, false],
                       'the activity log is the Me tab\\'s');

// + from here must leave the dropdown alone: 'me' matches no option, so the
// select blanks, and a recording uploaded with no subject is refused and lost.
writes = [];
openSheet();
assert.ok(!writes.some(w => w[0] === 'value'), 'nothing may be filed under #me');
location.hash = '#';
route();
assert.equal(view.code, null);

// Campus has nothing in it yet and says what is coming rather than showing an
// empty screen that reads as a bug -- and the router is what has to reach it,
// so this goes through the hash rather than calling it by name.
const says = word => writes.some(w => w[0] === 'textContent' && String(w[1]).includes(word));
writes = []; location.hash = '#campus'; route();
assert.ok(says('Clubs'), 'Campus must name what will live there');

// Search is the Classes tab's own tool. A needle left in the box must not turn
// Campus into a list of search hits the moment you tap over to it.
const nomatch = 'Nothing matches that. Try a subject code like CY1107.';
qvalue = 'zzz-nothing-matches-this';
writes = []; location.hash = '#classes'; route();
assert.ok(wrote(['textContent', nomatch]), 'Classes answers the search box');
writes = []; location.hash = '#campus'; route();
assert.ok(!wrote(['textContent', nomatch]) && says('Clubs'), 'Campus must not be searched');
qvalue = '';

// ---- HOME. Every section is a function of what the page already holds --
// TT, JOBS, PENDING, DATA -- so all of it can be driven from here.
const home = () => { writes = []; location.hash = '#home'; route(); };

// "Today" has to be the day it actually is. 9 September 2026 is a Wednesday.
assert.equal(dayOf(new Date(2026, 8, 9)), 3, 'Wednesday is day 3');
assert.equal(dayOf(new Date(2026, 8, 12)), 6, 'Saturday is the last taught day');
assert.equal(dayOf(new Date(2026, 8, 13)), 0, 'Sunday is day 0 and has no periods');
const week = [{day: 3, period: 2, code: 'CY1107'}, {day: 4, period: 1, code: 'MC1101'},
              {day: 3, period: 1, code: 'MC1101'}];
assert.deepStrictEqual(slotsOn(week, 3).map(s => s.period), [1, 2],
                       'today is only today, and in period order');
assert.deepStrictEqual(slotsOn(week, 6), [], 'a day with no classes has none');
assert.deepStrictEqual(slotsOn(null, 3), [], 'no timetable at all is not a crash');

// Nothing heard from the server yet: say so. Never draw a plausible Monday.
TT = null; JOBS = []; PENDING = 0; SEEN = null;
home();
assert.ok(says('Checking your timetable'), 'Home must not invent a timetable');
assert.ok(!says('Needs you'), 'nothing is waiting, so there is no section');
assert.ok(!says('New since'), 'no last visit, nothing to call new');

// Empty is the honest first state, and it offers the way to fill it in.
TT = []; live = true;
home();
assert.ok(says('Set up your timetable'), 'an empty timetable says how to fill it');

// A day with classes lists them in order, tappable, marked with what is there.
TT = [{day: 3, period: 3, code: 'CY1107'}, {day: 3, period: 1, code: 'MC1101'}];
const wed = new Date(2026, 8, 9);
const RealDate = Date;
Date = function () { return wed; };          // "today" is Wednesday for this stretch
Date.now = RealDate.now;
home();
const order = writes.filter(w => w[0] === 'textContent'
                                && String(w[1]).startsWith('Period ')).map(w => w[1]);
assert.deepStrictEqual(order, ['Period 1 \\u00b7 1 lecture \\u00b7 revision sheet',
                               'Period 3 \\u00b7 no notes yet'],
                       'the day runs in period order, each marked with what it has');
assert.ok(says('Mathematics 1') && says('Chemistry'), 'and each is the subject itself');

// Sunday is a real answer, not an empty list.
Date = function () { return new RealDate(2026, 8, 13); };
Date.now = RealDate.now;
home();
assert.ok(says('No classes on Sunday.'), 'a free day says so');
Date = RealDate;

// NEEDS YOU: only what is actually waiting on a person.
JOBS = [{id: 1, name: 'CY1107-lec.m4a', state: 'transcribing', detail: '40% · 2 of 5 min'},
        {id: 2, name: 'slides.pdf', state: 'done', detail: 'filed under CY1107'}];
home();
assert.ok(says('Needs you'), 'a running transcription needs you');
assert.ok(says('40%'), 'and says how far along it is');
JOBS = [{id: 2, name: 'slides.pdf', state: 'done', detail: 'filed under CY1107'}];
home();
assert.ok(!says('Needs you'), 'a finished job is not waiting on anyone');
PENDING = 2;
home();
assert.ok(says('2 people are waiting to be let in'), "the admin's queue needs them");
PENDING = 1;
home();
assert.ok(says('One person is waiting to be let in'), 'and it counts in words');
PENDING = 0; JOBS = [];

// NEW SINCE YOU LAST LOOKED, against the last look and nothing else.
SEEN = 1000;
home();
assert.ok(says('Nothing new since you last looked.'), 'nothing new must say so');
DATA[0].notes[0].at = 2000;
home();
assert.ok(says('New since you last looked') && says('week1'), 'a newer note is new');
DATA[0].notes[0].at = 900;
home();
assert.ok(says('Nothing new since you last looked.'), 'an older note is not new');
delete DATA[0].notes[0].at;
SEEN = null;

// An empty library says what to do first rather than showing a blank screen.
const keep = DATA.splice(0, DATA.length);
DATA.push({code: 'MC1101', name: 'Mathematics 1', notes: [], uploads: []});
SEEN = 1000;
home();
assert.ok(says('Nothing in the library yet'), 'day one must say what to do first');
assert.ok(!says('Nothing new since you last looked'), 'nothing can be new in an empty library');
DATA.length = 0; DATA.push(...keep);
SEEN = null;

// The editor is a level inside Home: its own URL, with a way back up.
TT = [{day: 3, period: 1, code: 'MC1101'}];
writes = []; location.hash = '#home/timetable'; route();
assert.deepStrictEqual([view.tab, view.edit], ['home', true], 'the editor is a level');
assert.ok(says('Period 1') && says('Period 8'), 'every period of the day is editable');
assert.ok(says('Save timetable'), 'and there is a way to save it');
assert.deepStrictEqual(chrome('#home/timetable'), [true, false, false, true, true, true],
                       'the editor names itself and keeps a way back up');
assert.equal(draft['3-1'], 'MC1101', 'the editor opens on what is already saved');
draft['3-2'] = 'CY1107';
location.hash = '#home'; route();
assert.equal(draft, null, 'walking away drops an unsaved week rather than saving it');
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


def test_the_vote_control_is_wired_to_the_server_and_nothing_else():
    """The stub cannot see an onclick assigned to a proxy, so the checks above
    build the control and this is the other half: what pressing it does."""
    for wiring in (
        # One request, carrying which item and which direction.
        "body: JSON.stringify({id: u.id, on: !u.voted})",
        "if (u.id) el.appendChild(voteBtn(u));",
    ):
        assert wiring in notes.PAGE, f"vote control not wired: {wiring}"
    # And then the whole list again, because a vote changes the ranking. Read
    # out of the handler itself: /revise refreshes too, so a page-wide grep for
    # this line passed with the vote's own refresh deleted.
    handler = re.search(r"function voteBtn\(u\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "await refresh();" in handler, "a vote must re-read the list it re-ranks"


def test_the_tab_bar_is_wired_and_gives_way_to_the_reading_dock():
    """The bar is the shell. The stub cannot see an onclick on a proxy or read
    CSS, so this reads the source: that a tab navigates, and that the one place
    the bar is not shown is under an open note, where the dock takes the strip.
    """
    for wiring in ("tabBtns.forEach(b => { b.onclick = () => go(b.dataset.tab); });",
                   'data-tab="home"', 'data-tab="classes"',
                   'data-tab="campus"', 'data-tab="me"',
                   # Reading takes the strip; back gives it straight back,
                   # because closeRead() drops the class that hid it.
                   "body.reading .tabs{display:none}",
                   # ...except on a wide screen, where the list stays beside
                   # the note and the dock starts at its edge.
                   "body.reading .tabs{display:flex}"):
        assert wiring in notes.PAGE, f"tab bar not wired: {wiring}"
    # The last row of a list must clear both the bar and the FAB floating above
    # it, not sit under either: the FAB reaches 76 + 58 = 134px up, and at 80px
    # it covered the bottom 54px of the list -- the last row's vote button with
    # it -- with no scroll left to escape.
    assert "#nav{padding-bottom:calc(142px + env(safe-area-inset-bottom))}" in notes.PAGE
    fab = re.search(r"#fab\{(.*?)\}", notes.PAGE, re.S).group(1)
    assert "bottom:calc(76px + env(safe-area-inset-bottom))" in fab and "height:58px" in fab, \
        "if the FAB moves, #nav's padding has to move with it"


def test_home_costs_no_request_of_its_own():
    """Home is opened between classes on mobile data, so it may not add a round
    trip: the timetable, the admin queue and the clock all ride on the /data the
    page fetches on the way in, and the jobs on the poll that already runs."""
    for wiring in ("TT = d.timetable || [];", "PENDING = d.pending || 0;",
                   "markSeen(d.now);", "JOBS = jobs;"):
        assert wiring in notes.PAGE, f"Home not wired: {wiring}"
    # The editor's save is the only thing that talks to /timetable at all.
    assert notes.PAGE.count("fetch('/timetable'") == 1
    assert "body: JSON.stringify({slots})" in notes.PAGE
    # And Home must not be one of the screens that waits on a fetch to draw.
    home = re.search(r"\nfunction renderHome\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "await" not in home and "fetch" not in home


def test_home_learns_the_server_is_there_before_it_draws():
    """`live` is what decides whether an empty timetable reads as "set one up"
    or as "there is no server to save it to". Set after the render rather than
    before it, a perfectly good server said the second one -- and nothing
    redrew Home afterwards to correct it."""
    body = re.search(r"async function refresh\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert body.index("live = true;") < body.index("render();")


def test_the_timetable_editor_is_reachable_and_leads_back():
    """A level inside Home, so it is a URL: back climbs out of it like every
    other step, and the tab bar is never the only way home."""
    for wiring in ("const edit = tab === 'home' && parts[1] === 'timetable';",
                   "if (!edit) draft = null;",
                   "start.onclick = () => go('home', 'timetable');",
                   "edit.onclick = () => go('home', 'timetable');",
                   "save.onclick = saveTimetable;"):
        assert wiring in notes.PAGE, f"the timetable editor is not wired: {wiring}"


def test_the_library_says_when_each_thing_arrived(tmp_path):
    """"New since you last looked" is a comparison against these, and disk is
    what knows -- so the static export carries them too."""
    folder = tmp_path / "library" / "MC1101-Mathematics-1"
    (folder / "lectures").mkdir(parents=True)
    (folder / "lectures" / "week1.md").write_text("## Summary\nlimits\n")
    (folder / "uploads").mkdir(parents=True)
    (folder / "uploads" / "slides.pdf").write_bytes(b"%PDF-1.4")

    mc = next(s for s in notes.build_data(tmp_path / "library", tmp_path)
              if s["code"] == "MC1101")
    assert mc["notes"][0]["at"] > 0 and mc["uploads"][0]["at"] > 0
    assert all(isinstance(x["at"], int)
               for x in mc["notes"] + mc["uploads"]), "seconds, like the server's clock"


def test_the_me_tab_carries_the_two_admin_things():
    """Contributions, plus -- for an admin only -- the invite code and the way
    to /admin. A member's /me carries no code to leak in the first place."""
    for wiring in ("if (d.admin) {",
                   "navigator.clipboard.writeText(d.invite)",
                   "a.href = '/admin';",
                   "block('Admin', rows);"):
        assert wiring in notes.PAGE, f"the Me tab is missing: {wiring}"


def test_points_never_gate_anything_on_the_page():
    """An explicit product decision, and the kind that rots quietly. Nothing on
    this page may branch on a score."""
    for lock in ("points <", "score <", "score >=", "points >=", "if (score",
                 "score &&", "unlock"):
        assert lock not in notes.PAGE, f"points became a gate: {lock!r}"


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
