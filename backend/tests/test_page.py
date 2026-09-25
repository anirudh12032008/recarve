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


def test_an_empty_library_exports_every_subject(tmp_path):
    """Nothing to show is not an error: this page is where the first thing goes.

    It also has to name every one of them. You cannot file a chemistry recording
    under a subject the app never told you was there.
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


def test_data_reads_the_library_only_when_it_changes(tmp_path, monkeypatch):
    """/data reuses the library until a file moves, and never shares a student's votes."""
    folder = tmp_path / "library" / "MC1101-Mathematics-1"
    (folder / "lectures").mkdir(parents=True)
    (folder / "uploads").mkdir()
    (folder / "lectures" / "week1.md").write_text("## Summary\nlimits\n")
    (folder / "uploads" / "a.pdf").write_bytes(b"x")
    reads = []
    real = notes.build_data
    monkeypatch.setattr(notes, "build_data", lambda *a: reads.append(1) or real(*a))
    mc = lambda data: next(s for s in data if s["code"] == "MC1101")

    first = notes.library_data(tmp_path / "library", tmp_path)
    assert notes.library_data(tmp_path / "library", tmp_path) == first
    assert len(reads) == 1, "an unchanged library is not read again"

    # apply_meta writes one student's view into what it is handed.
    notes.apply_meta(first, {("MC1101", "a.pdf"): {"votes": 3, "voted": True}},
                     {("MC1101", "week1"): "Asha"})
    again = mc(notes.library_data(tmp_path / "library", tmp_path))
    assert "voted" not in again["uploads"][0], "one student's vote reached the next"
    assert "by" not in again["notes"][0]

    (folder / "lectures" / "week1.md").write_text("## Summary\nlimits, continuity\n")
    assert "continuity" in mc(notes.library_data(tmp_path / "library", tmp_path))["notes"][0]["md"]
    (folder / "uploads" / "b.pdf").write_bytes(b"y")
    assert [u["name"] for u in mc(notes.library_data(tmp_path / "library", tmp_path))["uploads"]] \
        == ["a.pdf", "b.pdf"], "a new upload shows up"
    (folder / "uploads" / "a.pdf").unlink()
    assert [u["name"] for u in mc(notes.library_data(tmp_path / "library", tmp_path))["uploads"]] \
        == ["b.pdf"], "a removed one goes"
    assert len(reads) == 4


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

