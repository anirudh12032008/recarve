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
  get: (t, k) => (
    k === 'getElementById' ? (id => els[id] || any)
    : k === 'getSelection' ? getSelection
    // The one listener whose decision is worth checking: what a highlight does
    // is a rule (only inside a note, only a real phrase, only for a member who
    // may spend), and the proxy would swallow it.
    : k === 'addEventListener'
      ? ((ev, fn) => { if (ev === 'selectionchange') onSelectionChange = fn; })
    : any),
  set: (t, k, v) => (writes.push([k, v]), true),
  apply: (t, self, a) => (writes.push(['()'].concat(a)), any),
});
let qvalue = '';   // whatever is in the search box, so its scope can be tested
// The page is loaded the way the hard case arrives: a deep link, one history
// entry, straight into a note. What seedHistory() does about that on the way
// in is the first thing CHECKS looks at.
const location = {hash: '#classes/MC1101/week1'};
let hist = ['#classes/MC1101/week1'];   // every entry pushed, so back can be reasoned about
let backs = 0;                          // and every press of it, so a save can be shown to leave
const history = {
  pushState: (a, b, h) => { hist.push(h); location.hash = h; },
  replaceState: (a, b, h) => { hist[hist.length - 1] = h; location.hash = h; },
  back: () => { backs++; },
};
const window = {scrollTo: () => {}, print: () => {}, innerWidth: 390, scrollY: 0};
// What is highlighted, and the one listener that reads it. document.addEventListener
// goes into the proxy like everything else, so the script's handler is caught
// here by name instead -- it is the decision that matters, not the event.
// #panel is real for the same reason #ask is: the Explain guard asks whether
// the panel is already open, and the proxy answers 'yes' to every question.
const panelEl = {classList: {add: () => {}, remove: () => {}, contains: () => false}};
// #fab is real for the same reason: render() sets .hidden on seven other
// elements every route, so `wrote(['hidden', true])` was true before applyRole
// had done anything at all.
const fabEl = {hidden: null, className: null};
// The banner in the add sheet that says who adding is for. Real, because
// whether it is showing is the other half of what a locked + button means.
const lockEl = {hidden: null};
const els = {ask: askEl, panel: panelEl, fab: fabEl, lock: lockEl};
let selection = '';
let onSelectionChange = () => {};
const rect = {top: 100, left: 20, width: 80};
const getSelection = () => ({
  toString: () => selection,
  anchorNode: {},
  getRangeAt: () => ({getBoundingClientRect: () => rect}),
});
const marked = {parse: md => md};
// Every request the script makes, and what the next one is answered with.
// `reply = null` hangs, which is what the page sees before a check sets one --
// including the refresh() it fires on the way in.
let fetches = [], reply = null;
// 'gone' is a page with no server behind it at all -- the static export --
// where fetch itself rejects rather than answering anything.
const fetch = (url, init) => {
  fetches.push([url, init]);
  if (reply === 'gone') return Promise.reject(new TypeError('Failed to fetch'));
  return reply ? Promise.resolve(reply) : new Promise(() => {});
};
const answer = (ok, body) => ({ok, status: ok ? 200 : 503, json: async () => body});
const setInterval = () => 0, setTimeout = () => 0, clearInterval = () => {};
"""

CHECKS = """
// Nothing has answered /data yet -- the refresh() the script fired on the way
// in is still hanging on `reply = null`. The Add button is already away: it is
// shown on an answer, not hidden on one, so a student never gets the window in
// which it is there and /upload would 403 the tap.
assert.equal(ROLE, null, 'nobody has a role until the server gives them one');
assert.equal(fabEl.hidden, true, 'the Add button starts hidden, not shown');

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
assert.ok(wrote(['textContent', 'Your profile']), 'and that header must name it');

// Which chrome each tab carries. The brand is Home's and the subject header is
// everyone else's, because #lback -- the only back button at this depth --
// lives inside it; the search box belongs to Classes and the activity log to
// Me; and #lback itself only appears where there is a level above to climb to.
// The jobs box is chrome too, and Home is the one screen that must not show
// it: Home draws the same running transcription under "Needs you", and two
// copies of it on one screen is one copy too many.
// render() writes .hidden in one order: brand, shead, lback, scode, q, tools, jobs.
const chrome = h => {
  location.hash = h; route(); writes = []; render();
  return writes.filter(w => w[0] === 'hidden').map(w => w[1]).slice(0, 7);
};
assert.deepStrictEqual(chrome('#home'), [false, true, true, true, true, true, true],
                       'Home keeps the brand and nothing else');
assert.deepStrictEqual(chrome('#classes'), [true, false, true, true, false, true, false],
                       'the search box is the Classes tab\\'s');
assert.deepStrictEqual(chrome('#classes/MC1101'), [true, false, false, false, false, true, false],
                       'a subject is the one level with a way back up');
assert.deepStrictEqual(chrome('#campus'), [true, false, true, true, true, true, false],
                       'Campus has no search box and no log');
assert.deepStrictEqual(chrome('#campus/new'), [true, false, false, true, true, true, false],
                       'the composer is a level inside Campus, so it has a way back up');
assert.deepStrictEqual(chrome('#me'), [true, false, true, true, true, false, false],
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

// THE WAY INTO THE ADMIN PANEL, from the dashboard an admin actually opens.
// On ROLE and nothing else: PENDING is only ever non-zero for an admin, so a
// queue-shaped row would have hidden a plain admin behind an empty queue.
ROLE = 'student';
home();
assert.ok(!says('Class admin'), 'a student is offered no way into the admin panel');
ROLE = 'trusted';
home();
assert.ok(!says('Class admin'), 'nor is a trusted member');
ROLE = null;
home();
assert.ok(!says('Class admin'), 'and neither is a session nobody has named yet');
ROLE = 'admin';
home();
assert.ok(says('Class admin'), 'an admin is');
assert.ok(wrote(['className', 'row adm']),
          'and it is inked, like every other control only they may press');
assert.ok(wrote(['href', '/admin']), 'pointing at the route the server gates');
ROLE = null;

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
assert.deepStrictEqual(chrome('#home/timetable'), [true, false, false, true, true, true, true],
                       'the editor names itself and keeps a way back up');
assert.equal(draft['3-1'], 'MC1101', 'the editor opens on what is already saved');
draft['3-2'] = 'CY1107';
location.hash = '#home'; route();
assert.equal(draft, null, 'walking away drops an unsaved week rather than saving it');

// ---- Everything that waits on a promise, in one pass at the end. This file
// is CommonJS, so top-level await is a syntax error; a rejection in here still
// exits node non-zero, which is what the pytest wrapper reads.
(async () => {

// A /data that answers badly is not a page opened as a file. 403 (blocked
// mid-visit) and 503 (Postgres down) are both real answers from this server,
// and returning on them left Home on 'Checking your timetable...' with `live`
// false, the job poll switched off, and nothing left to switch it back on.
TT = null; live = true;
reply = answer(false, {error: 'the library is offline'});
await refresh();
assert.equal(live, false, 'a refused /data means there is no server to save to');
assert.deepStrictEqual(TT, [], 'and Home is told, rather than left waiting');
home();
assert.ok(!says('Checking your timetable'), 'Home may not sit on checking forever');
assert.ok(says('Your timetable needs the server'), 'it has to say what is wrong');

// ---- Saving the week. The stub swallows save.onclick, so this drives
// saveTimetable() by name, the way the practice checks drive the quiz.
TT = []; draft = {'3-1': 'MC1101', '3-3': ''};
fetches = []; backs = 0;
reply = answer(true, {saved: 1});
await saveTimetable();
assert.equal(fetches[0][0], '/timetable', 'the save is the one request Home makes');
assert.deepStrictEqual(JSON.parse(fetches[0][1].body),
                       {slots: [{day: 3, period: 1, code: 'MC1101'}]},
                       'day then period, and a period put back to free is not a slot');
assert.deepStrictEqual(TT, [{day: 3, period: 1, code: 'MC1101'}],
                       'Home shows the week that was just saved, not the old one');
assert.equal(draft, null, 'a saved week is no longer a draft');
assert.equal(backs, 1, 'and saving leaves the editor');

// A refused save may not read as a saved one: the week on screen stays
// whatever the server actually holds, and the reason is on screen.
const saved = TT;
draft = {'3-1': 'CY1107'};
reply = answer(false, {error: 'day 9 is not a day'});
writes = []; backs = 0;
await saveTimetable();
assert.deepStrictEqual(TT, saved, 'a refused save changes nothing');
assert.ok(says('day 9 is not a day'), 'and says why it was refused');
assert.ok(!says('Timetable saved'), 'a refused save must never report success');
assert.equal(backs, 0, 'and leaves the student in the editor to fix it');

// Every pick has to land in the draft: those selects are the only input the
// save has, and an onchange that does nothing re-posts the old week in silence.
location.hash = '#home/timetable'; route();
writes = [];
renderTimetable();
const picks = writes.filter(w => w[0] === 'onchange');
assert.equal(picks.length, PERIODS, 'one select per period');
qvalue = 'CY1107';
picks[PERIODS - 1][1]();
assert.equal(draft[slotKey(draftDay, PERIODS)], 'CY1107', 'a pick must land in the draft');
qvalue = ''; draft = null;

// ---- The jobs poll, which is the only thing that keeps 'Needs you' current.
// 'Needs you' is written by Home's render and by nothing else; the job's own
// text is written by the jobs box either way, so it cannot tell them apart.
live = true; JOBS = []; lastJobs = '';
const moving = d => answer(true, {jobs: [
  {id: 1, name: 'CY1107-lec.m4a', state: 'transcribing', detail: d}]});
reply = moving('10% - 1 of 5 min');
home(); writes = [];
await pollJobs();
assert.ok(says('Needs you'), 'a job that moved must redraw Home under the student');
writes = [];
await pollJobs();
assert.ok(!says('Needs you'), 'a job that has not moved must not fight the thumb');
reply = moving('60% - 3 of 5 min');
writes = [];
await pollJobs();
assert.ok(says('Needs you'), 'and the next step of it redraws again');
reply = moving('80% - 4 of 5 min');
location.hash = '#classes/MC1101'; route(); writes = [];
await pollJobs();
assert.ok(!says('Needs you'), 'it is Home that is redrawn, not whatever else is open');

// ---- What the server says this session may do. The lock is the server's;
// what the page owes a student is not offering the two buttons it would refuse.
location.hash = '#home'; route();
reply = answer(true, {subjects: [], codes: [], now: 1, role: 'student'});
writes = [];
await refresh();
assert.equal(ROLE, 'student', 'the role rides in on the /data the page already fetches');
// Shown, not hidden. A + button that is simply absent reads as an app that
// does not do that at all, so the student never learns the thing exists or
// who could give it to them.
assert.equal(fabEl.hidden, false, 'a student still sees the + button');
assert.equal(fabEl.className, 'locked', 'and it says it is not theirs yet');
assert.equal(lockEl.hidden, false, 'the sheet says who adding is for');

// Explain is offered too, and marked -- and tapping it explains itself rather
// than spending a request that comes back 403.
askCls.length = 0;
current = {title: 'week1'};
selection = 'a long enough phrase to explain';
onSelectionChange();
assert.ok(askCls.includes('+on'), 'a student is offered the Explain button');
assert.ok(askCls.includes('+locked'), 'in a state that says it is not theirs');
fetches = []; writes = [];
await askEl.onclick();
assert.ok(wrote(['textContent', LOCK_EXPLAIN]), 'tapping it says who Explain is for');
assert.ok(!fetches.some(f => f[0] === '/explain'),
          'and a locked Explain must never spend the API call it is locked for');
assert.ok(/trusted/.test(LOCK_EXPLAIN) && /admin/.test(LOCK_EXPLAIN),
          'the copy has to say what would unlock it, not just that it is locked');

reply = answer(true, {subjects: [], codes: [], now: 1, role: 'trusted'});
writes = []; askCls.length = 0;
await refresh();
assert.equal(ROLE, 'trusted');
assert.equal(fabEl.hidden, false, 'and a trusted member gets the Add button back');
assert.equal(fabEl.className, '', 'unlocked');
assert.equal(lockEl.hidden, true, 'with no lock banner in the sheet');
onSelectionChange();
assert.ok(askCls.includes('+on'), 'a trusted member is offered Explain');
assert.ok(!askCls.includes('+locked'), 'and it is not marked as locked');

// ---- The two ways /data can fail to say anything.
// A 503 (Postgres down) or a 403 is an answer, and it is not a yes. Before
// this, the catch left ROLE alone -- so a student kept the Add button for the
// life of the page and the sheet's only reply was "/upload needs trusted
// access", a route name, over "tap an option to try again".
ROLE = null;
reply = answer(false, {error: 'the library is offline'});
await refresh();
assert.equal(ROLE, null, 'a 503 is not permission');
fabEl.hidden = null; applyRole();
assert.equal(fabEl.hidden, true,
             'so the Add button stays away -- unknown is not a role to lock');
askCls.length = 0;
onSelectionChange();
assert.ok(!askCls.includes('+on'), 'and Explain, which spends money, is not offered');

// Nothing answered at all, and nothing ever will: that is the exported file,
// which has no server, nobody to be, and every button worth leaving on.
ROLE = null;
reply = 'gone';
await refresh();
assert.equal(ROLE, 'trusted', 'a page with no server behind it keeps its buttons');
assert.equal(fabEl.hidden, false);

// ---- Where the Explain button is allowed to land.
// At 390px the reading header is 65px tall and the first line of a note sits
// at y=87, which put the button at 35 -- inside the header, over the back
// button, with its own onclick eating the tap meant for it.
rect.top = 87; askEl.style.top = null;
onSelectionChange();
assert.equal(askEl.style.top, '70px', 'the button may not land inside the header');
rect.top = 400;
onSelectionChange();
assert.equal(askEl.style.top, '348px', 'and sits at the selection everywhere else');
rect.top = 100;

// The Me tab is the profile: who you are, what your role permits, and what you
// have put in. It is also the only screen that explains the roles, so it is
// where a student finds out who opens the controls that told them no -- and
// somebody who already has them must not be told to go ask for them.
const mine = (role, extra) => answer(true, Object.assign({
  role, admin: false, invite: null, name: 'Asha', roll_no: '24U001',
  phone: '+919876543210', section: 'Section I',
  points: {score: 0, uploads: 0, recordings: 0, votes_received: 0},
  uploads: [], recordings: []}, extra || {}));
reply = mine('student');
location.hash = '#me'; route();
writes = [];
await renderMe();
assert.ok(says('Asha'), 'the profile is the person, not just a score');
assert.ok(says('24U001') && says('Section I') && says('+919876543210'),
          'roll number, section and number are all on it');
assert.ok(says('Student'), 'and what they are');
assert.ok(says('What trusted adds'), 'a student is told what trusted would add');
assert.ok(/admin/.test(LOCK_ADD), 'and that an admin is what makes one');
assert.ok(says('Points are a thank-you'), 'the contributions are still here');

reply = mine('trusted');
writes = [];
await renderMe();
assert.ok(says('Trusted member'), 'a trusted member is told what they have');
assert.ok(!says('What trusted adds'), 'and is not told to go ask for it');

// Your own name and number, and nothing else on this screen: there is no
// control here that could ask for a role or a status at all.
reply = mine('student');
writes = []; fetches = [];
await renderMe();
const box = {className: '', innerHTML: '', appendChild: () => {},
             querySelector: () => any};
qvalue = 'Asha Sharma';
editProfile(box, {name: 'Asha', phone: '+919876543210'});
const save = writes.filter(w => w[0] === 'onclick').pop()[1];
reply = answer(true, {name: 'Asha Sharma', phone: '+919876543211'});
fetches = [];
await save();
assert.equal(fetches[0][0], '/profile', 'saving is one request');
const sent = JSON.parse(fetches[0][1].body);
assert.deepStrictEqual(Object.keys(sent).sort(), ['name', 'phone'],
                       'a member may send their name and their number and nothing else');

// An admin's own rows are marked as an admin's, in the one class the admin
// screen uses for the same thing.
reply = mine('admin', {admin: true, invite: 'ABCD', pending: 2});
writes = [];
await renderMe();
assert.ok(says('2 people are waiting to be let in'),
          'the queue is on the tab, not only behind a second screen');
assert.ok(wrote(['className', 'row adm']), 'an admin-only row is inked');
assert.ok(wrote(['textContent', 'Admin']), 'and says so');

// The Me tab is painted twice on the way in -- once by the router, once when
// /data answers -- and its own /me lands between the two. Both halves used to
// append, so an admin was shown two of every row and two profile cards.
location.hash = '#me';
reply = mine('admin', {admin: true, invite: 'ABCD'});
route();          // paint one, waiting on /me
render();         // paint two replaces it, and is also waiting on /me
writes = [];
await new Promise(setImmediate);
await new Promise(setImmediate);
const rows = writes.filter(w => JSON.stringify(w) === '["className","row adm"]').length;
assert.equal(rows, 2, 'two inked rows from the paint that survived, not four from both');


// ---- THE CLASS BOARD, on Campus. The server ranks; this draws what it sent.
const person = (name, rank, score, extra) => Object.assign(
  {id: name, name, role: 'trusted', rank, score,
   uploads: 0, recordings: 0, votes_received: 0, you: false}, extra || {});
const board = (all, week, you) => answer(true, {
  all: {top: all, you: you === undefined ? null : you},
  week: {top: week || [], you: you === undefined ? null : you}});

// Day one: nobody has added anything. A blank list reads as a broken screen,
// and a list of 110 people on nought is not a ranking. Landed on with the
// answer already waiting, so nothing from the screen before it settles behind.
BOARD = null; boardWindow = 'all';
reply = board([], [], person('You', 4, 0, {you: true}));
location.hash = '#campus'; route();
writes = [];
await renderCampus();
assert.ok(says('Nobody has added anything yet'), 'an empty board says so');
assert.ok(says('Points are recognition only'),
          'and says, where it would be assumed otherwise, that nothing is locked');

// A board with people on it: the place, the name, the role, the breakdown, the
// score -- and ties sharing a place, which is what the server sent.
BOARD = null;
reply = board(
  [person('Asha', 1, 30, {recordings: 3}), person('Bilal', 1, 30, {uploads: 6}),
   person('Chetan', 3, 5, {uploads: 1, role: 'student'})],
  [person('Asha', 1, 10, {recordings: 1})],
  person('You', 41, 1, {you: true, votes_received: 1}));
writes = [];
await renderCampus();
assert.ok(says('Asha') && says('Bilal') && says('Chetan'), 'the board is the class');
assert.ok(says('#1') && says('#3'), 'a tie shares a place and the next one is third');
assert.ok(says('Trusted member') && says('Student'), 'each row says what they are');
assert.ok(says('3 recordings'), 'and the breakdown the score is made of');
assert.ok(says('You (you)'), 'the viewer is on it from 41st, not only from the top');
assert.ok(wrote(['className', 'rank you']), 'and their own row is marked as theirs');
assert.ok(says('Your place'), 'pinned under a break rather than passed off as 21st');
assert.ok(says('Regular'), 'a level is a word somebody earned, on the row that earned it');

// Somebody already in the top is not printed twice.
BOARD = null;
reply = board([person('Asha', 1, 30, {you: true})], [], person('Asha', 1, 30, {you: true}));
writes = [];
await renderCampus();
assert.ok(!says('Your place'), 'a viewer in the top twenty is not also pinned below it');

// This week is its own board, off the same answer: the toggle costs no request.
BOARD = null;
reply = board([person('Asha', 1, 300)], [person('Dev', 1, 10)]);
writes = [];
await renderCampus();
boardWindow = 'week';
fetches = []; writes = [];
await renderCampus();
assert.ok(says('Dev'), 'this week is a different ranking');
assert.ok(!says('Asha'), 'and does not carry the all-time leader into it');
assert.equal(fetches.length, 0, 'both windows ride on one request');

// An empty week under a busy all-time board is its own sentence.
BOARD = null;
reply = board([person('Asha', 1, 300)], []);
writes = [];
await renderCampus();
assert.ok(says('Nobody has added anything this week'), 'an empty week says which window');
boardWindow = 'all';

// Levels are earned, and only from Regular up: a wall of words on every row is
// not recognition.
assert.deepStrictEqual(levelOf(0), [0, 'New here', false]);
assert.deepStrictEqual(levelOf(1), [1, 'Contributor', false]);
assert.deepStrictEqual(levelOf(25), [25, 'Regular', true]);
assert.equal(levelOf(99)[1], 'Regular');
assert.deepStrictEqual(levelOf(100), [100, 'Mainstay', true]);
assert.ok(!levelOf(24)[2], 'the first two rungs are not worn on the board');
assert.deepStrictEqual(nextLevel(0), [1, 'Contributor', false]);
assert.deepStrictEqual(nextLevel(26), [100, 'Mainstay', true]);
assert.equal(nextLevel(100), undefined, 'the top of the ladder has nothing above it');

// No server behind the page at all: say so rather than sit on 'reading...'.
BOARD = null;
reply = 'gone';
writes = [];
await renderCampus();
assert.ok(says('The class board needs the server'), 'a static export says why it is empty');
BOARD = null; reply = null;

// ---- The rules, on Me. Nobody should have to guess why they have the number
// they have, so every weight and what it earned this person is printed.
reply = mine('trusted', {points: {score: 26, uploads: 1, recordings: 2, votes_received: 1}});
location.hash = '#me'; route();
writes = [];
await renderMe();
assert.ok(says('26 points'), 'the score is the headline');
assert.ok(says('How points work'), 'and the rules are under it');
assert.ok(says('5 points each \u00b7 1 \u00d7 5 = 5 points'), 'an upload is worth five');
assert.ok(says('10 points each \u00b7 2 \u00d7 10 = 20 points'), 'a recording ten');
assert.ok(says('1 point each \u00b7 1 \u00d7 1 = 1 point'), 'a vote one');
assert.ok(says('Regular'), 'the level falls out of the score, nobody awards it');
assert.ok(says('Mainstay at 100 points'), 'and the next one is named');
assert.ok(says('Points are a thank-you, not a key'), 'points are still not a key');

reply = mine('student');
writes = [];
await renderMe();
assert.ok(says('nothing from this yet'),
          'a rule with nothing behind it still says what it is worth');
assert.ok(says('New here') && says('Contributor at 1 point'),
          'and the first rung is one point away, not hidden');


// ---- THE NOTICE BOARD. -------------------------------------------------
// A body is markdown typed by a person and rendered as markup on a hundred and
// ten phones, so it is the one thing on this page that could carry a tag onto
// somebody else's screen. mdSafe is what stops it, and this is the whole of
// what it promises.
assert.ok(!mdSafe('<script>alert(1)</script>').includes('<script'),
          'a script tag in a body must never survive into the page');
assert.ok(!mdSafe('<img src=x onerror=alert(1)>').includes('<img'),
          'nor any other tag: every < is escaped before marked sees it');
assert.ok(!mdSafe('<div onclick="x">hi</div>').includes('<div'),
          'and with no tag there is nowhere to hang a handler');
assert.ok(mdSafe('<b>hi</b>').includes('&lt;b&gt;') ||
          mdSafe('<b>hi</b>').includes('&lt;b>'),
          'the tag comes out as the text somebody typed, not as markup');
assert.ok(mdSafe('**bold** and a list').includes('**bold**'),
          'the markdown itself still goes through');
// The links marked builds out of safe-looking markdown are the other half:
// [tap](javascript:...) never reaches marked as a '<'.
const realParse = marked.parse;
marked.parse = () => '<a href="javascript:alert(1)">tap</a><img src="data:text/html,x">';
assert.ok(!mdSafe('x').includes('javascript:'), 'a javascript: link is disarmed');
assert.ok(!mdSafe('x').includes('data:'), 'and so is a data: source');
assert.ok(mdSafe('x').includes('<a'), 'the link is left in place, just dead');
marked.parse = () => '<a href="https://notes.example/x">ok</a>';
assert.ok(mdSafe('x').includes('https://notes.example/x'), 'a real link survives');
marked.parse = realParse;

const notice = (id, title, extra) => Object.assign(
  {id, title, body: 'the body', pinned: false, by: 'Asha', at: 100,
   edited: null, mine: false, unread: false, deleted: false}, extra || {});
const emptyBoard = {all: {top: [], you: null}, week: {top: [], you: null}};

// Campus: every live notice, newest first as the server sent them, with the
// flags that say why one is above the others.
ANN = [notice('a1', 'Lab moved to Friday', {pinned: true, unread: true}),
       notice('a2', 'Old news')];
NOW = 100; ROLE = 'student'; BOARD = emptyBoard; live = true;
location.hash = '#campus'; route();
writes = []; fetches = [];
await renderCampus();
assert.ok(says('Announcements'), 'Campus leads with the notice board');
assert.ok(says('Lab moved to Friday') && says('Old news'), 'both notices are on it');
assert.ok(says('Pinned · New'), 'an unread pinned notice says both, in one line');
assert.ok(!says('Post an announcement'), 'a student is offered no way to post');
assert.ok(!says('Clubs, events and announcements will live here'),
          'the placeholder copy cannot still promise what is now above it');

// Home: pinned or unread only, and never more than three. A wall of old
// notices at the top of Home is how people learn to scroll past the top of Home.
location.hash = '#home'; route();
writes = [];
renderHome();
assert.ok(says('Notices') && says('Lab moved to Friday'), 'Home surfaces what is new');
assert.ok(!says('Old news'), 'a notice you have read and nobody pinned is not Home');

ANN = ['b1', 'b2', 'b3', 'b4', 'b5'].map(i => notice(i, 'Notice ' + i, {unread: true}));
writes = [];
renderHome();
assert.ok(says('Notice b1') && says('Notice b3'), 'the first three are shown');
assert.ok(!says('Notice b4'), 'and the fourth is not');
assert.ok(says('and 2 more, under Campus.'), 'the rest are counted, not listed');

// Opening the board marks what is on it read, on the server, for this person.
// Not localStorage: the same person opens this on a phone and on a laptop.
BOARD = emptyBoard;
location.hash = '#campus'; route();
// After the route, not before it: painting the tab is itself an opening of the
// board, and the point here is what the paint under test asks for.
ANN = [notice('c1', 'Unread one', {unread: true}), notice('c2', 'Read already')];
READ_SENT.clear();
fetches = []; reply = answer(true, {ok: true});
await renderCampus();
const marks = fetches.filter(f => f[0] === '/read');
assert.equal(marks.length, 1, 'one request, for what is actually on screen');
assert.deepStrictEqual(JSON.parse(marks[0][1].body).ids, ['c1'],
                       'only the unread ones, and never one already read');
await new Promise(setImmediate);
assert.equal(ANN[0].unread, false, 'and Home stops calling it new straight after');
fetches = [];
await renderCampus();
assert.equal(fetches.filter(f => f[0] === '/read').length, 0,
             'a second paint of the same board is not a second request');

// Nothing up yet is a real state on day one, and it has to say what the space
// is for rather than showing an empty strip.
ANN = []; ROLE = 'student'; BOARD = emptyBoard;
writes = [];
await renderCampus();
assert.ok(says('Nothing on the notice board yet'), 'an empty board says so');
assert.ok(says('Your class admin puts them up'),
          'and says who fills it and where new ones show');
ROLE = 'admin';
writes = [];
await renderCampus();
assert.ok(says('Anything the whole section needs to know goes here'),
          'the admin reading the same empty screen is told what to do with it');
assert.ok(says('Post an announcement'), 'and is given the way in');
assert.ok(wrote(['className', 'row adm']),
          'inked, like every other control only an admin may press');

// Writing one takes the tab over, and costs no request until it is posted --
// and it is a URL, so the back gesture climbs out of it instead of leaving the
// app with what was typed, and the Campus tab button lands on the board again.
ROLE = 'admin'; ANN = [];
writes = []; fetches = [];
location.hash = '#campus/new'; route();
assert.equal(view.compose, 'new', 'the composer is a level, not a variable');
assert.ok(wrote(['className', 'compose']), 'the composer replaces the board');
assert.ok(!says('Who has contributed'), 'one thing at a time on a phone');
assert.equal(fetches.length, 0, 'and nothing is asked for until it is posted');
assert.ok(says('New notice'), 'the header says which level you are on');
assert.ok(says('\u2039 Campus'), 'and the back button says where it climbs to');

writes = [];
location.hash = '#campus'; route();
assert.ok(!wrote(['className', 'compose']),
          'walking back out of it lands on the board, not on the form again');

ANN = [notice('e1', 'Mine', {mine: true, body: 'the body'})];
writes = [];
location.hash = '#campus/e1'; route();
assert.ok(says('Edit notice'), 'editing one is a URL too');
assert.ok(wrote(['value', 'Mine']), 'and it opens on the notice that URL names');

// A student who types the URL gets the board, the same answer /announce gives.
ROLE = 'student';
writes = [];
location.hash = '#campus/e1'; route();
assert.ok(!wrote(['className', 'compose']), 'the composer is admin-only here too');
location.hash = '#campus'; route();
ROLE = null; ANN = [];

})().catch(e => { console.error(e); process.exit(1); });
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
    # An open note is the same problem: every generated note ends in a
    # <details><summary>Full transcript</summary>, and at 118px the FAB sat on
    # the bottom 16px of it and took the taps meant for it.
    assert "article{padding:22px 18px 142px" in notes.PAGE
    fab = re.search(r"#fab\{(.*?)\}", notes.PAGE, re.S).group(1)
    assert "bottom:calc(76px + env(safe-area-inset-bottom))" in fab and "height:58px" in fab, \
        "if the FAB moves, #nav's and article's padding have to move with it"


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


def test_a_file_that_vanishes_does_not_take_the_library_with_it(tmp_path):
    """Listing a folder and stat-ing what came back are two moments, and an
    upload lands between them: do_upload writes `<name>.part` and renames it
    into place. A dangling symlink is that race held still. Letting it raise
    dropped the whole /data response, which the phone reads as "no server".
    """
    folder = tmp_path / "library" / "MC1101-Mathematics-1"
    (folder / "uploads").mkdir(parents=True)
    (folder / "uploads" / "real.pdf").write_bytes(b"%PDF-1.4")
    (folder / "uploads" / "gone.pdf").symlink_to(folder / "uploads" / "never.pdf")

    mc = next(s for s in notes.build_data(tmp_path / "library", tmp_path)
              if s["code"] == "MC1101")             # must not raise
    at = {u["name"]: u["at"] for u in mc["uploads"]}
    assert at["real.pdf"] > 0, "the file that is there keeps its arrival time"
    assert at["gone.pdf"] == 0, "and the one that went away is never new"


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


# Same bug, the other floating thing: #busy was fixed with pointer-events, but
# #ask has its own onclick and needs the taps it gets, so it is ranked under
# the sticky header instead. Whatever the clamp does, CSS is the backstop once
# a scroll carries the button up into the strip.
def test_the_explain_button_ranks_under_the_reading_header():
    top = int(re.search(r"\.top\{[^}]*z-index:(\d+)", notes.PAGE, re.S).group(1))
    ask = int(re.search(r"#ask\{[^}]*z-index:(\d+)", notes.PAGE, re.S).group(1))
    assert ask < top, (f"#ask at z-index {ask} paints over the header at {top} "
                       "and eats the tap meant for the back button")


def test_the_fab_does_not_take_the_taps_meant_for_explain():
    """#ask is clamped to innerWidth-130, so on a 390px screen its right edge
    lands at 348 -- inside the FAB's 316-374 band. The FAB is z-index 7 to
    #ask's 4, so the right third of Explain opened the Add sheet instead: the
    locked one, for the student the button was offered to. They are never both
    wanted, so the selection hides the FAB outright."""
    fab = int(re.search(r"#fab\{[^}]*z-index:(\d+)", notes.PAGE, re.S).group(1))
    ask = int(re.search(r"#ask\{[^}]*z-index:(\d+)", notes.PAGE, re.S).group(1))
    assert ask < fab, "if #ask is raised above the FAB this rule can go"
    assert "body:has(#ask.on) #fab{display:none}" in notes.PAGE, \
        "the two overlap and the FAB is on top"


def test_a_refused_upload_is_not_told_to_try_again():
    """403 is the gate's, and its words are a route name and a role. There is
    one sentence for this in the whole page, and a retry cannot work."""
    up = re.search(r"function upload\(blob, name\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "if (xhr.status === 403)" in up, "a no is not a failed upload"
    assert "fail(LOCK_ADD, false)" in up, "and it says the same thing every other lock does"
    assert "retry ? ' — tap an option to try again' : ''" in up


# -------------------------------------------------- what a student is shown


def test_every_way_in_is_locked_rather_than_missing():
    """A student may not upload, record or Explain. Hiding those controls was
    the old answer and it taught nobody anything: the app simply looked like an
    app that does not do that. Each one stays, marked, with the reason."""
    for wiring in (
        # The + button is shown and marked, not removed.
        "fab.hidden = ROLE === null;",
        "fab.className = mayAdd() ? '' : 'locked';",
        "document.getElementById('lock').hidden = mayAdd();",
        "el.className = mayAdd() ? 'opt' : 'opt locked';",
        # Each option refuses to start rather than 403ing halfway through.
        "if (!mayAdd()) return;",
        # Explain is offered, and answers in the panel it opens.
        "if (!mayAdd()) { out.textContent = LOCK_EXPLAIN; return; }",
        "else ask.classList.add('locked');",
    ):
        assert wiring in notes.PAGE, f"the lock is not wired: {wiring}"
    assert notes.PAGE.count("if (!mayAdd()) return;") == 4, \
        "all four options in the add sheet, not the two that were easy"


@pytest.mark.parametrize("const", ["LOCK_ADD", "LOCK_EXPLAIN"])
def test_the_lock_copy_says_what_would_open_it(const):
    """"You cannot do this" is not an answer a first-year can act on. Both
    sentences have to name the role and the person who grants it."""
    said = re.search(rf"const {const} = (.*?);\n", notes.PAGE, re.S).group(1)
    assert "trusted" in said, f"{const} does not say what role this needs"
    assert "admin" in said, f"{const} does not say who makes one"


def test_a_locked_control_never_costs_a_request():
    """The point of showing the lock is that the phone already knows the
    answer. Both handlers have to return before the fetch, not after it."""
    ask = re.search(r"ask\.onclick = async \(\) => \{.*?\n\};", notes.PAGE, re.S).group(0)
    assert ask.index("if (!mayAdd())") < ask.index("fetch('/explain'")
    rev = re.search(r"getElementById\('opt-revise'\)\.onclick = async \(\) => \{.*?\n\};",
                    notes.PAGE, re.S).group(0)
    assert rev.index("if (!mayAdd())") < rev.index("fetch('/revise'")


# ------------------------------------------------ admin controls, and the ink


def test_admin_only_controls_are_marked_the_same_way_in_both_files():
    """Privileged actions appear on two screens -- the Me tab and the admin
    page -- and they have to be recognisable as the same thing on both. One
    pair of tokens, one class name, or it is two treatments that will drift."""
    for page in (notes.PAGE, notes.GATE_PAGE):
        assert "--admin:" in page and "--admin-fg:" in page, "the ink is not defined"
    assert "function inked(row)" in notes.PAGE, "the app has no single place for it"
    assert "row.className = 'row adm';" in notes.PAGE
    assert notes.PAGE.count("inked(") >= 4, "every admin row goes through it"
    # Ink, not a bolted-on red: red is the error colour and already means
    # something else on both screens.
    admin_light = re.search(r"--admin:\s*(#[0-9a-fA-F]{6})", notes.PAGE).group(1)
    assert admin_light != "#e5484d"


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("fg,bg", [("--admin-fg", "--admin"), ("--admin", "--bg"),
                                   ("--mut", "--surface"), ("--accent", "--bg"),
                                   ("--mut", "--bg")])
def test_the_ink_reads_in_both_themes(mode, fg, bg):
    """An inked slab, a muted lock label on the surface it sits on, and the
    accent it has to be told apart from. PAGE never had a contrast test at all;
    the gate's helper is the same arithmetic, so it is borrowed rather than
    written twice."""
    from test_gate_page import contrast

    css = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    roots = re.findall(r":root\{([^}]*)\}", css)
    light, dark = ({k: v for k, v in
                    re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", r)}
                   for r in roots)
    tokens = light if mode == "light" else {**light, **dark}
    ratio = contrast(tokens[fg], tokens[bg])
    assert ratio >= 4.5, f"{fg} on {bg} in {mode} is only {ratio:.2f}:1"


def test_the_profile_edits_two_fields_and_cannot_reach_for_a_third():
    """Name and phone are yours. Role and status are not, and the screen that
    could ask for them is the one place this is easy to get wrong -- so there
    is no control here that names either."""
    form = re.search(r"function editProfile\(box, d\) \{.*?\n\}\n", notes.PAGE, re.S).group(0)
    assert "body: JSON.stringify({name: name.value, phone: phone.value})" in form
    for forbidden in ("role", "status", "trusted", "admin"):
        assert forbidden not in form, f"the profile form reaches for {forbidden}"


def test_the_board_is_wired_to_the_server_and_to_the_shell():
    """The node stub swallows an onclick assigned to a proxy, so the checks
    above drive the board by name. This is the other half: that the window
    toggle is attached, that the board is fetched from the one route that
    serves it, and that a vote -- which re-ranks it -- drops the cached copy."""
    for wiring in ("await fetch('/standings')",
                   "b.onclick = () => { boardWindow = key; render(); };",
                   "else if (view.tab === 'campus') renderCampus();"):
        assert wiring in notes.PAGE, f"the board is not wired: {wiring}"
    refresh = re.search(r"async function refresh\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "BOARD = null;" in refresh, "a vote re-ranks the board, so it must be re-read"


def test_the_admin_way_in_is_wired_and_gated_on_the_server_too():
    """Hiding a button is a courtesy. /admin is in ROLE_REQUIRED, so the gate
    refuses a student's curl exactly as it refuses a student's browser."""
    assert notes.ROLE_REQUIRED["/admin"] == "admin"
    assert "if (ROLE !== 'admin') return;" in notes.PAGE, "the Home row is role-gated"
    assert "adminBlock();" in notes.PAGE, "and Home actually draws it"
    # Ink, not a fourth colour: the same helper the Me tab's admin rows use.
    home = re.search(r"function adminBlock\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "inked(" in home, "an admin-only control has to look like one"


def test_the_board_never_becomes_a_lock():
    """Read alongside test_points_never_gate_anything_on_the_page: that one bans
    the branch, this one keeps the sentence that promises there is not one."""
    assert "Points are a thank-you, not a key" in notes.PAGE
    assert "Points are recognition only" in notes.PAGE
    assert "/standings" not in notes.ROLE_REQUIRED, "seeing who contributed is not a privilege"


def test_the_notice_board_rides_on_the_data_the_page_already_fetches():
    """Home and Campus are both opened between classes on mobile data. The
    board arrives on /data with the timetable and the queue, so neither tab
    spends a round trip drawing one -- and only the two writes talk to the
    server at all."""
    assert "ANN = d.announcements || [];" in notes.PAGE
    assert "NOW = d.now || NOW;" in notes.PAGE
    assert notes.PAGE.count("fetch('/announce'") == 1, "one write path, in saveAnn"
    assert notes.PAGE.count("fetch('/read'") == 1, "one read mark, in markRead"
    assert "fetch('/announcements'" not in notes.PAGE, "the board is not its own request"
    # Home must stay the screen that waits on nothing.
    home = re.search(r"\nfunction noticeBlock\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "await" not in home and "fetch" not in home
    assert "show.slice(0, 3)" in home, "Home shows three notices, not a wall of them"


def test_posting_is_admin_only_on_the_server_too():
    """Hiding the composer is a courtesy. The lock is the gate, which refuses
    curl exactly as it refuses a student's browser -- and under that, a policy
    that refuses a stolen cookie too."""
    assert notes.ROLE_REQUIRED["/announce"] == "admin"
    assert "/read" not in notes.ROLE_REQUIRED, "what you have read is not a privilege"
    campus = re.search(r"function renderAnnouncements\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "if (ROLE === 'admin')" in campus, "the composer is offered on the role"
    assert "a.mine && ROLE === 'admin'" in notes.PAGE, \
        "and edit/delete only on your own, which is what the policy allows"


def test_a_hidden_notice_is_still_readable():
    """A hidden notice is the one card whose small print has to be read -- the
    word "Hidden" and the button that puts it back. An alpha on the card dims
    those with everything else, and no token test can see it, because the token
    is fine and the compositing is what fails. So the rule is that the state is
    said in colour, on the one span that says it, and never in opacity."""
    css = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    gone = re.search(r"\.ann\.gone\{([^}]*)\}", css).group(1)
    assert "opacity" not in gone, \
        "dimming the card dims the one word that explains the state"
    assert ".ann.gone .flag{color:var(--mut)}" in css, \
        "so 'Hidden' has to be muted by a token that passes on its own"


def test_saving_a_notice_disarms_the_button_that_started_it():
    """The busy strip is two hundred pixels below the thumb, so the control
    being tapped has to say for itself that it is working. A second tap posts
    the same notice to the whole section twice, and there is no delete policy
    to take one back with."""
    save = re.search(r"async function saveAnn\(.*?\n\}", notes.PAGE, re.S).group(0)
    assert "async function saveAnn(payload, err, btn) {" in save
    assert "if (btn) btn.disabled = true;" in save, "armed all through the request"
    assert "if (btn) btn.disabled = false;" in save, "and given back on a failure"
    # Every caller, not just the composer's: Delete and "Put it back" are the
    # same one-row write and had the same gap.
    for caller in ("saveAnn({id: a.id, deleted: !a.deleted}, null, del);",
                   "err, save);"):
        assert caller in notes.PAGE, f"a saveAnn caller passes no button: {caller}"
    assert notes.PAGE.count("saveAnn(") == 3, \
        "one definition and two callers; a third would need the same button"


def test_the_composer_is_a_url_like_every_other_level():
    """Without one the system back gesture threw away what was typed, and the
    Campus tab button kept landing back on the form. The timetable editor is
    the pattern; this is the same shape, so back, the header button and Cancel
    are all the one mechanism."""
    assert "post.onclick = () => go('campus', 'new');" in notes.PAGE
    assert "edit.onclick = () => go('campus', a.id);" in notes.PAGE
    assert "const compose = tab === 'campus' ? parts[1] || null : null;" in notes.PAGE
    assert "composing" not in notes.PAGE, "no variable may outlive the URL"
    assert "lback.hidden = !s && !view.edit && !view.att && !view.compose;" in notes.PAGE
    compose = re.search(r"function renderCompose\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "history.back()" in compose, "Cancel is a step back, like every other one"


def test_a_notice_body_is_never_written_into_the_page_as_it_was_typed():
    """The one field on this page a person types and the page renders as
    markup. Titles go in with textContent like every other name; bodies go
    through mdSafe, which is the only caller of marked outside mdInto."""
    assert "el.querySelector('h3').textContent = a.title;" in notes.PAGE
    assert "el.querySelector('.md').innerHTML = mdSafe(a.body);" in notes.PAGE
    assert notes.PAGE.count("marked.parse(") == 2, \
        "marked has exactly two callers: mdInto for notes, mdSafe for bodies"
    safe = re.search(r"function mdSafe\(src\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert r"replace(/</g, '&lt;')" in safe, "every < is escaped before marked sees it"