# The role ladder and the one question the page ever asks of it, lifted out of
# PAGE rather than written again here. Every harness below that pulls a single
# function out of the page needs these two lines beside it, because almost
# every control on the page decides whether to exist by calling atLeast() --
# and a stand-in written here would agree with whatever the test expected
# instead of with what ships.
LADDER = "\n".join(re.search(p, notes.PAGE).group(0) for p in (
    r"const ROLES = \[[^\]]*\];", r"const atLeast = [^\n]*"))

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
const says = word => writes.some(w => w[0] === 'textContent' && String(w[1]).includes(word));
// #ask is the one element whose class a check has to tell apart from every
// other 'on', so it is a real object rather than the proxy.
const askCls = [];
const askEl = {style: {}, classList: {
  add: c => askCls.push('+' + c), remove: c => askCls.push('-' + c),
  contains: () => false,
}};
// document.body is real, because the drawer asks it whether it is open --
// 'drawered' is the whole of that state and the discard proxy says yes to
// every question. add/remove still record into `writes` the way they did when
// the proxy swallowed them, so the checks that read body.reading back out of
// there are unchanged.
const bodyClasses = new Set();
const bodyNode = {
  contains: () => true,
  appendChild: c => c,
  classList: {
    add: c => { writes.push(['()', c]); bodyClasses.add(c); },
    remove: c => { writes.push(['()', c]); bodyClasses.delete(c); },
    toggle: (c, on) => { writes.push(['()', c, on]);
                         if (on) bodyClasses.add(c); else bodyClasses.delete(c); },
    contains: c => bodyClasses.has(c),
  },
};
const document = new Proxy(function () {}, {
  get: (t, k) => (
    k === 'getElementById' ? (id => els[id] || any)
    : k === 'body' ? bodyNode
    : k === 'activeElement' ? activeEl
    : k === 'visibilityState' ? visibility
    : k === 'getSelection' ? getSelection
    // The one listener whose decision is worth checking: what a highlight does
    // is a rule (only inside a note, only a real phrase, only for a member who
    // may spend), and the proxy would swallow it.
    : k === 'addEventListener'
      ? ((ev, fn) => { if (ev === 'selectionchange') onSelectionChange = fn;
                       if (ev === 'visibilitychange') onVisibility = fn; })
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
// It is also the one button whose job changes with the screen -- the composer
// on Community, the Add sheet everywhere else -- so its handler and the label
// it says about itself are read back rather than thrown away.
const fabAttrs = {};
const fabEl = {hidden: null, className: null, onclick: null,
               setAttribute: (k, v) => { fabAttrs[k] = v; }};
// The banner in the add sheet that says who adding is for. Real, because
// whether it is showing is the other half of what a locked + button means.
const lockEl = {hidden: null};
// Real, the same reason fabEl is: paintSaveBtn writes to it directly (it lives
// in the dock, outside #nav, so a redraw never touches it) and the test needs
// to read those writes back, not just see them thrown into the discard proxy.
const saveAttrs = {};
const saveEl = {textContent: null, disabled: false,
  setAttribute: (k, v) => { saveAttrs[k] = v; }};
// The avatar and its menu are real for the same reason #fab is: whether the
// menu is open, what is in it and what it says about itself are the whole of
// what a menu has to get right, and the discard proxy answers 'yes' to every
// question about all three.
const avatarAttrs = {};
let avatarFocused = 0;
// Named apart from the script's own consts, and handed to it through els --
// the page reaches for them by id, so these ARE its avatarEl and menuEl.
// It holds something drawn -- the glyph it ships with, or the face the name
// from /me makes -- so it has the two members that swap one for the other.
const avatarKids = [];
const avatarNode = {onclick: null,
                    focus: () => { avatarFocused++; activeEl = avatarNode; },
                    contains: () => false,
                    setAttribute: (k, v) => { avatarAttrs[k] = v; },
                    appendChild: c => { avatarKids.push(c); return c; }};
Object.defineProperty(avatarNode, 'innerHTML',
  {get: () => '', set: () => { avatarKids.length = 0; }});
// The drawer and the strip of links inside it. Real for the same reason the
// avatar is: whether it is open, what is in it, which row is lit and where the
// focus goes when it shuts are the whole of what a drawer has to get right,
// and the discard proxy answers 'yes' to every question about all four.
// `children` is flat -- a group heading and then its rows -- which is what the
// page appends, so a check can read the order straight off it.
let activeEl = null;               // document.activeElement, so focus can be followed
const dnavNode = {children: [], appendChild: c => { dnavNode.children.push(c); return c; }};
Object.defineProperty(dnavNode, 'innerHTML',
  {get: () => '', set: () => { dnavNode.children.length = 0; }});
// The foot of the rail, which holds one thing: You. Counted separately from
// the map above it, because "You quietly moved back into the map" is exactly
// what would go unnoticed if both were appended to the same node.
const dyouNode = {children: [], appendChild: c => { dyouNode.children.push(c); return c; }};
Object.defineProperty(dyouNode, 'innerHTML',
  {get: () => '', set: () => { dyouNode.children.length = 0; }});
// Everything in the drawer that can take focus. The page builds its own rows
// out of document.createElement, which is the discard proxy and cannot be told
// apart from a heading here -- so what querySelector/querySelectorAll hand back
// are three stand-ins that say when they are focused. What is IN the drawer is
// checked off dnavNode.children and the textContent writes; this half is only
// about where the focus goes.
const focused = [];
const focusable = name => {
  const el = {name, focus: () => { activeEl = el; focused.push(name); }};
  return el;
};
const drawerKeys = [focusable('first'), focusable('middle'), focusable('last')];
const drawerNode = {
  onkeydown: null,
  contains: () => drawerKeys.includes(activeEl),
  addEventListener: () => {},
  querySelector: () => drawerKeys[0],
  querySelectorAll: () => drawerKeys,
};
const els = {ask: askEl, panel: panelEl, fab: fabEl, lock: lockEl, save: saveEl,
             avatar: avatarNode, drawer: drawerNode, dnav: dnavNode,
             dyou: dyouNode};
// The app asks the platform whether the phone is dark, and listens in case it
// changes under it. Neither is a decision this page makes -- the media query
// is what actually swaps the palette -- so the stand-in answers 'light' and
// records nothing.
const matchMedia = () => ({matches: false, addEventListener: () => {}});
let selection = '';
let onSelectionChange = () => {};
let onVisibility = () => {}, visibility = 'visible';
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
// Nothing fires; the job poll's next delay is kept so its cadence can be read.
let jobsDelay = null;
const setInterval = () => 0, clearInterval = () => {};
const setTimeout = (fn, ms) => { if (fn === pollJobs) jobsDelay = ms; return 0; };
const clearTimeout = () => {};
// The page listens for 'scroll' on the window to take the + out of the way.
const addEventListener = () => {};
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

// The other tabs are levels of their own, none of them is a subject, and
// tapping one while reading has to put the note away -- with the class that
// hid the tab bar, and with the Explain button that was floating over the note.
// 'me' is in this list although it is not in the bar: it is still a tab to the
// router, which is what keeps every '#me' link anybody has ever sent working.
for (const t of ['home', 'campus', 'community', 'me']) {
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

// Past papers count too. A course can hold thirty of them and nothing this
// class made itself, and until the archive was counted here that course said
// "Nothing yet" over a full shelf.
PAPER_COUNTS = {[DATA[1].code]: 30};
assert.equal(counts(DATA[1]), '30 papers', 'papers alone are not "Nothing yet"');
PAPER_COUNTS = {[DATA[0].code]: 1};
assert.equal(counts(DATA[0]), '1 lecture \\u00b7 revision sheet \\u00b7 1 paper',
             'one paper is singular, and it comes after what the class made');
// A course with no papers must print nothing rather than "0 papers", which is
// what every lab would otherwise wear.
PAPER_COUNTS = {};
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

// ---- Spaced repetition. The store above is still installed. ----
// A question you got wrong must come back sooner and more often than one you
// got right, so the order a run is dealt in is the whole feature.
localStorage.removeItem = k => store.delete(k);
store.clear();
qz = null;
qOpen(mc, null);
assert.deepStrictEqual(qz.order, [0, 1, 2], 'nothing seen yet: the notes\u2019 own order');
qReveal(); qMark(false);                        // q1 wrong -- due now
qReveal(); qMark(true);                         // q2 right -- waits a day
assert.ok(store.get('recarve.sr'), 'the schedule is kept on the phone, not the server');

qz = null;
store.delete('recarve.quiz.MC1101');            // a fresh run, not a resume
qOpen(mc, null);
assert.equal(qz.order[0], 0, 'the one you missed comes back first');
assert.equal(qz.order[2], 1, 'the one you got right sinks below the unseen');

// Wrong twice beats wrong once. Both are due, so only how often they have gone
// wrong can separate them -- and it must beat the order they are listed in.
const mcq = quizItems(mc, null);
store.clear();
srMark(mcq[2], false); srMark(mcq[2], false);   // the last question, missed twice
srMark(mcq[0], false);                          // the first, missed once
qz = null; store.delete('recarve.quiz.MC1101');
qOpen(mc, null);
assert.deepStrictEqual(qz.order.slice(0, 2), [2, 0],
  'the question missed most often leads, ahead of one missed once');

// Honest counts: what is here, what has been answered, what keeps going wrong.
// No mastery percentage anywhere -- self-marked recall cannot measure one.
store.clear();
srMark(mcq[0], false); srMark(mcq[1], true);
const st = srStats(mcq);
assert.deepStrictEqual([st.total, st.seen, st.due, st.shaky], [3, 2, 1, 1],
  'seen means answered, due means owed today, shaky means still going wrong');
assert.equal(srSays(st), '3 questions \u00b7 2 seen \u00b7 1 due \u00b7 1 still shaky');
assert.ok(!/%/.test(srSays(st)), 'no invented mastery percentage');

// One run over the whole library, for the week before an exam.
assert.equal(allItems().length, 3, 'everything means every subject');
qz = null;
qOpenAll();
assert.equal(qz.items.length, 3, 'the whole-library run opens');
assert.equal(qz.key, 'recarve.quiz.*', 'and keeps its own saved place');

// The exam on the institute's calendar is what that row says it is for.
ATT = {today: '2026-01-01', upcoming: [
  {date: '2026-01-08', kind: 'exam', title: 'Mid-terms'}]};
assert.equal(examSoon(), 'Mid-terms in 7 days');
ATT.upcoming[0].date = '2026-04-01';
assert.equal(examSoon(), '', 'an exam months away is not a countdown');
ATT.upcoming = [{date: '2026-01-03', kind: 'holiday', title: 'Pongal'}];
assert.equal(examSoon(), '', 'a holiday is not an exam');
ATT = null;

// With no storage at all -- the static export in private mode -- practice must
// still deal a run rather than throw.
delete globalThis.localStorage;
qz = null;
qOpen(mc, null);
assert.deepStrictEqual(qz.order, [0, 1, 2], 'no storage: practice degrades to plain order');
qReveal(); qMark(false);
assert.equal(qz.i, 1, 'and marking still moves on');
assert.equal(srStats(mcq).seen, 0, 'with nothing remembered, nothing is claimed');
globalThis.localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
};

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

// ---- Removing a file, admin only, and never through a control that cannot
// work: a static export's file has no id, and a revision sheet has no row.
ROLE = null;
writes = [];
fileRow({name: 'slides.pdf', path: 'p', id: 'm1', votes: 3, voted: false}, mc);
assert.ok(!wrote(['className', 'row-del']), 'nobody but admin sees Remove');
writes = [];
noteRow({title: 'week1', kind: 'lecture', md: ''}, mc);
assert.ok(!wrote(['className', 'row-del']));

ROLE = 'trusted';
writes = [];
fileRow({name: 'slides.pdf', path: 'p', id: 'm1', votes: 3, voted: false}, mc);
assert.ok(!wrote(['className', 'row-del']), 'trusted may add; only admin may remove');

ROLE = 'admin';
writes = [];
fileRow({name: 'slides.pdf', path: 'p', id: 'm1', votes: 3, voted: false}, mc);
assert.ok(wrote(['className', 'row-del']), 'admin sees Remove on a real file');

writes = [];
fileRow({name: 'orphan.pdf', path: 'p'}, mc);
assert.ok(!wrote(['className', 'row-del']),
          'no id, no row behind it, no remove control that could not work');

writes = [];
noteRow({title: 'week1', kind: 'lecture', md: ''}, mc);
assert.ok(wrote(['className', 'row-del']), 'admin sees Remove on a recorded lecture');

writes = [];
noteRow({title: 'Revision sheet', kind: 'revision', md: ''}, mc);
assert.ok(!wrote(['className', 'row-del']),
          'a revision sheet has no lectures row -- Revise remakes it instead');

ROLE = null;

// You are still a tab to the router even though the bar no longer carries one,
// so back climbs out of it rather than leaving the app.
location.hash = '#me';
route();
assert.equal(view.tab, 'me');
assert.equal(current, null, 'your own screens are not a note');
writes = [];
render();
assert.ok(wrote(['textContent', 'You']), 'and that header must name it');

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
assert.deepStrictEqual(chrome('#community'), [true, false, true, true, true, true, false],
                       'Community is a tab of its own and carries the same chrome');
assert.deepStrictEqual(chrome('#home/new'), [true, false, false, true, true, true, true],
                       'the notice composer is a level inside Home, with a way back up');
assert.deepStrictEqual(chrome('#me'), [true, false, true, true, true, false, false],
                       'the activity log is your profile\\'s');
assert.deepStrictEqual(chrome('#me/saved'), [true, false, false, true, true, true, false],
                       'what you saved is a level under it, and has no log of its own');

// ---- Links that were sent before anything moved. Every one of them still
// opens the screen it named, and says so in the bar by upgrading itself.
const moved = (from, to) => {
  location.hash = from; route();
  assert.equal(location.hash, to, from + ' must still land on ' + to);
};
// The editor is gone: a section's week is set once by an admin, so both old
// links land on the screen that week is now read from.
moved('#home/timetable', '#classes');
moved('#classes/timetable', '#classes');
moved('#home/attendance', '#classes/attendance');
moved('#campus/new', '#home/new');
moved('#campus/feed', '#community');
moved('#campus/confession', '#community');
// An announcement's id is a uuid, so a campus level that is not one of the
// three composer words is one of those and belongs to Home now.
moved('#campus/7a3f1e02-0000-0000-0000-000000000000',
      '#home/7a3f1e02-0000-0000-0000-000000000000');
// Campus's own three composers are not moved by that rule.
ROLE = 'admin';
location.hash = '#campus/club'; route();
assert.equal(location.hash, '#campus/club', 'Campus keeps the forms still its own');
assert.deepStrictEqual([view.tab, view.compose], ['campus', 'club']);
ROLE = null;
location.hash = '#classes'; route();

// ---- The drawer. Every screen in the app behind the avatar, and the only
// global way to the levels inside the four tabs, each of which used to be
// reachable from one block on one screen and nowhere else. Four sections in
// the map, which are the four the thumb bar names: a section with levels
// inside it opens to show them, a section that IS one screen is a link. You is
// the fifth thing in the rail and is not one of them -- it is whose account
// this is, and it stands at the foot behind your own face.
ROLE = 'student';
writes = []; dnavNode.children.length = 0; drawnFor = null; focused.length = 0;
activeEl = null;
avatarEl.onclick();
assert.ok(bodyClasses.has('drawered'), 'the avatar opens the drawer');
assert.equal(avatarAttrs['aria-expanded'], 'true', 'and says so to a screen reader');
// The sections, which are the app's own shape and not a filing of it: the
// four tabs, plus you at the foot. A rail that grouped Home under "Your week"
// and Subjects under "The library" taught a hierarchy the router does not have.
for (const g of ['Home', 'Classes', 'Campus', 'Community', 'You'])
  assert.ok(says(g), 'the drawer names the section: ' + g);
// The levels that had no global way in at all before this.
for (const row of ['Home', 'Your day', 'Catching up', 'Subjects',
                   'Past papers', 'Saved', 'Campus', 'Community', 'Your profile',
                   'Your contributions', 'About recarve'])
  assert.ok(says(row), 'the drawer holds: ' + row);
assert.ok(!says('Your timetable'), 'and not a screen that no longer exists');
assert.ok(!says('Class admin'), 'and nothing a student may not press');
// Four sections in the map, and no more: the levels hang INSIDE two of them
// rather than beside them. Counted, because a section that quietly stopped
// being built still says everything the OTHER sections say.
assert.equal(dnavNode.children.length, 4);
// And You is at the foot, on its own, rather than a fifth row in the map.
assert.equal(dyouNode.children.length, 1, 'You stands at the foot of the rail');
assert.equal(focused[0], 'first', 'opening puts the focus inside it');

// Where you are, in the drawer as well as in the bar. Standing three levels
// deep inside a subject lights Subjects: that is the row this screen is under.
// Everything the page does to one row, which is everything between that row's
// own label and the next row's -- the label, the href, the handler and the
// aria-current, in whatever order the painter writes them.
const lit = () => {
  const at = writes.findIndex(w => w[0] === 'textContent' && w[1] === 'Subjects');
  if (at < 0) return false;
  let end = writes.findIndex((w, i) => i > at && w[0] === 'textContent');
  if (end < 0) end = writes.length;
  return writes.slice(at + 1, end).some(
    w => w[0] === '()' && w[1] === 'aria-current' && w[2] === 'page');
};
writes = []; drawnFor = null;
location.hash = '#classes/MC1101/week1'; route();
assert.ok(lit(), 'reading a lecture is standing in Subjects');
writes = []; drawnFor = null;
location.hash = '#classes/day'; route();
assert.ok(!lit(), 'and your day is not -- it is its own row');

// Tab wraps inside it rather than stepping out onto the page behind. The
// arrows are gone with the menu: this is a <nav> of links, and a browser
// already walks links with Tab.
bodyClasses.add('drawered');
activeEl = drawerKeys[2]; focused.length = 0;
drawerEl.onkeydown({key: 'Tab', shiftKey: false, preventDefault: () => {}});
assert.equal(focused[0], 'first', 'Tab off the last row wraps to the first');
activeEl = drawerKeys[0]; focused.length = 0;
drawerEl.onkeydown({key: 'Tab', shiftKey: true, preventDefault: () => {}});
assert.equal(focused[0], 'last', 'and Shift+Tab back off the first wraps to the last');

// Escape is one of the three ways out -- the scrim and the Close button are
// the other two -- and it hands the focus back to what opened it rather than
// dropping it on the page behind.
avatarFocused = 0; activeEl = drawerKeys[0];
drawerEl.onkeydown({key: 'Escape', preventDefault: () => {}});
assert.ok(!bodyClasses.has('drawered'), 'Escape closes it');
assert.equal(avatarFocused, 1, 'and the focus goes back to the avatar');
assert.equal(avatarAttrs['aria-expanded'], 'false');

// The focus stays where it is when it was never inside -- a tap on the page
// behind must not yank the caret up to the header.
avatarEl.onclick();
activeEl = null; avatarFocused = 0;
avatarEl.onclick();
assert.ok(!bodyClasses.has('drawered'), 'the avatar closes it again');
assert.equal(avatarFocused, 0, 'and leaves a caret that was never in it alone');

// Walking to one of them closes it: render() is what every navigation ends in.
avatarEl.onclick();
assert.ok(bodyClasses.has('drawered'));
render();
assert.ok(!bodyClasses.has('drawered'), 'going anywhere at all puts the drawer away');

// ---- The lists inside a tab, each one addressable. Campus held three
// unrelated things behind one scroll -- what is on, who runs it, where it is --
// and the map was always last. Each is a level now, and the tab with none of
// them named is still all three, because that is where the thumb bar lands.
// The rail names these lists too, so a check that simply read every write
// could not tell the map from the screen. route() first, which paints both;
// then render() alone, which repaints only the screen -- paintDrawer is keyed
// and will not redraw a rail it has already drawn for this URL.
const screenAt = (hash) => {
  location.hash = hash; route();
  writes = [];
  render();
  return (word) => says(word);
};
let onScreen = screenAt('#campus');
for (const h of ['Coming up on campus', 'Clubs and societies', 'Finding your way'])
  assert.ok(onScreen(h), 'the whole tab still holds: ' + h);
onScreen = screenAt('#campus/clubs');
assert.equal(location.hash, '#campus/clubs',
             'a section is a URL of its own and is not rewritten to Home -- the '
             + 'rule that upgrades an old announcement id must not eat one');
assert.ok(onScreen('Clubs and societies'), 'and it draws the list it names');
assert.ok(!onScreen('Coming up on campus') && !onScreen('Finding your way'),
          'and only that list: a level shows one thing, or it is not a level');
onScreen = screenAt('#campus/places');
assert.ok(onScreen('Finding your way') && !onScreen('Clubs and societies'),
          'the map is reachable without scrolling past the other two');
// A composer word still wins where it always did: 'club' is a form, 'clubs'
// is a list, and the two sit at the same step of the hash. Asked of somebody
// who may actually open the form -- a student is bounced back to the tab, and
// a bounce would answer this question by accident rather than on purpose.
ROLE = 'admin';
location.hash = '#campus/club'; route();
assert.equal(location.hash, '#campus/club', 'the composer keeps its own URL');
ROLE = 'student'; drawnFor = null;
// Community, the same shape.
onScreen = screenAt('#community/doubts');
assert.ok(!onScreen('Who has contributed'),
          'one list on Community too, and not the board underneath it');

// The map lights the level you are standing in, not just the tab it is under.
const litRow = (label) => {
  const at = writes.findIndex(w => w[0] === 'textContent' && w[1] === label);
  if (at < 0) return false;
  let end = writes.findIndex((w, i) => i > at && w[0] === 'textContent');
  if (end < 0) end = writes.length;
  return writes.slice(at + 1, end).some(
    w => w[0] === '()' && w[1] === 'aria-current' && w[2] === 'page');
};
writes = []; drawnFor = null; location.hash = '#campus/places'; route();
assert.ok(litRow('Where things are'), 'the map row is lit when you are on the map');
assert.ok(!litRow('Everything on campus'), 'and the tab root is not lit as well');

// The one row only an admin may press is BUILT for an admin, not drawn and
// hidden from everybody else -- a hidden link is still a link in the markup.
ROLE = 'admin';
writes = []; dnavNode.children.length = 0; drawnFor = null;
avatarEl.onclick();
assert.ok(says('Class admin'), 'an admin is offered the way into the panel');
assert.ok(wrote(['className', 'tag']), 'marked with the ink every admin row carries');
assert.equal(dnavNode.children.length, 5, 'a fifth section, and it is one row');
avatarEl.onclick();
ROLE = null;

// + from here must leave the dropdown alone: 'me' matches no option, so the
// select blanks, and a recording uploaded with no subject is refused and lost.
location.hash = '#me'; route();
writes = [];
openSheet();
assert.ok(!writes.some(w => w[0] === 'value'), 'nothing may be filed under #me');
location.hash = '#';
route();
assert.equal(view.code, null);

// Campus has nothing in it yet and says what is coming rather than showing an
// empty screen that reads as a bug -- and the router is what has to reach it,
// so this goes through the hash rather than calling it by name.
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

// Empty is the honest first state, and it says whose job filling it is rather
// than offering the student an editor they no longer have.
TT = []; live = true;
home();
assert.ok(says('has not been set up yet'), 'an empty timetable says who fills it');
assert.ok(!says('Set up your timetable'), 'and never offers the student the grid');

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

// The editor is gone. '#classes/timetable' is not a level any more: it is
// rewritten to the subject list, and nothing on the way there offers a save.
TT = [{day: 3, period: 1, code: 'MC1101'}];
writes = []; location.hash = '#classes/timetable'; route();
assert.equal(location.hash, '#classes', 'the old editor URL lands on Subjects');
assert.ok(!says('Save timetable'), 'and there is no week to save anywhere on it');

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

// ---- How often it asks. Every two seconds from every open tab was most of
// what the server answered for an idle class (load test, 2026-09-25).
assert.equal(jobsDelay, 2000, 'while a lecture transcribes, progress stays live');
reply = answer(true, {jobs: []});
await pollJobs();
assert.equal(live, true, 'still talking to a server');
assert.equal(jobsDelay, 30000, 'with nothing running, a tab asks every thirty seconds');
reply = answer(true, {jobs: [{id: 2, name: 'CY1107-two.m4a', state: 'queued', detail: ''}]});
await pollJobs();
assert.equal(jobsDelay, 2000, 'a queued lecture (an upload just made) is watched closely again');
visibility = 'hidden'; jobsDelay = null;
await pollJobs();
assert.equal(jobsDelay, null, 'a hidden tab schedules nothing');
visibility = 'visible'; const before = fetches.length;
onVisibility();   // pollJobs fetches before its first await, so this is synchronous
assert.equal(fetches.length - before, 1, 'coming back into view asks at once instead of waiting');
assert.equal(live, true, 'and every step above was a real poll, not the waiting-on-/data path');
// Leave JOBS as the checks below found it: one lecture at 80%.
reply = moving('80% - 4 of 5 min');
await pollJobs();

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
// The score is a level down, not a fifth thing on this screen: the profile is
// who you are, and what you have put in is its own list.
assert.ok(!says('Points are a thank-you'), 'the score is not on the profile screen');
reply = mine('student');
// Draining after each route: the paint the router starts is itself waiting on
// /me, and it lands in `writes` under whatever check comes next otherwise.
location.hash = '#me/points'; route();
await new Promise(setImmediate); await new Promise(setImmediate);
writes = [];
await renderMe();
assert.ok(says('Points are a thank-you'), 'it is on the screen the avatar names');
location.hash = '#me'; route();
await new Promise(setImmediate); await new Promise(setImmediate);

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


// ---- THE CLASS BOARD, on Community. The server ranks; this draws what it sent.
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
location.hash = '#community'; route();
writes = [];
await renderCommunity();
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
await renderCommunity();
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
await renderCommunity();
assert.ok(!says('Your place'), 'a viewer in the top twenty is not also pinned below it');

// This week is its own board, off the same answer: the toggle costs no request.
BOARD = null;
reply = board([person('Asha', 1, 300)], [person('Dev', 1, 10)]);
writes = [];
await renderCommunity();
boardWindow = 'week';
fetches = []; writes = [];
await renderCommunity();
assert.ok(says('Dev'), 'this week is a different ranking');
assert.ok(!says('Asha'), 'and does not carry the all-time leader into it');
assert.equal(fetches.length, 0, 'both windows ride on one request');

// An empty week under a busy all-time board is its own sentence.
BOARD = null;
reply = board([person('Asha', 1, 300)], []);
writes = [];
await renderCommunity();
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
await renderCommunity();
assert.ok(says('The class board needs the server'), 'a static export says why it is empty');
BOARD = null; reply = null;

// ---- The rules, on Me. Nobody should have to guess why they have the number
// they have, so every weight and what it earned this person is printed.
reply = mine('trusted', {points: {score: 26, uploads: 1, recordings: 2, votes_received: 1}});
location.hash = '#me/points'; route();
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

// Home, in full and in one place: every live notice, newest first as the
// server sent them, with the flags that say why one is above the others. It
// used to be here in three-row summary AND on Campus in full, so a notice was
// on the screen twice and said two different things about itself.
ANN = [notice('a1', 'Lab moved to Friday', {pinned: true, unread: true}),
       notice('a2', 'Old news')];
NOW = 100; ROLE = 'student'; BOARD = emptyBoard; live = true;
location.hash = '#home'; route();
writes = []; fetches = [];
renderHome();
assert.ok(says('Announcements'), 'Home carries the notice board');
assert.ok(says('Lab moved to Friday') && says('Old news'),
          'both notices are on it, not only the unread ones');
assert.ok(says('Pinned · New'), 'an unread pinned notice says both, in one line');
assert.ok(!says('Post an announcement'), 'a student is offered no way to post');

// And Campus does not carry it as well.
location.hash = '#campus'; route();
writes = [];
renderCampus();
assert.ok(!says('Lab moved to Friday'), 'the board is not printed on Campus too');
assert.ok(says('Clubs'), 'what Campus has is the place itself');
location.hash = '#home'; route();

// Opening the board marks what is on it read, on the server, for this person.
// Not localStorage: the same person opens this on a phone and on a laptop.
BOARD = emptyBoard;
location.hash = '#home'; route();
// After the route, not before it: painting the tab is itself an opening of the
// board, and the point here is what the paint under test asks for.
ANN = [notice('c1', 'Unread one', {unread: true}), notice('c2', 'Read already')];
READ_SENT.clear();
fetches = []; reply = answer(true, {ok: true});
renderHome();
const marks = fetches.filter(f => f[0] === '/read');
assert.equal(marks.length, 1, 'one request, for what is actually on screen');
assert.deepStrictEqual(JSON.parse(marks[0][1].body).ids, ['c1'],
                       'only the unread ones, and never one already read');
await new Promise(setImmediate);
assert.equal(ANN[0].unread, false, 'and it stops being called new straight after');
fetches = [];
renderHome();
assert.equal(fetches.filter(f => f[0] === '/read').length, 0,
             'a second paint of the same board is not a second request');

// Nothing up yet is a real state on day one, and it has to say what the space
// is for rather than showing an empty strip.
ANN = []; ROLE = 'student'; BOARD = emptyBoard;
writes = [];
renderHome();
assert.ok(says('Nothing on the notice board yet'), 'an empty board says so');
assert.ok(says('Your CR or your admin puts them up'),
          'and says who fills it and where new ones show');
ROLE = 'admin';
writes = [];
renderHome();
assert.ok(says('Anything the whole section needs to know goes here'),
          'the admin reading the same empty screen is told what to do with it');
assert.ok(says('Post an announcement'), 'and is given the way in');
assert.ok(wrote(['className', 'row adm']),
          'inked, like every other control only an admin may press');

// Writing one takes the screen over, and costs no request until it is posted
// -- and it is a URL, so the back gesture climbs out of it instead of leaving
// the app with what was typed, and the Home tab button lands on the board.
ROLE = 'admin'; ANN = [];
writes = []; fetches = [];
location.hash = '#home/new'; route();
assert.equal(view.compose, 'new', 'the composer is a level, not a variable');
assert.ok(wrote(['className', 'compose']), 'the composer replaces the board');
assert.ok(!says('Today'), 'one thing at a time on a phone');
assert.equal(fetches.length, 0, 'and nothing is asked for until it is posted');
assert.ok(says('New notice'), 'the header says which level you are on');
assert.ok(says('\u2039 Home'), 'and the back button says where it climbs to');

writes = [];
location.hash = '#home'; route();
assert.ok(!wrote(['className', 'compose']),
          'walking back out of it lands on the board, not on the form again');

ANN = [notice('e1', 'Mine', {mine: true, body: 'the body'})];
writes = [];
location.hash = '#home/e1'; route();
assert.ok(says('Edit notice'), 'editing one is a URL too');
assert.ok(wrote(['value', 'Mine']), 'and it opens on the notice that URL names');

// A student who types the URL gets the board, the same answer /announce gives.
ROLE = 'student';
writes = [];
location.hash = '#home/e1'; route();
assert.ok(!wrote(['className', 'compose']), 'the composer is admin-only here too');
location.hash = '#home'; route();
ROLE = null; ANN = [];

// ---- ATTENDANCE. Three states, and the page never guesses one. ----
assert.equal(ATT, null, 'there is no attendance until the server sends some');
// An earlier check answered /data with an empty library, which emptied DATA.
// Put the two subjects back: what is under test here is the marking, and
// Home only lists a class whose subject the library knows about.
DATA.length = 0;
DATA.push({code: 'MC1101', name: 'Mathematics 1', notes: [], uploads: []},
          {code: 'CY1107', name: 'Chemistry', notes: [], uploads: []});
ATT = {today: '2026-09-07', window: 28,          // that date is a Monday
  subjects: [
    {code: 'MC1101', attended: 9, held: 12, pct: 75, ok: true, can_miss: 0,
     must_attend: 0, note: 'Miss the next class and you drop below 75%.'},
    {code: 'CY1107', attended: 7, held: 10, pct: 70, ok: false, can_miss: 0,
     must_attend: 2,
     note: 'Attend the next 2 classes in a row to get back to 75%.'}],
  marks: [{date: '2026-09-07', period: 1, code: 'MC1101', state: 'present'}],
  off: [{date: '2026-09-07', period: 2, code: 'CY1107', reason: ''}]};
TT = [{day: 1, period: 1, code: 'MC1101'}, {day: 1, period: 2, code: 'CY1107'},
      {day: 1, period: 3, code: 'MC1101'}];

// The date is the server's, never the handset's: it is the one the marks are
// stamped with. And it is never built with toISOString(), which is UTC and
// hands back yesterday for everybody in India before half past five.
assert.equal(attToday(), '2026-09-07');
assert.equal(isoDay(new Date(2026, 8, 7, 0, 30)), '2026-09-07');
assert.equal(dayOfISO('2026-09-07'), 1, 'the server said Monday');

// A level inside Classes, so it is a URL and back climbs out like any other.
location.hash = '#classes/attendance';
writes = [];
route();
assert.equal(view.att, true, 'catching up is a level inside Classes');
assert.ok(wrote(['textContent', 'Your attendance']), 'and it names itself');
assert.ok(wrote(['value', '2026-09-07']), 'it opens on the day the server calls today');
assert.ok(wrote(['max', '2026-09-07']), 'and cannot reach a class that has not happened');

// All three states said in WORDS on the row, not carried by colour or by which
// button looks filled.
assert.ok(wrote(['textContent', 'Period 1 · Present']));
assert.ok(wrote(['textContent', 'Period 3 · Not marked']));
assert.ok(wrote(['textContent', 'Period 2 · Class off']));
assert.ok(wrote(['()', 'aria-pressed', 'true']), 'the marked state is on its button too');

// And when there is a reason for the cancellation, it is on that row. It is
// stored, it is class-readable on purpose and it rides in every payload; a row
// that drops it makes the student ask somebody why their denominator moved.
ATT.off[0].reason = 'lab shifted to Friday';
writes = []; route();
assert.ok(wrote(['textContent', 'Period 2 · Class off · lab shifted to Friday']),
          'the reason a class was called off belongs on the row');
ATT.off[0].reason = '';

// attended / held and the true percentage, straight off the server -- the page
// does none of this arithmetic itself.
assert.ok(wrote(['textContent', '9 of 12 · 75.0%']));
assert.ok(wrote(['textContent', '7 of 10 · 70.0%']));
assert.ok(wrote(['textContent',
                 'Attend the next 2 classes in a row to get back to 75%.']));
// Below 75% carries a word. Never a colour on its own.
assert.ok(wrote(['textContent', 'Below 75%']));

// Marking is on Home, on the classes Home already lists: that is the screen a
// student opens between periods, and a mark that costs a trip elsewhere is a
// mark nobody makes.
location.hash = '#home'; writes = []; route();
assert.ok(wrote(['textContent', 'Period 1 · Present']),
          "today's classes must be markable from Home");
// And nothing else about the week: catching up is Classes's, one tap away,
// and printing it here as well was the same row in two places.
assert.ok(!says('Catch up on another day'), 'Home is today, not the week around it');

// It is on Classes, where the timetable it is about lives.
qvalue = '';                          // an earlier check left a search in the box
location.hash = '#classes'; writes = []; route();
assert.ok(says('Your week'), 'the week is a block on the subject list');
assert.ok(says('Your attendance'), 'and catching up is reached from it');
assert.ok(!says('Your timetable'), 'and the week itself is not editable from it');
location.hash = '#home'; route();

// The NUMBER is with the subject, because that is the screen you open when the
// subject is what you are worried about.
qvalue = '';                          // an earlier check left a search in the box
location.hash = '#classes/MC1101'; writes = []; route();
assert.ok(wrote(['textContent', '9 of 12 · 75.0%']),
          'the per-subject number belongs with the subject');
assert.ok(wrote(['textContent', 'Miss the next class and you drop below 75%.']));

// One tap for a whole day -- offered only where it saves one, and only over
// the classes it may actually touch. Monday has three: period 1 is already
// marked, period 2 was called off, so period 3 is the only thing left and one
// class is already one tap.
assert.equal(allPresentRow('2026-09-07', slotsOn(TT, 1)), null,
             'one class left to mark is not worth a row of its own');

// Give the day a second unmarked class and the row appears. What it posts is
// the point: never the period that was called off, and never one the student
// has already answered -- a deliberate 'absent' is not something a bulk button
// may overwrite.
TT.push({day: 1, period: 5, code: 'CY1107'});
writes = []; fetches = []; reply = null;
assert.ok(allPresentRow('2026-09-07', slotsOn(TT, 1)),
          'two classes still to mark are worth one tap');
assert.ok(says('Mark all 2 present'), 'and it counts those two, not the day');
writes.filter(w => w[0] === 'onclick').pop()[1]();
assert.equal(fetches.length, 1, 'the whole day is one request');
assert.deepStrictEqual(JSON.parse(fetches[0][1].body).marks,
  [{date: '2026-09-07', period: 3, state: 'present'},
   {date: '2026-09-07', period: 5, state: 'present'}],
  'only the unmarked classes that actually happened');
TT.pop();

// ---- THE DAY VIEW. One day, its periods, and what each one holds. ----
// A level inside Classes, so back climbs to the subject list.
DATA[0].notes.push({title: '2026-09-07 limits', kind: 'lecture', md: '', questions: []});
DATA[1].uploads.push({name: 'slides.pdf', path: 'x.pdf'});
location.hash = '#classes/day';
writes = [];
route();
assert.equal(view.day, true, 'the day view is a level inside Classes');
assert.equal(view.code, null, "'day' is not a subject");
assert.ok(wrote(['textContent', 'Your day']), 'and it names itself');
assert.ok(wrote(['value', '2026-09-07']), 'it opens on the day the server calls today');
assert.ok(says('Today'), 'and says so in words rather than only in the picker');

// Every period of that Monday, in order, marked by the control that already
// exists -- the same words the catch-up screen prints, off the same payload.
assert.ok(wrote(['textContent', 'Period 1 · Present']));
assert.ok(wrote(['textContent', 'Period 2 · Class off']));
assert.ok(wrote(['textContent', 'Period 3 · Not marked']));

// What the app already knows about each period: the lecture recorded in that
// slot, and the way into the rest of the subject's shelf.
assert.ok(says('2026-09-07 limits'), "the day's own lecture belongs on its period");
assert.ok(says('Notes & slides in CY1107'), 'and the subject the period is in');
assert.ok(!says('2026-09-07 limits · '), 'nothing here is invented about it');

// Stepping a day is two taps and no picker: the arrows are the first two
// controls on the screen, and they are what a thumb reaches for.
let steps = writes.filter(w => w[0] === 'onclick').map(w => w[1]);
writes = [];
steps[0]();                                      // yesterday
assert.equal(dayDate, '2026-09-06', 'the left arrow is one day back');
assert.ok(says('Sunday'), 'and Sunday holds no periods, which is an answer');
assert.ok(says('No classes on Sunday.'));

// Forward the same way, over today, into a day nobody can mark yet: the row is
// still worth drawing -- it says what you have -- and it loses the buttons.
dayDate = '2026-09-07'; writes = []; render();
steps = writes.filter(w => w[0] === 'onclick').map(w => w[1]);
writes = [];
steps[1]();                                      // tomorrow
assert.equal(dayDate, '2026-09-08');
assert.ok(says('Tomorrow'), 'the three days with names get their name');

// A day further back than the payload reaches is drawn and not marked: the
// marks exist, they are simply not here, and "Not marked" would be a lie about
// the one number that costs an exam.
dayDate = '2026-01-05';                          // also a Monday, months back
writes = []; render();
assert.ok(wrote(['textContent', 'Period 1']), 'the day is still worth reading');
assert.ok(!wrote(['textContent', 'Period 1 · Not marked']),
          'nothing may claim a mark is missing when it is only out of reach');
assert.ok(says('Monday 5 January'), 'and a dated day says which one it is');

// Leaving and coming back starts on today again, never on last January.
location.hash = '#classes'; route();
assert.equal(dayDate, null);
writes = []; render();
assert.ok(says('Today\u2019s classes'), 'the subject list is the way in');
location.hash = '#classes/day'; writes = []; route();
assert.ok(says('Today'));
DATA[0].notes.pop(); DATA[1].uploads.pop();
location.hash = '#classes'; route();

// ---- The institute's calendar. A holiday is a Monday like any other as far
// as the timetable knows, so a screen that asks by weekday draws a full day of
// classes onto it. 14 Sep 2026 is Ganesh Chaturthi AND a Monday, which is the
// whole reason this is asked by date.
ATT.closed = [{date: '2026-09-14', title: 'Ganesh Chaturthi', kind: 'holiday'}];
dayDate = '2026-09-14';
location.hash = '#classes/day'; writes = []; route();
assert.ok(says('Ganesh Chaturthi, no classes.'),
          'the day names the holiday, not "No classes on Monday"');
assert.ok(!wrote(['textContent', 'Period 1 · Not marked']),
          'and offers no class to mark on a day the institute closed');
assert.equal(slotsFor('2026-09-14').length, 0, 'a closed date holds no periods');
assert.equal(slotsFor('2026-09-07').length, 3, 'an ordinary Monday still does');
assert.equal(markable('2026-09-14'), false, 'nothing to mark on a holiday');
assert.equal(markable('2026-09-07'), true, 'an ordinary past Monday still marks');

// An ordinary empty day still says so the plain way -- the holiday wording
// must not leak onto a Sunday.
dayDate = '2026-09-13';                       // the Sunday before it
writes = []; route();
assert.ok(says('No classes on Sunday.'), 'a plain empty day keeps the plain line');

ATT.closed = []; dayDate = null;
location.hash = '#classes'; route();

// ---- The countdown, on the tab that owns the calendar. The institute's own
// next date, not a semester total this app was never told -- and honest about
// a window already running. It is on Classes rather than Home because Home is
// today and this is the term around it.
location.hash = '#classes'; writes = []; route();
ATT.next = {date: '2026-09-20', ends: '2026-09-20', title: 'Attendance displayed',
            kind: 'milestone'};                          // 13 days from 2026-09-07
writes = []; render();
assert.ok(says('Coming up'), 'the calendar gets its own block on Classes');
assert.ok(says('Attendance displayed'));
assert.ok(says('In 13 days · Sep 20'));

ATT.next = {date: '2026-09-06', ends: '2026-09-10', title: 'Mid-terms',
            kind: 'exam'};                    // started yesterday, still running
writes = []; render();
assert.ok(says('On now · ends Sep 10'), 'a window already running says so, not a stale date');

ATT.next = {date: '2026-09-07', ends: '2026-09-07', title: 'Last day', kind: 'milestone'};
writes = []; render();
assert.ok(says('On now · ends today'), 'a one-day window ending today does not repeat the date');

ATT.next = {date: '2026-09-08', ends: '2026-09-08', title: 'Tomorrow thing',
            kind: 'milestone'};
writes = []; render();
assert.ok(says('Tomorrow'), 'one day out is named, not counted');

// ---- The week strip. 2026-09-07 is a Monday with three classes on it, and
// the Wednesday after is closed, so the strip has to tell the three apart.
ATT.closed = [{date: '2026-09-09', title: 'Test holiday', kind: 'holiday'}];
ATT.upcoming = [{date: '2026-09-20', ends: '2026-09-20', title: 'Attendance displayed', kind: 'milestone'},
                {date: '2026-10-27', ends: '2026-11-03', title: 'Mid-term examinations', kind: 'exam'}];
writes = []; render();
assert.ok(says('Calendar') && says('September 2026') && says('This week'), 'a calendar, not a sentence');
assert.ok(wrote(['className', 'today has']), 'today is marked, and has classes');
assert.ok(wrote(['()', 'aria-label', 'Wednesday 9 September, Test holiday']),
          'a closed day is marked as one, in words');
assert.ok(wrote(['()', 'aria-label', 'Today, 3 classes']), 'today says how many classes');
assert.ok(says('Wednesday: Test holiday, no classes'), 'and the strip says why');
assert.ok(says('Mid-term examinations') && says('In 50 days · Oct 27'), 'several dates, not one');
assert.ok(wrote(['textContent', 'Oct']) && wrote(['textContent', 27]), 'each on a tear-off date tile');
assert.ok(says('See the whole month'), 'the strip is the way into the month');
ATT.closed = []; delete ATT.upcoming;

// ---- The month. Both calendars on one page, and the whole term behind it:
// `closed` only reaches three weeks ahead, so mid-terms in November have to
// come from the whole-term list or a November Monday draws three classes.
ATT.calendar = [
  {date: '2026-09-14', ends: '2026-09-14', title: 'Ganesh Chaturthi', kind: 'holiday',
   teaching: false, notable: true},
  {date: '2026-10-27', ends: '2026-11-03', title: 'Mid-term examinations', kind: 'exam',
   teaching: false, notable: true}];
CAMPUS = {events: [{id: 'e1', title: 'Robotics fest', society: 'Robotics', venue: 'OAT',
                    date: '2026-09-21', ends: '2026-09-21', deleted: false}]};
location.hash = '#classes/calendar'; writes = []; route();
assert.ok(says('Calendar') && says('September 2026') && says('This month'));
assert.ok(says('Ganesh Chaturthi') && says('Robotics fest'), 'both calendars, together');
assert.ok(wrote(['()', 'aria-label', 'Monday 14 September, Ganesh Chaturthi']),
          'a holiday from the whole-term list closes the day');
assert.ok(wrote(['()', 'aria-label', 'Monday 21 September, 3 classes, Robotics fest']),
          'an ordinary day with a fest on it says both');
assert.equal(slotsFor('2026-11-02').length, 0, 'mid-terms past the closed window still empty a day');
calMonth = '2026-10'; writes = []; render();
assert.ok(says('October 2026') && says('Mid-term examinations') && !says('Robotics fest'),
          'stepping a month shows that month');
delete ATT.calendar; CAMPUS = null;
location.hash = '#classes'; route();

ATT.next = null;
writes = []; render();
assert.ok(!says('Coming up'), 'nothing to count down to draws nothing');

// A society's fest arrives in the same array and belongs to Campus, which has
// its own list of them. Printing it here as well was two calendars pretending
// to be one, and a student keeping both of them in their head.
ATT.upcoming = [{date: '2026-09-20', ends: '2026-09-20', title: 'Attendance displayed',
                 kind: 'milestone'},
                {date: '2026-09-21', ends: '2026-09-21', title: 'Robotics fest',
                 what: 'campus', kind: 'event'}];
writes = []; render();
assert.ok(says('Attendance displayed'), 'the institute calendar is here');
assert.ok(!says('Robotics fest'), 'and a campus event is not, because Campus has it');
delete ATT.upcoming;

// Home is today, and holds neither of them.
location.hash = '#home'; writes = []; route();
assert.ok(!says('Coming up') && !says('This week'),
          'Home no longer carries the calendar it shared with Campus');

// No server, no marks: Home still draws today rather than offering a control
// that cannot save anything.
ATT = null;
location.hash = '#home'; writes = []; route();
assert.ok(!wrote(['textContent', 'Period 1 · Not marked']),
          'a page with no server behind it offers no marking control');
TT = [];

// ---- Saving a note. Private, and nothing to do with the vote or the score --
// this is "I want to find this again", not "this is good".
// The day view above emptied and re-popped DATA[0].notes, so put one back
// rather than lean on whatever an earlier section happened to leave there.
DATA[0].notes.push({title: 'week1', kind: 'lecture', md: '# limits', questions: []});
ATT = {today: '2026-09-07', window: 28, subjects: [], marks: [], off: [], closed: []};
BOOKMARKS = [];
location.hash = '#classes/MC1101/week1'; route();
assert.equal(saveEl.textContent, 'Save', 'not saved until the student says so');
assert.equal(saveAttrs['aria-pressed'], 'false');

reply = answer(true, {bookmarks: [{code: 'MC1101', title: 'week1'}]});
fetches = [];
await saveEl.onclick({currentTarget: saveEl});
assert.ok(fetches.some(f => f[0] === '/bookmark'
  && JSON.parse(f[1].body).subject === 'MC1101'
  && JSON.parse(f[1].body).title === 'week1'
  && JSON.parse(f[1].body).on === true), 'asks to save THIS note, by (subject, title)');
assert.equal(saveEl.textContent, 'Saved', 'the server answered and the button follows it');
assert.deepStrictEqual(BOOKMARKS, [{code: 'MC1101', title: 'week1'}]);

// Reopening the same note later must read the saved state back, not just
// remember it from the toggle that just ran.
location.hash = '#classes/MC1101'; route();
location.hash = '#classes/MC1101/week1'; route();
assert.equal(saveEl.textContent, 'Saved', 'opening an already-saved note shows it as saved');

reply = answer(true, {bookmarks: []});
fetches = [];
await saveEl.onclick({currentTarget: saveEl});
assert.ok(fetches.some(f => JSON.parse(f[1].body).on === false), 'the second tap takes it back');
assert.equal(saveEl.textContent, 'Save');
assert.deepStrictEqual(BOOKMARKS, []);

// What you saved is its own screen behind the avatar, not a block near the
// bottom of Home: what you saved is yours, and Home is today. It is drawn from
// whatever is still really there, and skips a bookmark whose note is gone -- a
// renamed file, a replaced revision sheet -- rather than showing a row with
// nothing behind it.
BOOKMARKS = [{code: 'MC1101', title: 'week1'}];
location.hash = '#home'; writes = []; route();
assert.ok(!says('week1'), 'Home no longer carries what you saved');
location.hash = '#me/saved'; writes = []; route();
assert.equal(view.me, 'saved', 'it is a level under you, and therefore a URL');
assert.ok(says('week1'), 'and a real save is on it');

BOOKMARKS = [{code: 'MC1101', title: 'a note that was deleted'}];
writes = []; render();
assert.ok(!says('a note that was deleted'),
          'a stale bookmark for a gone note draws nothing, not a dead row');
assert.ok(says('Nothing saved yet'), 'and the screen says so rather than going blank');

BOOKMARKS = [];
location.hash = '#home'; route();
reply = null;


// ---- Doubts. What one student typed, put on the page as typing and never as
// markup: this is the one screen in the app whose words come from a classmate
// and are not run through marked, and the difference is textContent.
reply = answer(true, {doubts: [{
  id: 'q1', body: '<img src=x onerror=alert(1)>', by: 'Chan', at: 1,
  mine: false, votes: 0, voted: false,
  answers: [{id: 'a1', body: 'Because the equation balances.', by: 'Dia',
             at: 2, mine: false, votes: 2, voted: false}],
}]});
writes = [];
await loadDoubts({subject: 'MC1101', title: 'week1'}, doubtsBox, 'Doubts');
assert.ok(fetches.some(f => f[0] === '/doubts?subject=MC1101&title=week1'),
          'the thread is asked for by the two things the library names a note by');
assert.ok(wrote(['textContent', '<img src=x onerror=alert(1)>']),
          'a question is somebody typing, never markup');
assert.ok(!writes.some(w => w[0] === 'innerHTML' && String(w[1]).includes('img')),
          'and nothing anybody types is ever parsed');
assert.ok(wrote(['textContent', 'Because the equation balances.']),
          'an answer is drawn the same way');
assert.ok(wrote(['className', 'vote']),
          "an answer carries the notes' own vote control");
assert.ok(wrote(['textContent', 2]), 'with the count the class gave it');

// A page with no server behind it says so instead of showing a form that
// cannot post -- the static export has no /doubts to ask.
reply = 'gone';
writes = [];
await loadDoubts({subject: 'MC1101', title: 'week1'}, doubtsBox, 'Doubts');
assert.ok(wrote(['textContent', 'This needs the server. Run: notes.py serve']),
          'no server, no form');
reply = null;

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
                   "classList.toggle('primary', practice.hidden)",
                   "b.onclick = qOpenAll",
                   "srMark(qz.items[qz.order[qz.i]], ok)"):
        assert wiring in notes.PAGE, f"practice button not wired: {wiring}"


def test_the_vote_control_is_wired_to_the_server_and_nothing_else():
    """The stub cannot see an onclick assigned to a proxy, so the checks above
    build the control and this is the other half: what pressing it does."""
    for wiring in (
        # One request, carrying which item and which direction. Three shapes
        # of it, because one control votes for an upload, an answer to a doubt
        # and something said on the wall -- they are one table and one endpoint.
        "body: JSON.stringify(u.post ? {post: u.id, on: !u.voted}",
        ": answer ? {answer: u.id, on: !u.voted}",
        ": {id: u.id, on: !u.voted}",
        "if (u.id) el.appendChild(voteBtn(u));",
    ):
        assert wiring in notes.PAGE, f"vote control not wired: {wiring}"
    # And then the whole list again, because a vote changes the ranking -- or
    # the thread, when it was an answer that was voted for. Read out of the
    # handler itself: /revise refreshes too, so a page-wide grep for this line
    # passed with the vote's own refresh deleted.
    handler = re.search(r"function voteBtn\(u, answer\) \{.*?\n\}",
                        notes.PAGE, re.S).group(0)
    assert "else await (answer ? loadDoubts() : refresh());" in handler, \
        "a vote must re-read the list, or the thread, it re-ranks"
    assert "if (u.post) { WALLS[wallOn] = null; render(); }" in handler, \
        "and the wall it re-ranks, when the vote was for a post"


def test_the_tab_bar_is_wired_and_gives_way_to_the_reading_dock():
    """The bar is the shell. The stub cannot see an onclick on a proxy or read
    CSS, so this reads the source: that a tab navigates, and that the one place
    the bar is not shown is under an open note, where the dock takes the strip.
    """
    for wiring in ("tabBtns.forEach(b => { b.onclick = () => go(b.dataset.tab); });",
                   'data-tab="home"', 'data-tab="classes"',
                   'data-tab="campus"', 'data-tab="community"',
                   # And Me is no longer one of them: it is behind the avatar,
                   # which opens the drawer that holds the whole map.
                   'id="avatar"', 'id="drawer"',
                   # Reading takes the strip; back gives it straight back,
                   # because closeRead() drops the class that hid it.
                   "body.reading .tabs{display:none}"):
        assert wiring in notes.PAGE, f"tab bar not wired: {wiring}"
    # And on a wide screen there is no bar at all: the drawer stands open as a
    # rail beside the two panes, and every tab in the bar is a row in it. A
    # thumb bar pinned under a 320px column on a 1400px screen was a phone
    # control that came along by accident.
    wide = re.search(r"@media \(min-width:760px\)\{(.*?)\n\}\n", notes.PAGE, re.S).group(1)
    assert ".tabs{display:none}" in wide, "the wide layout navigates from the rail"
    assert "body.reading .tabs" not in wide, "there is no bar left to give back"
    for rail in ("#drawer{position:sticky", "#scrim,#dclose{display:none}",
                 "#avatar{display:none}"):
        assert rail in wide, f"the rail is not wired: {rail}"
    # The last row of a list must clear both the bar and the FAB floating above
    # it, not sit under either: the FAB reaches 76 + 58 = 134px up, and at 80px
    # it covered the bottom 54px of the list -- the last row's vote button with
    # it -- with no scroll left to escape.
    # Both columns take it from one token now -- it was two copies of the same
    # number and it was missing everywhere else. See
    # test_the_plus_button_has_room_reserved_for_it_everywhere.
    assert "#nav{padding-bottom:var(--fabclear)}" in notes.PAGE
    # An open note is the same problem: every generated note ends in a
    # <details><summary>Full transcript</summary>, and at 118px the FAB sat on
    # the bottom 16px of it and took the taps meant for it. The clearance sits
    # on the Doubts thread now, which is what the note ends in.
    assert "#doubts{max-width:70ch;margin:0 auto;padding:0 18px var(--fabclear)}" in notes.PAGE
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
    # And nothing on the page writes a timetable: the week is the section's,
    # seeded from its template, and the student's copy is read-only.
    assert "fetch('/timetable'" not in notes.PAGE
    assert "{slots}" not in notes.PAGE
    # And Home must not be one of the screens that waits on a fetch to draw.
    home = re.search(r"\nfunction renderHome\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "await" not in home and "fetch" not in home


def test_home_learns_the_server_is_there_before_it_draws():
    """`live` is what decides whether an empty timetable reads as "nobody has
    put the section's week in" or as "there is no server". Set after the render rather than
    before it, a perfectly good server said the second one -- and nothing
    redrew Home afterwards to correct it."""
    body = re.search(r"async function refresh\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert body.index("live = true;") < body.index("render();")


def test_no_student_can_edit_a_timetable_from_this_page():
    """The week belongs to the section, not to the reader of it. A student
    editing their own copy only ever drifted away from the grid the registrar
    published, so the editor and every way into it are gone -- and '#classes/
    timetable' is rewritten to the subject list rather than left as a URL that
    opens nothing."""
    for gone in ("renderTimetable", "saveTimetable", "Save timetable",
                 "Set up your timetable", "go('classes', 'timetable')",
                 "view.edit"):
        assert gone not in notes.PAGE, f"the timetable editor is still here: {gone}"
    assert "'classes/timetable': 'classes'" in notes.PAGE


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
    # The + used to stay while you read, on the grounds that adding works from
    # anywhere. What that meant in practice was 58px of accent sitting on the
    # paragraph, 76px above a dock that is already the reading screen's bar.
    # It goes on every layout now: the wide one is two panes and not three, so
    # a note covers the list it came out of there too, and a + left standing
    # would be over the words exactly as it was on a phone.
    assert "body.reading #fab{display:none}" in notes.PAGE
    assert "body.reading #fab{display:block}" not in notes.PAGE


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
    assert "retry ? ', tap an option to try again' : ''" in up


# -------------------------------------------------- what a student is shown


def test_every_way_in_is_locked_rather_than_missing():
    """A student may not upload, record or Explain. Hiding those controls was
    the old answer and it taught nobody anything: the app simply looked like an
    app that does not do that. Each one stays, marked, with the reason."""
    for wiring in (
        # The + button is shown and marked, not removed. It is away only where
        # there is no role yet, or where the screen has a primary action of its
        # own for it to be standing on.
        "fab.hidden = ROLE === null || !!view.compose;",
        # And it is not locked on Community, where what it opens is a box to
        # type in rather than an upload -- students write on the wall.
        "fab.className = (fabWrites() || mayAdd()) ? '' : 'locked';",
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
                                   ("--mut", "--bg"),
                                   # Below 75%. It is the one screen somebody
                                   # reads while anxious, so the word in the
                                   # amber has to be legible in both themes --
                                   # on its own chip and on the page behind it.
                                   ("--warn", "--warn-bg"), ("--warn", "--bg")])
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


def test_a_control_that_draws_no_fill_has_an_edge_you_can_see():
    """WCAG 1.4.11 wants 3:1 for the boundary of something you press.

    --line is the hairline between two rows and is 1.24:1 on the ground, and
    every outlined control in the app was drawing it: Rename, Remove, the two
    attendance marks, Cancel, the day stepper, a post's Take down. At that
    ratio they are grey text, not buttons. --edge is --mut thinned to 70% --
    the same ink those buttons write in, composited onto whichever ground
    they sit on -- so this checks the arithmetic rather than the literal.
    """
    from test_gate_page import contrast

    css = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    mix = re.search(r"--edge:color-mix\(in srgb,var\((--[\w-]+)\) (\d+)%", css)
    assert mix, "--edge is no longer a mix of a token with transparent"
    over, pct = mix.group(1), int(mix.group(2)) / 100

    roots = re.findall(r":root\{([^}]*)\}", css)
    light, dark = ({k: v for k, v in
                    re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", r)}
                   for r in roots)
    for mode, tokens in (("light", light), ("dark", {**light, **dark})):
        ink = tokens[over].lstrip("#")
        ground = tokens["--bg"].lstrip("#")
        # alpha over the paper: what the eye is actually given to see.
        edge = "#" + "".join(
            "%02x" % round(int(ink[i:i + 2], 16) * pct
                           + int(ground[i:i + 2], 16) * (1 - pct))
            for i in (0, 2, 4))
        ratio = contrast(edge, tokens["--bg"])
        assert ratio >= 3, f"a control's only edge is {ratio:.2f}:1 in {mode}"


def test_no_control_is_bounded_by_the_line_between_two_rows():
    """The rule above is only worth having if nothing quietly goes back.

    --line is for a divider. Anything a thumb lands on that draws a border and
    no fill takes --edge, --admin or --accent, all of which clear 3:1.
    """
    css = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    pressable = (".row-del", ".mark button", ".card .acts", ".compose .go button",
                 ".pform .go button", ".dpick .step", ".dpick input", ".mine .edit",
                 "#batchName", ".thread .acts button", ".askbox textarea",
                 ".saybox input", "#sheet select")
    for block in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        selector, body = block[0].strip(), block[1]
        if not re.search(r"border(-[a-z]+)?:1px solid var\(--line\)", body):
            continue
        assert not any(sel in selector for sel in pressable), \
            f"{selector} is bounded by --line, which is 1.24:1"


def test_every_screen_motion_is_short_and_answers_something():
    """Fast, one direction, no overshoot -- and off entirely for anybody who
    has asked for that. A page that animates on its own is a page that has to
    be waited for, and this app is used walking out of a lecture."""
    css = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    for ms in re.findall(r"transition:[^;}]*?(\d+)ms", css):
        assert int(ms) <= 200, f"a {ms}ms transition is a wait, not an answer"
    for ms in re.findall(r"animation:\w+ (\d+)ms", css):
        assert int(ms) <= 200, f"a {ms}ms animation is a wait, not an answer"
    assert "@media (prefers-reduced-motion:reduce){*{animation:none!important;" \
           "transition:none!important}}" in css, "reduced motion is not honoured"
    # The screen animation is route()'s alone. render() runs again on every
    # vote, every mark and every poll, and a screen that re-animates when you
    # tick one box is a screen that flickers.
    assert notes.PAGE.count("arrive(") == 2, \
        "the screen animation has more than one caller and one definition"
    assert "if (moving) arrive(" in notes.PAGE


def test_waiting_on_the_server_never_looks_like_having_nothing():
    """"Checking your attendance" and "You have marked nothing" used to be the
    same shape: one line of grey text. Every section that fetches draws the
    indeterminate bar the rest of the app already uses."""
    for waited in ("waitline('Checking your timetable\u2026')",
                   "waitline('Checking your attendance\u2026')",
                   "waitline('Reading what is on\u2026')",
                   "waitline('Reading the directory\u2026')",
                   "waitline('Opening the room\u2026')"):
        assert waited in notes.PAGE, f"a screen still waits silently: {waited}"
    # An unasked room is not an empty one and must not be drawn as one.
    assert "if (!roomRead) return log.appendChild(waitline(" in notes.PAGE
    assert "roomRead = true;" in notes.PAGE


def test_attendance_is_wired_to_the_server_and_never_guesses_a_state():
    """The node stub swallows an onclick assigned to a proxy, so the checks
    above build the controls and this is the other half: what pressing one
    does, and what the page refuses to work out for itself."""
    for wiring in (
        # One route for a mark, one for a cancellation, and nothing else.
        "attPost('/attendance', {marks: [{date, period: slot.period,",
        "attPost('/cancelled',",
        # Pressing the state you are already in clears it. Without this there
        # is no way back to not-yet-marked and a mistap is permanent.
        "state: now && now.state === state ? 'clear' : state}]}, btns)",
        # A whole day in one request: catching up on a week must not be one
        # round trip per period.
        "{marks: todo.map(sl => ({date, period: sl.period, state: 'present'}))}",
    ):
        assert wiring in notes.PAGE, f"attendance not wired: {wiring}"

    # Every write answers with the whole payload and the page adopts it whole.
    # The alternative is the page doing this arithmetic too, and two places
    # computing the number a student plans a term around will disagree.
    post = re.search(r"async function attPost\(.*?\n\}", notes.PAGE, re.S).group(0)
    assert "ATT = d;" in post and "render();" in post
    # The row prints what the server sent and works nothing out. The
    # percentage, the run of misses and the sentence all arrive made -- two
    # places computing the number a student plans a term around will disagree,
    # and only one of them is the one the database agrees with.
    row = re.search(r"function attRow\(a, showCode\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "a.pct.toFixed(1)" in row, "the server's floored figure, printed as it came"
    for invented in ("/ a.held", "* 100", "0.75", "Math.round", "toFixed(0)"):
        assert invented not in row, f"the row is recomputing attendance: {invented}"
    assert "a.note" in row, "the consequence is the server's sentence, not one of ours"


def test_attendance_rides_on_the_request_the_page_already_makes():
    """Home marks today's classes and the subject screen prints the number.
    Neither may add a round trip: this tab is opened between periods on mobile
    data, and /data is already in flight."""
    assert "ATT = d.attendance || null;" in notes.PAGE
    # No GET of its own, ever. Only the two writes talk to a route at all.
    assert notes.PAGE.count("fetch('/attendance'") == 0
    assert notes.PAGE.count("attPost('/attendance'") == 2
    home = re.search(r"\nfunction todayBlock\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "await" not in home and "fetch" not in home


def test_the_catch_up_screen_is_a_url_and_uses_the_platform_date_picker():
    """A level inside Classes with a URL of its own, so back climbs out of it.
    The picker is the browser's own: it is the fastest one on a phone and it
    costs nothing to ship."""
    for wiring in ("const att = tab === 'classes' && parts[1] === 'attendance';",
                   "if (!att) attDate = null;",
                   "mark.onclick = () => go('classes', 'attendance');",
                   "input.type = 'date';",
                   # Bounded by what the server keeps, and by what has happened:
                   # the day view has the same picker and no bounds at all, so
                   # these two are the caller's, not the picker's.
                   "shiftDay(attToday(), -ATT.window), attToday());"):
        assert wiring in notes.PAGE, f"the catch-up screen is not wired: {wiring}"
    # The date is built by hand rather than with toISOString(), which is UTC
    # and hands back yesterday for every student this app has.
    day = re.search(r"const isoDay = .*?;", notes.PAGE, re.S).group(0)
    assert "toISOString" not in day and "getFullYear()" in day


def test_the_day_view_marks_a_class_the_one_way_this_app_marks_a_class():
    """A second way to mark is a second denominator, and the number is the one
    a student plans a term around. The day view draws classRow and posts
    nothing of its own: no arithmetic, no endpoint, no second control."""
    body = re.search(r"function renderDay\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "classRow(date, slot, mayAdd())" in body, "the row that already marks"
    assert "allPresentRow(date, slots)" in body, "and the one tap that marks a day"
    for forbidden in ("fetch(", "attPost", "pct", "attended", "held", "75"):
        assert forbidden not in body, f"renderDay reaches for {forbidden}"
    for once in ("function markCtl(", "function classRow(", "function allPresentRow("):
        assert notes.PAGE.count(once) == 1, f"{once} must have one definition"
    # The periods are this student's own timetable, never the section template:
    # Section I splits for the labs, and the seeded per-profile copy is the only
    # thing that knows which batch this phone belongs to.
    #
    # Asked by DATE rather than by weekday, because the two differ on exactly
    # the days that matter: Ganesh Chaturthi is a Monday and the timetable is
    # full of Mondays. slotsFor still reads TT -- asserted where it is defined
    # rather than by the shape of the call here.
    assert "slotsFor(date)" in body
    assert re.search(r"const slotsFor = date =>[^;]*slotsOn\(TT,", notes.PAGE), \
        "slotsFor must still read this student's own timetable"
    assert "section_timetable" not in notes.PAGE


def test_the_day_view_is_a_url_and_steps_a_day_without_the_picker():
    """Today by default, and yesterday one tap away. A date picker is the only
    route on nobody's phone: the step is what gets used walking out of a
    lecture, and the wheel is for jumping a month."""
    for wiring in ("const dayv = tab === 'classes' && parts[1] === 'day';",
                   "if (!dayv) dayDate = null;",
                   "today.onclick = () => go('classes', 'day');",
                   "const date = dayDate || attToday();",
                   "b.setAttribute('aria-label', n < 0 ? 'The day before' : 'The day after');"):
        assert wiring in notes.PAGE, f"the day view is not wired: {wiring}"
    # One picker, two screens. A second copy is a second set of bounds to drift.
    assert notes.PAGE.count("function dayPicker(") == 1
    assert notes.PAGE.count("dayPicker(") == 3


def test_nobody_else_ever_sees_a_students_attendance():
    """Private in the way a mark is private. The server is what enforces it,
    but the page must not have a screen that would show one if it arrived."""
    for screen in ("renderMe", "renderCampus", "renderCommunity", "boardRow",
                   "renderAnnouncements"):
        body = re.search(r"function " + screen + r"\(.*?\n\}", notes.PAGE, re.S)
        assert body, screen
        for word in ("ATT", "attendance", "attOf", "attRow"):
            assert word not in body.group(0), (
                f"{screen} reaches for {word}; attendance is nobody else's")


def test_below_seventy_five_is_never_carried_by_colour_alone():
    """It is a fact about a term still in progress, not an error the app made,
    so it is not red -- and the state is in the word as well as the amber."""
    assert "f.textContent = 'Below 75%';" in notes.PAGE
    assert "el.classList.add('low');" in notes.PAGE
    warn = re.search(r"--warn:\s*(#[0-9a-fA-F]{6})", notes.PAGE).group(1)
    assert warn != "#e5484d", "warning red already means an error on this page"


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
                   "else if (view.tab === 'community') renderCommunity();"):
        assert wiring in notes.PAGE, f"the board is not wired: {wiring}"
    refresh = re.search(r"async function refresh\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "BOARD = null;" in refresh, "a vote re-ranks the board, so it must be re-read"


def test_the_admin_way_in_is_wired_and_gated_on_the_server_too():
    """Hiding a button is a courtesy. /admin is in ROLE_REQUIRED, so the gate
    refuses a student's curl exactly as it refuses a student's browser."""
    assert notes.ROLE_REQUIRED["/admin"] == "admin"
    assert "if (!atLeast('admin')) return;" in notes.PAGE, "the Home row is role-gated"
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
    # Home must stay the screen that waits on nothing. The board is drawn there
    # in full now -- there is no second, three-row copy of it to keep in step.
    assert "function noticeBlock(" not in notes.PAGE, \
        "a summary of the board on the same tab as the board is two boards"
    home = re.search(r"\nfunction renderAnnouncements\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "await" not in home and "fetch" not in home
    assert "markRead(list);" in home, "opening the board is what marks it read"


def test_posting_is_for_the_class_rep_on_the_server_too():
    """Hiding the composer is a courtesy. The lock is the gate, which refuses
    curl exactly as it refuses a student's browser -- and under that, a policy
    that refuses a stolen cookie too."""
    assert notes.ROLE_REQUIRED["/announce"] == "cr"
    assert "/read" not in notes.ROLE_REQUIRED, "what you have read is not a privilege"
    campus = re.search(r"function renderAnnouncements\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "if (atLeast('cr'))" in campus, "the composer is offered on the rung"
    assert "a.mine && atLeast('cr')" in notes.PAGE, \
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
    Campus tab button kept landing back on the form. The catch-up screen is
    the pattern; this is the same shape, so back, the header button and Cancel
    are all the one mechanism."""
    assert "post.onclick = () => go('home', 'new');" in notes.PAGE
    assert "edit.onclick = () => go('home', a.id);" in notes.PAGE
    # A section word and a composer word sit at the same step of the hash, so
    # the section is read first and the composer only takes what is left.
    assert ("const compose = !sec && (tab === 'campus' || tab === 'home' || tab === 'community')\n"
            "    ? parts[1] || null : null;" in notes.PAGE)
    assert "composing" not in notes.PAGE, "no variable may outlive the URL"
    assert ("lback.hidden = !s && !view.att && !view.compose && !view.day\n"
            "                 && !view.me && !view.papers && !view.cal;" in notes.PAGE)
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


# --------------------------------------------------------- offline reading

def test_sw_js_is_served_with_a_registrable_content_type(tmp_path):
    """Not just that the string exists -- a real GET to /sw.js, over the wire,
    with a content type a browser will actually register a worker from."""
    import threading
    import urllib.request

    args = make_args(tmp_path)
    was = notes.LOG_PATH
    try:
        srv = notes.build_server(args)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/sw.js", timeout=5) as r:
                assert r.status == 200
                assert "javascript" in r.headers.get("Content-Type", "")
                assert r.headers.get("Cache-Control") == "no-store", \
                    "a stale service worker script is a phone that never " \
                    "learns the caching strategy changed"
                assert r.read().decode() == notes.SW_JS
        finally:
            srv.shutdown()
    finally:
        notes.LOG_PATH = was


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_service_worker_script_parses(tmp_path):
    f = tmp_path / "sw.js"
    f.write_text(notes.SW_JS)
    r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# A worker global scope, not a window: self, caches and fetch are globals
# there, never parameters, so the stub declares them the same way.
SW_STUB = """
const assert = require('node:assert');
let currentCache = new Map();
let deletedCaches = [];
const caches = {
  open: async () => ({
    put: async (req, res) => { currentCache.set(req.url, res); },
  }),
  match: async (req) => currentCache.get(req.url),
  keys: async () => ['recarve-v0', 'recarve-v1'],
  delete: async (name) => { deletedCaches.push(name); return true; },
};
let handlers = {};
let claimed = false, skipped = false;
const self = {
  location: { origin: 'https://x.test' },
  addEventListener: (ev, fn) => { handlers[ev] = fn; },
  skipWaiting: () => { skipped = true; },
  clients: { claim: () => { claimed = true; } },
};
let mode = 'ok';           // 'ok' | 'fail'
let fetched = [];
function fetch(request) {
  fetched.push(request.url);
  if (mode === 'fail') return Promise.reject(new Error('offline'));
  return Promise.resolve({ ok: true, clone() { return this; } });
}
function ev(url, method) {
  let responded = null;
  return {
    request: { method: method || 'GET', url },
    respondWith: (p) => { responded = p; },
    result: () => responded,
  };
}
"""

SW_CHECKS = """
(async () => {

assert.ok(handlers.install && handlers.activate && handlers.fetch,
          'install, activate and fetch are all wired');

// install takes over immediately rather than waiting for every open tab to
// close -- a small app where the alternative is a phone stuck on yesterday's
// cache until it is force-quit.
handlers.install();
assert.equal(skipped, true);

// activate clears anything that is not this exact cache name, so a version
// bump is the whole migration and nothing lingers holding an old shape of
// /data.
let waited = null;
handlers.activate({ waitUntil: (p) => { waited = p; } });
await waited;
assert.deepStrictEqual(deletedCaches, ['recarve-v0'], 'only the OLD cache is dropped');
assert.equal(claimed, true);

// The shell and /data are cached on a successful fetch.
let e = ev('https://x.test/data');
handlers.fetch(e);
await e.result();
assert.ok(fetched.includes('https://x.test/data'));
assert.ok(currentCache.has('https://x.test/data'), 'a good answer is kept');

// Offline, the same URL is answered from what was kept -- not a rejection,
// not the browser's own error page.
mode = 'fail';
e = ev('https://x.test/data');
handlers.fetch(e);
const r = await e.result();
assert.ok(r, 'the cached copy answers when the network cannot');

// A path this was never asked to keep is left alone entirely: no cache read,
// no cache write, and respondWith is never even called, so the browser's own
// default handling runs.
mode = 'ok'; fetched = [];
e = ev('https://x.test/vote');
handlers.fetch(e);
assert.equal(e.result(), null, 'an endpoint outside KEEP is not intercepted');
assert.deepStrictEqual(fetched, [], 'and never even reaches fetch()');

// A POST is never cached, however familiar the path -- a vote or a mark only
// means something if it reaches the server, and a cached POST answer would be
// a write silently reported as done that was not.
e = ev('https://x.test/data', 'POST');
handlers.fetch(e);
assert.equal(e.result(), null, 'POST is never intercepted, even to /data');

// A different origin (a CDN, an API this app does not run) is never touched.
e = ev('https://elsewhere.test/data');
handlers.fetch(e);
assert.equal(e.result(), null, 'only this origin is ever cached');

})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not NODE, reason="needs node")
def test_offline_reading_serves_the_shell_and_the_library_when_the_network_cannot(
        tmp_path):
    """Network-first, cache as the fallback, and nothing outside the two GET
    routes that make an offline reload worth doing at all."""
    f = tmp_path / "sw_test.js"
    f.write_text(SW_STUB + notes.SW_JS + SW_CHECKS)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ----------------------------------------------------------- removing a file

@pytest.mark.skipif(not NODE, reason="needs node")
def test_remove_needs_a_second_tap_and_only_then_fires(tmp_path):
    """removeBtn's own logic, driven with real objects rather than the shared
    discard proxy the rest of this harness uses -- document.createElement()
    there always hands back the SAME singleton, so a click handler assigned to
    it can never be read back and actually invoked. This is the one control in
    the app for which that distinction matters: everything else here only
    needs to prove a write happened, and this one needs to prove a tap did
    NOT fire a request."""
    fn = re.search(r"function removeBtn\(onConfirmed\) \{.*?\n\}", notes.PAGE, re.S)
    assert fn, "removeBtn must exist for this test to mean anything"
    script = f"""
const assert = require('node:assert');
let confirmed = 0;
const b = {{ className: '', textContent: '', disabled: false,
  classList: {{ add() {{}}, remove() {{}} }} }};
document.createElement = () => b;
{fn.group(0)}

(async () => {{
  const btn = removeBtn(async () => {{ confirmed++; }});
  assert.equal(btn.textContent, 'Remove');

  await btn.onclick({{ stopPropagation() {{}} }});
  assert.equal(confirmed, 0, 'the first tap must not fire the request');
  assert.equal(btn.textContent, 'Tap again to remove');
  assert.equal(btn.disabled, false, 'still tappable -- it is only armed');

  await btn.onclick({{ stopPropagation() {{}} }});
  assert.equal(confirmed, 1, 'the second tap is the one that fires it');
  assert.equal(btn.disabled, true);
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    f = tmp_path / "removebtn.js"
    f.write_text("const document = {};\n" + script)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not NODE, reason="needs node")
def test_removing_a_file_posts_the_right_shape_to_the_right_route(tmp_path):
    """Not just that a request goes out -- /remove takes an id, /remove-lecture
    takes (subject, title), and a bug that swapped the two would 404 or hit
    the wrong item silently."""
    file_fn = re.search(r"function fileRow\(u, s\) \{.*?\n\}", notes.PAGE, re.S)
    note_fn = re.search(r"function noteRow\(n, s\) \{.*?\n\}", notes.PAGE, re.S)
    rem_fn = re.search(r"function removeBtn\(onConfirmed\) \{.*?\n\}", notes.PAGE, re.S)
    vote_fn = re.search(r"function voteBtn\(u, answer\) \{.*?\n\}", notes.PAGE, re.S)
    assert file_fn and note_fn and rem_fn and vote_fn
    IS_IMAGE = re.search(r"const IS_IMAGE = [^\n]*", notes.PAGE).group(0)
    CMT = re.search(r"function commentsBtn\(id, name\) \{.*?\n\}",
                    notes.PAGE, re.S).group(0)
    RENAME = "\n".join(re.search(p_, notes.PAGE, re.S).group(0) for p_ in (
        r"function renameBtn\(current, save\) \{.*?\n\}",
        r"async function renameItem\(payload, btn\) \{.*?\n\}"))
    script = f"""
const assert = require('node:assert');
const document = {{}};
let ROLE = 'admin';
{LADDER}
let calls = [];
global.fetch = (url, init) => {{
  calls.push([url, JSON.parse(init.body)]);
  return Promise.resolve({{ ok: true, json: async () => ({{}}) }});
}};
async function refresh() {{}}
function busyDone() {{}}
function hue() {{ return 0; }}
const buttons = [];
document.createElement = (tag) => {{
  const el = {{ tag, className: '', textContent: '', disabled: false, innerHTML: '',
    style: {{ setProperty() {{}} }}, appendChild(c) {{ el._child = c; }},
    setAttribute() {{}}, classList: {{ add() {{}}, remove() {{}} }},
    querySelector: () => el, _child: null }};
  if (tag === 'button') buttons.push(el);
  return el;
}};
{RENAME}
{IS_IMAGE}
{CMT}
{vote_fn.group(0)}
{rem_fn.group(0)}
{file_fn.group(0)}
{note_fn.group(0)}

(async () => {{
  buttons.length = 0;
  fileRow({{name: 'x.pdf', path: 'p', id: 'mat-1', votes: 0, voted: false}}, {{code: 'MC1101'}});
  const fileRemove = buttons[buttons.length - 1];
  await fileRemove.onclick({{ stopPropagation() {{}} }});   // arm
  await fileRemove.onclick({{ stopPropagation() {{}} }});   // fire
  assert.deepStrictEqual(calls.pop(), ['/remove', {{id: 'mat-1'}}]);

  buttons.length = 0;
  noteRow({{title: 'a lecture', kind: 'lecture', md: ''}}, {{code: 'CY1107'}});
  const noteRemove = buttons[buttons.length - 1];
  await noteRemove.onclick({{ stopPropagation() {{}} }});
  await noteRemove.onclick({{ stopPropagation() {{}} }});
  assert.deepStrictEqual(calls.pop(),
    ['/remove-lecture', {{subject: 'CY1107', title: 'a lecture'}}]);
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    f = tmp_path / "removepayload.js"
    f.write_text(script)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------- grouped uploads

@pytest.mark.skipif(not NODE, reason="needs node")
def test_files_sharing_a_batch_render_as_one_row(tmp_path):
    """The whole point: eight photos of one handout are one row with one
    title, not eight rows named after whatever the camera called each one --
    and a file with no batch is completely unaffected."""
    grouped_fn = re.search(r"function groupedFileRows\(s\) \{.*?\n\}", notes.PAGE, re.S)
    group_fn = re.search(r"function fileGroupRow\(files, s\) \{.*?\n\}", notes.PAGE, re.S)
    file_fn = re.search(r"function fileRow\(u, s\) \{.*?\n\}", notes.PAGE, re.S)
    vote_fn = re.search(r"function voteBtn\(u, answer\) \{.*?\n\}", notes.PAGE, re.S)
    rem_fn = re.search(r"function removeBtn\(onConfirmed\) \{.*?\n\}", notes.PAGE, re.S)
    assert grouped_fn and group_fn and file_fn and vote_fn and rem_fn
    IS_IMAGE = re.search(r"const IS_IMAGE = [^\n]*", notes.PAGE).group(0)
    CMT = re.search(r"function commentsBtn\(id, name\) \{.*?\n\}",
                    notes.PAGE, re.S).group(0)
    RENAME = "\n".join(re.search(p_, notes.PAGE, re.S).group(0) for p_ in (
        r"function renameBtn\(current, save\) \{.*?\n\}",
        r"async function renameItem\(payload, btn\) \{.*?\n\}"))
    script = f"""
const assert = require('node:assert');
const document = {{}};
let ROLE = 'admin';
{LADDER}
let calls = [];
global.fetch = (url, init) => {{
  calls.push([url, JSON.parse(init.body)]);
  return Promise.resolve({{ ok: true, json: async () => ({{}}) }});
}};
async function refresh() {{}}
function busyDone() {{}}
function hue() {{ return 0; }}
const buttons = [];
function makeEl(tag) {{
  const el = {{
    tag, className: '', textContent: '', innerHTML: '', href: '', disabled: false,
    style: {{ setProperty() {{}} }}, setAttribute() {{}},
    classList: {{ add() {{}}, remove() {{}} }}, kids: [],
    appendChild(c) {{ el.kids.push(c); }},
    append(...a) {{ el.appended = (el.appended || []).concat(a); }},
    _sub: {{}},
    querySelector(sel) {{
      if (!el._sub[sel]) el._sub[sel] = makeEl(sel);
      return el._sub[sel];
    }},
  }};
  return el;
}}
document.createElement = (tag) => {{
  const el = makeEl(tag);
  if (tag === 'button') buttons.push(el);
  return el;
}};
{RENAME}
{IS_IMAGE}
{CMT}
{vote_fn.group(0)}
{rem_fn.group(0)}
{file_fn.group(0)}
{group_fn.group(0)}
{grouped_fn.group(0)}

(async () => {{

const s = {{
  code: 'MC1101',
  uploads: [
    {{name: 'page1.jpg', path: 'p1', id: 'm1', votes: 2, voted: false,
      by: 'Asha', batch: 'b1', title: 'Unit 3 handout'}},
    {{name: 'page2.jpg', path: 'p2', id: 'm2', votes: 0, voted: false,
      by: 'Asha', batch: 'b1', title: 'Unit 3 handout'}},
    {{name: 'solo.pdf', path: 'p3', id: 'm3', votes: 1, voted: false, by: 'Dia'}},
  ],
}};

const rows = groupedFileRows(s);
assert.equal(rows.length, 2, 'the batch collapses to one row; the solo file is a second');

const group = rows[0];
assert.equal(group.querySelector('b').textContent, 'Unit 3 handout',
             'the row is titled by the shared title, not either filename');
const smallKids = group.querySelector('small').kids || [];
const linkNames = smallKids.filter(k => k.tag === 'a').map(k => k.textContent);
assert.deepStrictEqual(linkNames, ['page1.jpg', 'page2.jpg'],
             'both files are individually named and individually linked');
assert.equal(smallKids.filter(k => k.tag === 'a')[0].href, 'p1');
assert.equal(smallKids.filter(k => k.tag === 'a')[1].href, 'p2');

// The vote anchors on the first file in the batch -- one control for the
// group, not one per file.
buttons.length = 0;
fileGroupRow(s.uploads.slice(0, 2), s);
// removeBtn is the LAST button (fileGroupRow appends vote, then remove);
// vote is whichever came before it.
const groupRemove = buttons[buttons.length - 1];
await groupRemove.onclick({{ stopPropagation() {{}} }});   // arm
await groupRemove.onclick({{ stopPropagation() {{}} }});   // fire
const fired = calls.filter(c => c[0] === '/remove');
assert.deepStrictEqual(fired.map(c => c[1]), [{{id: 'm1'}}, {{id: 'm2'}}],
             'removing a group removes every file it contains');

}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    f = tmp_path / "grouped.js"
    f.write_text(script)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr



@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_picked_file_is_renamed_before_it_uploads(tmp_path):
    """The picker's own handlers, driven with real objects: one document is
    offered for renaming (pre-filled, extension kept), several get one shared
    title, and a recording still goes straight up under its own name."""
    block = re.search(r"\nfileInput\.onchange = .*?\n(?=document\.getElementById\('opt-rec'\))",
                      notes.PAGE, re.S)
    assert block, "the picker block must exist for this test to mean anything"
    script = """
const assert = require('node:assert');
const el = () => ({ textContent: '', value: '', classList: { add() {}, remove() {} },
                    focus() {}, select() {} });
const fileInput = { multiple: true, files: [], value: '' };
const blabel = el(), btitle = el(), batchName = el();
const btns = { bcancel: el(), bgo: el() };
const document = { getElementById: id => btns[id] };
let pendingFiles = null, uploads = [], batches = [];
const upload = (f, name) => uploads.push(name);
const uploadBatch = (files, title) => batches.push([files.length, title]);
""" + block.group(0) + """
// One photo from the doc picker: a rename box, pre-filled from the camera's name.
fileInput.multiple = true; fileInput.files = [{ name: 'IMG_4821.jpg' }];
fileInput.onchange();
assert.equal(uploads.length, 0, 'nothing goes up before it is named');
assert.equal(blabel.textContent, 'Name this file');
assert.equal(btitle.value, 'IMG 4821', 'pre-filled, so keeping it is one tap');
btitle.value = 'Unit 3 Notes';
btns.bgo.onclick();
assert.deepStrictEqual(uploads, ['Unit 3 Notes.jpg'], 'typed name, original extension');

// A blank name is not an upload.
fileInput.files = [{ name: 'scan.pdf' }]; fileInput.onchange();
btitle.value = '   '; btns.bgo.onclick();
assert.equal(uploads.length, 1, 'an empty name uploads nothing');
btns.bcancel.onclick();
assert.equal(pendingFiles, null, 'cancel drops what was picked');

// Several: one shared title, handed to the batch uploader.
fileInput.files = [{ name: 'a.jpg' }, { name: 'b.jpg' }]; fileInput.onchange();
assert.equal(blabel.textContent, 'One title for all 2 files');
btitle.value = 'Handout'; btns.bgo.onclick();
assert.deepStrictEqual(batches, [[2, 'Handout']]);

// A recording skips the step entirely.
fileInput.multiple = false; fileInput.files = [{ name: 'lecture.m4a' }];
fileInput.onchange();
assert.equal(uploads[uploads.length - 1], 'lecture.m4a', 'audio goes straight up');
"""
    f = tmp_path / "rename.js"
    f.write_text(script)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_photo_shows_a_thumbnail_and_a_document_does_not(tmp_path):
    """Previews cost nothing extra: the photo already on the shelf is its own
    thumbnail, loaded lazily. A PDF, or a HEIC most browsers cannot draw,
    keeps the plain row."""
    fns = [re.search(p, notes.PAGE, re.S).group(0) for p in (
        r"const IS_IMAGE = [^\n]*", r"function voteBtn\(u, answer\) \{.*?\n\}",
        r"function removeBtn\(onConfirmed\) \{.*?\n\}", r"function fileRow\(u, s\) \{.*?\n\}",
        r"function fileGroupRow\(files, s\) \{.*?\n\}")]
    script = """
const assert = require('node:assert');
let ROLE = null;
""" + LADDER + """
function hue() { return 0; }
function makeEl(tag) {
  const el = { tag, innerHTML: '', className: '', textContent: '', kids: [], _sub: {},
    style: { setProperty() {} }, setAttribute() {}, classList: { add() {}, remove() {} },
    appendChild(c) { el.kids.push(c); }, append() {},
    querySelector(sel) { return el._sub[sel] || (el._sub[sel] = makeEl(sel)); } };
  return el;
}
const document = { createElement: makeEl };
""" + "\n".join(fns) + """
const s = {code: 'MC1101'};
const photo = fileRow({name: 'board.jpg', path: 'lib/board.jpg'}, s);
assert.ok(photo.innerHTML.includes('class="thumb"'), 'a photo gets a thumbnail');
assert.ok(photo.innerHTML.includes('loading="lazy"'), 'fetched only when scrolled to');
assert.equal(photo.querySelector('img').src, 'lib/board.jpg', 'the file is its own thumbnail');

for (const name of ['notes.pdf', 'IMG_1.HEIC']) {
  const row = fileRow({name, path: 'p'}, s);
  assert.ok(!row.innerHTML.includes('thumb'), name + ' keeps the plain row');
}

const group = fileGroupRow([{name: 'cover.pdf', path: 'c'}, {name: 'p2.png', path: 'lib/p2.png',
                             title: 'Handout'}], s);
assert.equal(group.querySelector('img').src, 'lib/p2.png', 'the group shows its first photo');
"""
    f = tmp_path / "thumbs.js"
    f.write_text(script)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not NODE, reason="needs node")
def test_rename_turns_the_row_into_an_edit_box_and_posts_the_new_name(tmp_path):
    fns = "\n".join(re.search(p_, notes.PAGE, re.S).group(0) for p_ in (
        r"function renameBtn\(current, save\) \{.*?\n\}",
        r"async function renameItem\(payload, btn\) \{.*?\n\}"))
    script = """
const assert = require('node:assert');
let rendered = 0, refreshed = 0, calls = [];
function render() { rendered++; }
async function refresh() { refreshed++; }
function busyDone() {}
global.fetch = (url, init) => { calls.push([url, JSON.parse(init.body)]);
  return Promise.resolve({ ok: true, json: async () => ({}) }); };
function makeEl(tag) {
  const el = { tag, kids: [], className: '', textContent: '', value: '', disabled: false,
    setAttribute() {}, focus() {}, select() {},
    appendChild(c) { el.kids.push(c); c.parentNode = el; },
    append(...cs) { cs.forEach(c => el.appendChild(c)); } };
  Object.defineProperty(el, 'innerHTML', { set() { el.kids = []; }, get() { return ''; } });
  return el;
}
const document = { createElement: makeEl };
""" + fns + """
(async () => {
const row = makeEl('div');
const b = renameBtn('IMG_4821', (v, btn) => renameItem({kind: 'material', id: 'm1', name: v}, btn));
row.appendChild(b);
b.onclick({ stopPropagation() {} });
const form = row.kids[0];
assert.equal(form.className, 'rename', 'the row becomes the edit box');
const [input, save, cancel] = form.kids;
assert.equal(input.value, 'IMG_4821', 'it starts from the current name');

input.value = 'IMG_4821';
await form.onsubmit({ preventDefault() {} });
assert.equal(calls.length, 0, 'the same name sends nothing');

input.value = '  Unit 3 notes ';
await form.onsubmit({ preventDefault() {} });
assert.deepStrictEqual(calls, [['/rename', {kind: 'material', id: 'm1', name: 'Unit 3 notes'}]]);
assert.equal(refreshed, 1, 'the shelf is refetched with the new name');
})().catch(e => { console.error(e); process.exit(1); });
"""
    f = tmp_path / "renamebtn.js"
    f.write_text(script)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not NODE, reason="needs node")
def test_an_anonymous_confession_is_never_drawn_with_initials(tmp_path):
    """The face is the one thing on the wall made of somebody's name, so it is
    the one thing that could undo what 0035 spent a column privilege on.

    A confession arrives with `by: null` and nothing else about its author --
    `authenticated` has no select on posts.author_id at all -- so the branch
    under test is what this page does with that null. Nothing it draws may
    carry a letter, a hue, or any other value read off the row: two letters in
    a colour are an identity, and a *stable* colour per confession is a
    fingerprint linking every confession one person ever wrote, which is
    worse than the name would have been.

    The second half is the other failure this could have: a face that is
    always blank is not a face. A named post has to come out with initials and
    a hue, or the first half passes on an empty feature.
    """
    fns = "\n".join(re.search(p, notes.PAGE, re.S).group(0) for p in (
        r"const nameHue = .*?\n\};", r"const initialsOf = .*?\n\};",
        r"function face\(name, size\) \{.*?\n\}",
        r"function ago\(then, now\) \{.*?\n\}", r"const agoNow = .*?\n",
        r"function agoSpan\(when\) \{.*?\n\}",
        r"function saidByFace\(name, when\) \{.*?\n\}",
        r"function postCard\(x, kind\) \{.*?\n\}"))
    fns = "let NOW_AT = 0;\n" + fns
    script = """
const assert = require('node:assert');
function makeEl(tag) {
  const el = { tag, kids: [], className: '', textContent: '', hues: [],
    setAttribute(k, v) { el['@' + k] = v; },
    style: { setProperty(k, v) { if (k === '--h') el.hues.push(String(v)); } },
    appendChild(c) { el.kids.push(c); return c; },
    append(...cs) { cs.forEach(c => el.appendChild(c)); } };
  Object.defineProperty(el, 'innerHTML', { set() { el.kids = []; }, get() { return ''; } });
  return el;
}
const document = { createElement: makeEl,
                   createTextNode: t => ({ tag: '#text', kids: [], textContent: t, hues: [] }) };
const NOW = 0;
function ago() { return '4 min ago'; }
function atLeast() { return false; }
function voteBtn() { return makeEl('button'); }
function acts(...b) { const r = makeEl('div'); b.filter(Boolean).forEach(x => r.appendChild(x)); return r; }
function writePost() {}
""" + fns + """
const walk = (el, out = []) => (out.push(el), el.kids.forEach(k => walk(k, out)), out);
const facesIn = el => walk(el).filter(n => String(n.className).split(' ').includes('face'));
const textIn = el => walk(el).map(n => String(n.textContent)).join(' ');
const huesIn = el => walk(el).flatMap(n => n.hues);

// What the server actually sends for a confession, and all of it.
const said = 'I have been pretending to understand thermodynamics since week two.';
const conf = postCard({id: 'a1b2c3d4-0000-4000-8000-000000000001', body: said,
                       by: null, at: 0, mine: false, votes: 3, voted: false, photos: []},
                      'confession');

const marks = facesIn(conf);
assert.equal(marks.length, 1, 'a confession gets exactly one mark');
const mark = marks[0];
assert.ok(mark.className.split(' ').includes('none'),
          'the anonymous branch, not the initials one');
assert.equal(mark.textContent, '', 'no letter of any kind inside the mark');
assert.deepStrictEqual(mark.hues, [], 'no hue: a stable colour per author IS the author');
assert.deepStrictEqual(huesIn(conf), [], 'and nothing else on the row is coloured by one either');

// Nothing anywhere in the row is a short run of capitals -- which is what
// initials are, and what anybody adding one later would add.
for (const node of walk(conf)) {
  const t = String(node.textContent).trim();
  assert.ok(!/^[A-Z\\u00C0-\\u024F]{1,3}$/.test(t),
            'something on an anonymous row reads as initials: ' + JSON.stringify(t));
}
const shown = textIn(conf);
assert.ok(shown.includes('Anonymous'), 'the honest word is still written');
assert.ok(shown.includes(said), 'and the confession itself is still on the page');

// The other half: a named post has to actually get a face, or the above is
// a test that a broken feature stays broken.
const post = postCard({id: 'p2', body: 'Library is open till 11.', by: 'Priya Nair',
                       at: 0, mine: false, votes: 0, voted: false, photos: []}, 'feed');
const named = facesIn(post);
assert.equal(named.length, 1, 'a named post gets one face');
assert.equal(named[0].textContent, 'PN', 'and it is her initials');
assert.ok(!named[0].className.split(' ').includes('none'), 'not the anonymous mark');
assert.equal(named[0].hues.length, 1, 'coloured from the name');
// Deterministic, and the same name is the same colour every time it is drawn.
assert.equal(nameHue('Priya Nair'), nameHue('Priya Nair'));
assert.notEqual(nameHue('Priya Nair'), nameHue('Rohan Deshmukh'));
"""
    f = tmp_path / "anon.js"
    f.write_text(script)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_day_timeline_sizes_classes_merges_labs_and_shows_the_gaps(tmp_path):
    """Section I's real Monday: Chem 9-10, Maths 10-11, then a BEEE lab from
    11 to 12:55 and again 2:30-4:25 either side of lunch, then IKS 4:30. And a
    Wednesday with a free first period."""
    seg = notes.PAGE[notes.PAGE.index("const PERIOD_TIMES"):notes.PAGE.index("function blockWithTimeline")]
    script = """
const assert = require('node:assert');
const made = [];
function makeEl(tag) {
  const el = { tag, className: '', textContent: '', style: { setProperty() {} }, _sub: {},
    setAttribute(k, v) { el['@' + k] = v; }, appendChild(c) { made.push(c); },
    querySelector(q) { return el._sub[q] || (el._sub[q] = makeEl(q)); } };
  Object.defineProperty(el, 'innerHTML', { set() {}, get() { return ''; } });
  return el;
}
const document = { createElement: makeEl };
const NAMES = {CY1107: 'Engg. Chemistry', MC1101: 'Maths 1', EE1125: 'BEEE Lab', HS1112: 'IKS'};
const subjectOf = c => NAMES[c] ? {name: NAMES[c]} : null;
const hue = () => 0, go = () => {}, attToday = () => '2026-09-07';
let SLOTS = [];
const slotsFor = () => SLOTS;
""" + seg + """
SLOTS = [{period: 1, code: 'CY1107'}, {period: 2, code: 'MC1101'},
         {period: 3, code: 'EE1125'}, {period: 4, code: 'EE1125'},
         {period: 5, code: 'EE1125'}, {period: 6, code: 'EE1125'}, {period: 7, code: 'HS1112'}];
const b = timelineBlocks('2026-09-07');
assert.deepStrictEqual(b.map(x => [x.code, hhmm(x.start), hhmm(x.end)]), [
  ['CY1107', '9:00', '9:55'], ['MC1101', '10:00', '10:55'],
  ['EE1125', '11:00', '12:55'],          // periods 3+4 merged into one block
  ['EE1125', '2:30', '4:25'],            // lunch splits the lab, it is not bridged
  ['HS1112', '4:30', '5:25']]);
assert.equal(long(115), '1 h 55 min');

made.length = 0;
dayTimeline('2026-09-07');
const evs = made.filter(e => e.className.startsWith('ev'));
assert.equal(evs.length, 5, 'one block per class, labs merged');
assert.equal(evs[2]._sub.small.textContent, '11:00–12:55 · 1 h 55 min · EE1125',
             'a block says its time, its length and its code');
assert.ok(made.some(e => e.className === 'lunch'), 'lunch is on the day');
assert.ok(!made.some(e => e.className === 'free'), 'a packed Monday has no free gap');

// Wednesday: nothing at 9, IKS at 10 -- the timeline starts at the first class,
// so there is no 'free' before it; but a hole in the middle is shown.
SLOTS = [{period: 2, code: 'HS1112'}, {period: 4, code: 'MC1101'}];
made.length = 0;
dayTimeline('2026-09-09');
const free = made.filter(e => e.className === 'free').map(e => e.textContent);
assert.deepStrictEqual(free, ['Free · 1 h 5 min'], 'the empty 11 o\\'clock slot is visible');

SLOTS = [];
assert.equal(dayTimeline('2026-09-13'), null, 'a day with no classes draws no timeline');
"""
    f = tmp_path / "timeline.js"
    f.write_text(script)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_stylesheet_is_inlined_not_fetched():
    """Tailwind is compiled and inlined, so the app paints with no network.

    A CDN script would be ~300KB before first paint and a blank app offline;
    a <link> to a built sheet would be a second round trip. Neither is here.
    """
    styles = re.findall(r"<style>(.*?)</style>", notes.PAGE, re.S)
    assert len(styles) == 2, "the token block, then Tailwind's sheet"
    assert "--accent:" in styles[0], "tokens stay in the FIRST style block"
    assert len(styles[1].strip()) > 500, "the compiled sheet is inlined and real"
    assert "__CSS__" not in notes.PAGE
    assert "cdn.tailwindcss.com" not in notes.PAGE
    assert '<link rel="stylesheet" href="/' not in notes.PAGE


def test_the_page_is_written_on_one_scale():
    """Six type sizes, five weights, four radii -- and the reasons for each.

    Drift is what eleven sizes and eleven radii looked like before, and it
    arrives one declaration at a time. 30px is the FAB's single glyph and 50%
    is a circle, neither of which is a step on any scale.

    40px/800 is the display step, added deliberately on 2026-09-14. It is the
    size GATE_PAGE's headline lands on at 375px -- clamp(2.5rem,9.5vw,4.4rem)
    sits on its 2.5rem floor at that width -- and it takes the app's
    largest-to-body ratio to 2.5x, which is the landing page's ratio. Before
    it the app topped out at 26px, a ratio of 1.6x, and read as a different
    product from its own front door. It is spent in four places and no more:
    the screen's own name, the app's name on Home, the points total, and the
    result of a practice run.
    """
    tokens = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    scale = {"11px", "13px", "16px", "20px", "26px", "30px", "40px"}
    sizes = set(re.findall(r"font-size:(\d+px)", tokens))
    assert sizes <= scale, f"off the type scale: {sorted(sizes - scale)}"
    # One rule, not a size anybody may reach for: a second declaration of it is
    # exactly the drift this test exists to catch.
    assert tokens.count("font-size:40px") == 1, \
        "40px is the display step and belongs to one rule"
    weights = set(re.findall(r"font-weight:(\d+)", tokens))
    assert weights <= {"400", "500", "600", "700", "800"}, \
        f"off the weight scale: {weights}"
    assert tokens.count("font-weight:800") == 1, \
        "800 is the display weight and belongs to that same one rule"
    radii = set(re.findall(r"border-radius:(\d+px)(?![\d ])", tokens))
    # 2px is a progress bar's cap, 4px the focus ring, 5px half of a 10px bar
    # and 999px a pill -- shapes, not corners.
    assert radii <= {"2px", "4px", "5px", "7px", "11px", "14px", "18px", "999px"}, \
        f"a fifth corner: {sorted(radii)}"


# --------------------------------------- the composer, and what draws it

def test_no_screen_in_this_app_shows_a_raw_file_input():
    """"Choose files / No file chosen" is the one control here that nobody
    under twenty has ever seen in anything else they use, and a dashed grey
    slab of it sat in the middle of the Community composer.

    appearance:none does nothing to a file input -- the slab is shadow DOM --
    so the input is hidden and a real control drives it. The select keeps its
    native wheel, which is still the fastest picker on a phone, and loses the
    OS chevron for the one this app draws everywhere else.
    """
    tokens = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    assert "::file-selector-button" not in tokens, \
        "a styled file button is still a file button: hide it and drive it"
    assert "input[type=file]" not in tokens, "nothing styles a visible file input"
    picker = re.search(r"function wallComposer\(kind\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "picker.hidden = true" in picker, "the file input is hidden"
    assert "picker.click()" in picker, "and opened by a control of our own"
    assert re.search(r"\.compose select,\.askbox select\{[^}]*appearance:none", tokens), \
        "the OS chevron goes; the wheel behind it stays"


def test_community_opens_on_the_feed_and_its_composer_lives_behind_the_plus():
    """The tab used to open on an empty form with the feed underneath it, so
    the first thing a student saw on Community was work rather than content.

    The composer is a level with a URL now, like Campus's three and the notice
    board's -- which is what keeps the back gesture from dropping what was
    typed.
    """
    wall = re.search(r"function wallSection\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "wallComposer" not in wall, "the feed screen must not draw the composer"
    community = re.search(r"async function renderCommunity\(\) \{.*?\n\}",
                          notes.PAGE, re.S).group(0)
    assert "wallComposer" in community and "view.compose" in community
    assert "WALL_COMPOSE = {say: 'feed', confess: 'confession'}" in notes.PAGE
    assert "go('community', wallOn === 'feed' ? 'say' : 'confess')" in notes.PAGE, \
        "the + is what opens it"


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_plus_writes_on_community_and_adds_everywhere_else(tmp_path):
    """Words on the wall are not an upload, so the + is never locked there --
    a student may post one and may not add to the library, and the same button
    has to say both things depending on where it is standing.
    """
    checks = """
ROLE = 'student';
location.hash = '#community';
route();
assert.equal(view.compose, null, 'Community opens on the feed, not on a form');
assert.equal(fabEl.hidden, false, 'and the + is there');
assert.equal(fabEl.className, '', 'a student may post words, so it is not locked');

location.hash = '#classes';
route();
assert.equal(fabEl.className, 'locked', 'a student still may not add to the library');

location.hash = '#community';
route();
fabEl.onclick();
assert.equal(location.hash, '#community/say', 'the + opens the composer');
route();
assert.deepStrictEqual([view.tab, view.compose], ['community', 'say']);
assert.equal(fabEl.hidden, true, 'and takes itself off the form it just opened');

wallOn = 'confession';
location.hash = '#community';
route();
fabEl.onclick();
assert.equal(location.hash, '#community/confess',
             'the + writes the list you are reading');
"""
    f = tmp_path / "fab.js"
    f.write_text(STUB + SCRIPT.replace("__DATA__", json.dumps(DATA_FIXTURE)) + checks)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_now_line_passes_behind_the_words_it_used_to_cross():
    """At 11:41 the current-time rule ran straight through "11:00-12:55 - 1 h
    55 min - EE1125" and neither the time nor the code could be read. The words
    knock it out and it carries on either side of them, which is what the hour
    labels already do to the hour rules.
    """
    tokens = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    knock = re.search(r"\.tl \.ev b,\.tl \.ev small\{([^}]*)\}", tokens)
    assert knock, "the words in a block must knock the now line out"
    for part in ("background:var(--fill)", "z-index:3", "width:fit-content"):
        assert part in knock.group(1), f"the knockout needs {part}"
    now = re.search(r"\.tl \.now\{([^}]*)\}", tokens).group(1)
    assert "z-index:2" in now, "and the line still paints over the block's fill"
    assert re.search(r"\.tl \.ev\{[^}]*--fill:color-mix", tokens), \
        "a knockout can only be drawn in a fill that is a known colour"


def test_every_class_on_the_day_is_named_in_the_same_ink():
    """The two classes that were over were grey and the one running was black,
    so three blocks forty-five minutes apart read as three different kinds of
    thing. Over is said in the block and never in the words.
    """
    tokens = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    done = re.search(r"\.tl \.ev\.done\{([^}]*)\}", tokens)
    assert done, "a class that has happened still says so"
    assert "color:" not in done.group(1).replace("border-left-color:", ""), \
        "it says so in the fill and the rule, not in the ink"
    assert not re.search(r"\.tl \.ev\.done b", tokens), \
        "no rule may recolour one block's title and not another's"


# ------------------------------------------ the reading screen's masthead

def test_the_reading_screen_says_which_lecture_it_is():
    """The header said "< MC1101" on the left and MC1101 on a chip on the right
    -- the whole of it spent on one string, twice -- while the lecture's own
    title appeared nowhere on the screen. Under that sat 150px of nothing.
    """
    assert '<header class="mast" id="mast" hidden>' in notes.PAGE
    open_note = re.search(r"function openNote\(n, s\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "rtitle.textContent = n.title;" in open_note, "the title goes on the screen"
    assert "mast.hidden = false;" in open_note
    assert "backBtn.textContent = '‹ ' + s.name;" in open_note, \
        "back says the subject by name; the chip beside it is the code"
    assert "backBtn.textContent = '‹ ' + s.code" not in notes.PAGE, \
        "the code must not be printed twice in one header"
    closing = re.search(r"function closeRead\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "mast.hidden = true;" in closing, "nothing open, nothing to head"
    assert "if (own && own.tagName === 'H1') own.remove();" in open_note, \
        "a note that carries its own title must not print it twice"


def test_a_list_in_a_note_still_has_its_bullets():
    """Tailwind's preflight sets list-style:none on every ul and ol, which took
    the markers off every list in every lecture note and left the indent
    behind. A key-points list read as four stranded paragraphs.
    """
    tokens = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    assert "article ul,.ann .md ul{list-style:disc}" in tokens
    assert "article ol,.ann .md ol{list-style:decimal}" in tokens


def test_a_heading_in_a_note_is_not_the_colour_a_link_is():
    """Every '###' in every note was set in the accent, which is what a link
    is and what nothing else in this app is -- so headings read as tappable
    and tapping them did nothing."""
    tokens = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    h3 = re.search(r"article h3\{([^}]*)\}", tokens).group(1)
    assert "var(--accent)" not in h3, "a heading is not a link"
    assert "font-weight:700" in h3, "weight is what makes it a heading instead"
    assert "article a{color:var(--accent)}" in tokens or "article .md a" in tokens \
        or "var(--accent)" in re.search(r"\.ann \.md a\{([^}]*)\}", tokens).group(1), \
        "the accent still belongs to links"


def test_the_plus_button_has_room_reserved_for_it_everywhere():
    """It is 58px of accent fixed over the page and it sat on live content on
    16 of 38 screens -- a confession's third line, a card's buttons, the
    paragraph under a composer's Post button. The clearance is one token
    now, used by both scrolling columns, and the button takes itself away
    while a screen is moving under it and on any screen with a bar of its own.
    """
    tokens = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    assert "--fabclear:calc(142px + env(safe-area-inset-bottom));" in tokens
    assert "#nav{padding-bottom:var(--fabclear)}" in tokens
    assert re.search(r"#doubts\{[^}]*var\(--fabclear\)", tokens), \
        "the reading screen's tail needs the same clearance"
    assert not re.search(r"padding[^;{}]*\b142px", tokens), \
        "one clearance, written once, and it is the token"
    assert "body.reading #fab{display:none}" in tokens, \
        "the reading screen has the dock; it does not need a second bar"
    assert "body.fabaway #fab{" in tokens
    assert "document.body.classList.toggle('fabaway'," in notes.PAGE


def test_the_newest_lectures_are_on_the_screen_that_lists_the_subjects():
    """At 1440x900 the app was a 320px column and 1120px of near-black with a
    small grey box in it reading "Pick a lecture to start reading", under a bar
    of four buttons -- Save, Share, Download, Print -- that acted on nothing.
    The pane earned itself by carrying "Added most recently", the one list the
    column beside it did not already show. There is no second pane now, so the
    list stands on the screen it was always about, and the pane with nothing in
    it is not written at all.
    """
    tokens = re.search(r"<style>\n(.*?)\n</style>", notes.PAGE, re.S).group(1)
    assert "body:not(.reading) .dock{display:none}" in tokens, \
        "a bar of controls with nothing to control is worse than no bar"
    assert "body:not(.reading) .dock{display:flex}" not in tokens
    subjects = re.search(r"function renderSubjects\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "block('Added most recently'" in subjects, \
        "the newest lectures are on the screen that lists the subjects"
    assert "go('classes', x.s.code, x.n.title)" in subjects, "each row opens its lecture"
    # The list is lectures and the block above it is subjects. A recent list
    # built off the same DATA.map as the subject rows would be the same list
    # twice, which is what made the pane worth removing in the first place.
    assert "b.onclick = () => go('classes', s.code);" in subjects, \
        "and the subject rows it sits under are still there"
    closing = re.search(r"function closeRead\(\) \{.*?\n\}", notes.PAGE, re.S).group(0)
    assert "body.innerHTML = '';" in closing, \
        "closing a note must empty the pane, not leave its markup lying in it"
    assert "doubtsBox.innerHTML = '';" in closing, \
        "and take the closed note's thread with it"
    # The pane that had something to say is gone, and so is everything that
    # dressed it: #read is hidden on every layout until a note is open.
    assert "function emptyRead" not in notes.PAGE
    assert ".empty{" not in tokens


# ------------------------------------------------- what a paper says it holds

ANSWERS_CHECKS = """
const assert = require('assert');

// The four the portal actually has, spelled the four ways it spells them.
assert.equal(answersIn({title: 'Mini Test 2024-25 Sem 2 Section B (with Answer Key)'}),
             'with answers');
assert.equal(answersIn({title: 'Quiz 2024-25 Sem 2 Section F with Solution'}),
             'with answers');
assert.equal(answersIn({title: 'End Term 2024-25 Sem 2 MS'}), 'marking scheme');
assert.equal(answersIn({title: 'Marking Scheme 2023'}), 'marking scheme');

// A marking scheme is answers ONLY and must not be read as a paper carrying
// them: the two words mean different things to somebody revising.
assert.notEqual(answersIn({title: 'End Term 2024-25 Sem 2 MS'}), 'with answers');

// And the ninety-six that hold no answers at all say nothing, rather than
// promising a key that is not in the file.
for (const t of ['End Term 2025-26 Sem 1', 'Mid Term Section G Sem 1 2025-26 ',
                 'MINI Test 2025-26 Sem 1 Sec B', 'End Term 2022-23 Sem 1'])
  assert.equal(answersIn({title: t}), null, t + ' promises nothing');

// 'MS' is a word here, not two letters inside one. These are the titles that
// prove it: each holds the letters m-s and none of them is a marking scheme.
assert.equal(answersIn({title: 'Signals and Systems End Term 2024-25'}), null);
assert.equal(answersIn({title: 'Exams paper 2023'}), null);
assert.equal(answersIn({title: 'Mechanisms Mid Term Sem 1'}), null);
assert.equal(answersIn({title: ''}), null);
assert.equal(answersIn({}), null);
"""


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_paper_only_claims_answers_when_its_name_says_so(tmp_path):
    """The archive has no column for answers -- the title is the whole of what
    the portal ever recorded. So this reads titles, and it must not over-read
    them: a paper wrongly marked 'with answers' sends somebody revising to a
    file that does not have what they opened it for."""
    fn = re.search(r"\nfunction answersIn\(p\) \{.*?\n\}", SCRIPT, re.S)
    assert fn, "answersIn has been renamed or removed"
    f = tmp_path / "answers_test.js"
    f.write_text(fn.group(0) + ANSWERS_CHECKS)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ------------------------------------------------------------ campus events

CAMPUS_CHECKS = r"""
const assert = require('node:assert');
const T = '2026-09-07';
const one = {id: 'e1', title: 'Robotics; demo, night', date: '2026-09-08',
             ends: '2026-09-08', venue: 'LT-1', society: 'Robotics', blurb: 'Bring\nfriends'};
const fest = {id: 'e2', title: 'Fest', date: '2026-09-05', ends: '2026-09-09',
              society: 'Drama'};
const far = {id: 'e3', title: 'Far', date: '2026-10-01', ends: '2026-10-01', society: 'Robotics'};

// Today is an event that is on today, including a fest that started last week.
assert.ok(eventShows(fest, 'today', T));
assert.ok(!eventShows(one, 'today', T));
assert.ok(eventShows(one, 'week', T));
assert.ok(!eventShows(far, 'week', T));
assert.ok(eventShows(far, 'Robotics', T) && !eventShows(fest, 'Robotics', T));
assert.ok(eventShows(far, 'all', T));

// The .ics is one all-day VEVENT with an exclusive end, and the text is escaped
// the way RFC 5545 wants it rather than pasted in.
const ics = icsFor(one);
assert.ok(ics.startsWith('BEGIN:VCALENDAR\r\n'));
assert.ok(ics.includes('DTSTART;VALUE=DATE:20260908\r\nDTEND;VALUE=DATE:20260909\r\n'));
assert.ok(ics.includes('SUMMARY:Robotics\\; demo\\, night\r\n'));
assert.ok(ics.includes('LOCATION:LT-1\r\n'));
assert.ok(ics.includes('DESCRIPTION:Robotics. Bring\\nfriends\r\n'));
assert.ok(ics.endsWith('END:VCALENDAR\r\n'));
const multi = icsFor(fest);
assert.ok(multi.includes('DTEND;VALUE=DATE:20260910'), 'a fest ends the morning after its last day');
assert.ok(!multi.includes('LOCATION:'), 'no venue, no LOCATION line');
"""


@pytest.mark.skipif(not NODE, reason="needs node")
def test_campus_events_filter_and_export_by_the_calendar_rules(tmp_path):
    """Today includes a fest already running; the week is the next seven days;
    the .ics a phone imports is all-day with the exclusive DTEND the standard
    asks for, and commas in a title do not split a field."""
    src = "".join(re.search(p, SCRIPT, re.S).group(0) for p in (
        r"const isoDay = .*?;\n", r"const shiftDay = .*?;\n", r"\nfunction icsFor\(e\) \{.*?\n\}",
        r"\nfunction eventShows\(e, filter, today\) \{.*?\n\}"))
    f = tmp_path / "campus_test.js"
    f.write_text(src + CAMPUS_CHECKS)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_tour_starts_once_and_the_profile_can_start_it_again():
    """First sign-in on a device, never over a shared link, and Help on the
    profile brings it back. Done-ness is one localStorage key."""
    page = notes.PAGE
    assert "if (d.role && /^(#home)?$/.test(location.hash)) setTimeout(maybeTour);" in page
    assert "localStorage.setItem(TOUR_KEY, 'done')" in page
    assert "tour.onclick = startTour;" in page and "block('Help', [tour]);" in page
