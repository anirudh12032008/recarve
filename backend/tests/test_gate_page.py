"""The gate: the three screens you see before you are in the library.

GATE_PAGE is a second, much smaller UI -- join, wait, blocked, admin -- and it
never got the passes PAGE got two thousand lines earlier. It is also the only
screen a stranger sees, and the admin screen behind it is the one place roles
can be changed at all, so what it does when the server says no matters more
here than anywhere.

Ceiling, named rather than designed around: contrast is computed from the
tokens, not measured in a browser, so a colour written straight into a rule
instead of a token is invisible to it. The `color:#fff` that started this is
caught by the token tests only because the rule now reads var(--accent-fg).
"""

import pathlib
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402

NODE = shutil.which("node")
CSS = re.search(r"<style>\n(.*?)\n</style>", notes.GATE_PAGE, re.S).group(1)

# Two :root blocks and no more: the light one, then the dark override.
ROOTS = re.findall(r":root\{([^}]*)\}", CSS)
LIGHT, DARK = ({k: v for k, v in re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", r)}
               for r in ROOTS)
DARK = {**LIGHT, **DARK}      # the dark block only restates what changes


def rule(selector):
    """The declarations inside the first rule with this selector."""
    return CSS.split(selector + "{", 1)[1].split("}", 1)[0]


# ---------------------------------------------------------------- contrast


def _linear(channel):
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour):
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def contrast(a, b):
    dark, light = sorted((luminance(a), luminance(b)))
    return (light + 0.05) / (dark + 0.05)


def test_the_helper_agrees_with_the_published_numbers():
    """WCAG's own extremes, so a wrong ratio below is the page's, not this."""
    assert round(contrast("#000", "#fff"), 2) == 21.0
    assert round(contrast("#777", "#fff"), 2) == 4.48


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("fg,bg", [
    # The accent is the Join button and the Approve button. It goes pale in the
    # dark, and white on the pale one is 2.8:1 -- which is why the foreground
    # is a token that flips rather than a literal #fff.
    ("--accent-fg", "--accent"),
    ("--fg", "--bg"),
    ("--mut", "--bg"),
    ("--err", "--bg"),
    # The admin slab: Approve, Copy join link and the Admin tag. Both halves
    # are written twice, once per theme, and a value copied from the wrong
    # block is near-white on near-white.
    ("--admin-fg", "--admin"),
    # button.ghost is inked text and an inked ring straight on the ground.
    ("--admin", "--bg"),
])
def test_every_pair_on_the_gate_is_readable_in_both_themes(mode, fg, bg):
    tokens = LIGHT if mode == "light" else DARK
    ratio = contrast(tokens[fg], tokens[bg])
    assert ratio >= 4.5, f"{fg} on {bg} in {mode} is only {ratio:.2f}:1"


# ------------------------------------------------------------------- type


def test_no_font_shorthand_passes_inherit_off_as_a_family():
    """`inherit` is a CSS-wide keyword, excluded from <family-name>, so
    `font:600 1rem/1 inherit` does not parse and the whole declaration is
    dropped -- leaving a 44px-tall button rendering 13.3px Arial 400. Whole-
    value `font:inherit` is fine and is not what this looks for."""
    bad = re.findall(r"font:\s*(?!inherit\s*[;}])[^;}]*\binherit\b", CSS)
    assert not bad, f"invalid font shorthand, silently dropped: {bad}"


@pytest.mark.parametrize("selector", ["button", "input", ".row select"])
def test_every_control_takes_the_page_font(selector):
    """Form controls do not inherit font-family. Set only a size and the button
    beside the select disagrees with it on both size and family."""
    assert "font:inherit" in rule(selector), \
        f"{selector} falls back to the UA default without it"


@pytest.mark.parametrize("selector", [".row select", "input"])
def test_every_control_has_a_boundary_you_can_see(selector):
    """WCAG 1.4.11 wants 3:1 for the edge of a control. --line is 1.24:1 on the
    ground here, and every control on these screens draws no background at all
    -- so the picker that can promote anybody to admin, and the password boxes
    on /login and the forced-change screen, read as the static text beside
    them."""
    token = re.search(r"border:1px solid var\((--[\w-]+)\)", rule(selector)).group(1)
    for mode, tokens in (("light", LIGHT), ("dark", DARK)):
        ratio = contrast(tokens[token], tokens["--bg"])
        assert ratio >= 3, f"{selector}'s only edge is {ratio:.2f}:1 in {mode}"


def test_the_admin_screen_has_a_way_back():
    """It is reached by leaving the app entirely -- two full-page navigations
    out of the SPA -- into a page with no tab bar and no header. In an
    installed PWA there was nothing on screen to tap."""
    assert 'href="/"' in notes.ADMIN_BODY, "no way back to the library"


def test_the_way_back_is_big_enough_to_hit():
    """A bare anchor inherits body's 16px/1.5 and gets a ~19px box. Every other
    control on the gate sets min-height:44px; this one is the sole navigation
    off /admin, so it is the one that matters most."""
    assert "min-height:44px" in rule("main>p>a"), \
        "the only way off /admin is a 19px tap target"


def test_a_name_with_no_spaces_in_it_cannot_push_the_gate_sideways():
    """Names are whatever the joiner typed, up to 80 characters, no format
    check -- and an email address in the Name box is the ordinary way one
    arrives with no break in it."""
    assert "overflow-wrap:break-word" in rule("body")
    # body's rule wraps the text but does not shrink a flex item's automatic
    # minimum size, so the name in .row still pushed the row -- and the page --
    # sideways: 170px of horizontal overflow at 390px wide on a 40-char name.
    assert "overflow-wrap:anywhere" in rule(".row b"), \
        "the name in a row is a flex item and needs its own"


# --------------------------------------------------------- the join form


def test_the_form_asks_for_the_four_things_and_shows_the_fifth():
    body = notes.join_body()
    for field in ('id="nm"', 'id="roll"', 'id="ph"', 'id="code"'):
        assert field in body, f"{field} is not on the form"
    assert 'id="sec" value="Section I" readonly' in body, \
        "there is one section; a box you can type in invites the wrong one"
    assert "phone: $('ph').value" in body, "collected but never sent"


def test_the_number_field_opens_a_keypad_and_does_not_zoom_the_page():
    """type=tel is the keypad. The 16px comes from font:inherit on input --
    anything smaller and iOS zooms the whole form on focus and the joiner is
    left scrolling sideways with one thumb."""
    body = notes.join_body()
    assert 'type="tel"' in body and 'inputmode="tel"' in body
    assert "font:inherit" in rule("input")


def test_a_form_nobody_was_linked_to_carries_no_code_and_no_name():
    body = notes.join_body()
    assert 'id="code" required autocomplete="off" autocapitalize="off" value=""' in body
    assert "Invited by" not in body, "an empty name is worse than no line"
    assert "You need the invite code" in body, "say where the code comes from"


def test_a_form_that_came_from_a_link_does_not_ask_for_what_it_already_has():
    """"You need the invite code" over a box that already holds one is how a
    form reads as broken before it has been used."""
    body = notes.join_body("392b9ea7")
    assert "You need the invite code" not in body
    assert "already in" in body


def test_the_inviter_is_named_when_the_database_knows_one():
    assert '<p class="by">Invited by Anirudh</p>' in notes.join_body("392b9ea7", "Anirudh")


@pytest.mark.parametrize("hostile", [
    '"><script>alert(1)</script>',
    "' onfocus=alert(1) autofocus '",
    "</form><form action=//evil",
])
def test_neither_the_code_nor_the_name_can_get_out_of_the_page(hostile):
    """The code is a query parameter a stranger writes and the page reflects.
    The name is whatever an admin typed into this same form months ago."""
    body = notes.join_body(hostile, hostile)
    assert hostile not in body, "reflected verbatim"
    assert "<script>alert" not in body
    value = body.split('id="code"')[1].split('value="')[1].split('"')[0]
    assert not set(value) & set("<>'"), f"attribute is escapable: {value}"


# ------------------------------------------ what the admin screen does on a no

ADMIN_SCRIPT = re.search(r"<script>\n(.*)\n</script>", notes.ADMIN_BODY, re.S).group(1)

STUB = """
const assert = require('node:assert');
const alerts = [];
const alert = m => alerts.push(m);
// Enough DOM to answer three questions: what got built, what it says, and
// what its buttons post. `kids` is the tree, `text` reads it back, and
// setting innerHTML clears it the way the real one does -- load() re-renders
// every list on every action, so a stub that only ever appends double-counts.
const made = [];
const el = () => {
  const e = {
    textContent: '', className: '', value: '', selected: false, aria: null,
    disabled: false, onclick: null, onchange: null, kids: [], q: {},
    setAttribute(k, v) { if (k === 'aria-label') e.aria = v; },
    appendChild(c) { e.kids.push(c); return c; },
    append(...c) { e.kids.push(...c); },
    querySelector(sel) { return e.q[sel] || (e.q[sel] = e.appendChild(el())); },
    get text() { return e.textContent + e.kids.map(k => k.text).join(' '); },
  };
  let html = '';
  Object.defineProperty(e, 'innerHTML',
    {get: () => html, set(v) { html = v; e.kids.length = 0; e.q = {}; }});
  made.push(e);
  return e;
};
const byId = {list: el(), members: el(), counts: el(), invite: el(), reports: el()};
const document = {getElementById: id => byId[id] || el(), createElement: el};
// null hangs up the way a dead server does: fetch itself rejects.
let reply = {ok: true, status: 200, json: async () => ({pending: [], members: [], me: 'x'})};
// Every button on this screen posts. What it posts is the whole difference
// between Reject and Approve, so the payloads are kept and read back.
const posts = [];
const fetch = (path, init) => {
  if (init && init.method === 'POST') {
    posts.push({path, body: JSON.parse(init.body)});
    return Promise.resolve({ok: true, status: 200, json: async () => ({})});
  }
  return reply ? Promise.resolve(reply) : Promise.reject(new TypeError('Failed to fetch'));
};
const answer = (ok, body) => ({ok, status: ok ? 200 : 503, json: async () => body});
const location = {origin: 'https://notes.workwithani.tech'};
const navigator = {clipboard: {writeText: async () => {}}};
"""

CHECKS = """
(async () => {
  // The script fires its own load() on the way in; let that one land before
  // driving it, or its answer overwrites the one under test.
  await new Promise(setImmediate);

  // Both of these come out of this server's own gate: 503 when Postgres is
  // down, 403 for an admin a second admin has just demoted. Neither body has
  // a `pending` key, and reading one used to throw after the placeholder was
  // already wiped -- leaving the admin on a screen with nothing on it at all.
  for (const r of [answer(false, {error: 'admins only'}),
                   answer(false, {error: 'the library is offline'}),
                   null]) {
    byId.list.textContent = 'loading…';
    byId.members.textContent = 'loading…';
    reply = r;
    await load();
    assert.match(byId.list.textContent, /Could not load/,
                 'a refused /pending must leave a reason, not a blank screen');
    assert.match(byId.members.textContent, /Could not load/,
                 'and the members list is just as empty without it');
  }

  reply = answer(true, {pending: [], members: [], me: 'x'});
  await load();
  assert.equal(byId.list.textContent, 'Nobody waiting.',
               'and a good answer still renders');

  // ---- one good payload, and the wirings a one-word typo silently inverts.
  reply = answer(true, {
    me: 'me-1', now: 1000, invite: 'ab/cd',
    counts: {pending: 1, members: 3, blocked: 1, reports: 1},
    pending: [{id: 'p-1', name: 'Joiner', roll_no: 'R1', phone: '9', asked: 990}],
    members: [{id: 'me-1', name: 'Me', role: 'admin', status: 'approved', roll_no: 'R0'},
              {id: 'm-2', name: 'Other', role: 'student', status: 'approved', roll_no: 'R2',
               password: 'member-password'},
              {id: 'm-3', name: 'Out', role: 'student', status: 'blocked', roll_no: 'R3'}],
    reports: [{id: 'rep-1', material_id: 'mat-9', filename: 'f.pdf', status: 'open',
               subject: 'CY1107', reason: 'wrong', by: 'Someone', at: 900}],
  });
  made.length = 0;
  await load();
  // Taken now: every button re-runs load(), so a later count would see the
  // rows from every render at once.
  const drawn = made.slice();

  // Your own row gets no controls at all. An admin who demotes or blocks
  // themselves leaves a class nobody can approve anyone into.
  assert.ok(!drawn.some(b => b.aria === 'Role for Me'),
            'your own row must not offer a role picker');
  assert.ok(drawn.some(b => b.aria === 'Role for Other'), 'but everyone else has one');
  assert.equal(drawn.filter(b => b.textContent === 'Block').length, 1,
               'your own row must not offer a Block button');

  // The tiles, the code and the reports each ride on this same payload, and
  // each has its own line in load() that can simply go missing.
  assert.deepEqual(byId.counts.kids.map(t => t.text),
                   ['1 waiting', '3 members', '1 blocked', '1 reported'],
                   'the counts must be drawn, each against its own label');
  assert.ok(byId.invite.text.includes('/?code=ab%2Fcd'),
            'the join link is what gets pasted into WhatsApp; ?code is what /  reads');
  assert.ok(byId.reports.text.includes('f.pdf'), 'a live report must be listed');

  const tap = async label => {
    const b = made.find(x => x.textContent === label);
    assert.ok(b, label + ' is not on the screen');
    posts.length = 0;
    await b.onclick();
    return posts[0];
  };

  // Reject sits next to Approve and is one word away from being it.
  assert.deepEqual(await tap('Reject'),
                   {path: '/block', body: {id: 'p-1', blocked: true}},
                   'Reject must block the joiner, not approve them');
  assert.deepEqual(await tap('Approve'), {path: '/approve', body: {id: 'p-1'}});

  // Block toggles. Nailed to a constant, either nobody can be blocked or
  // nobody who has been can ever be let back in.
  assert.deepEqual(await tap('Block'),
                   {path: '/block', body: {id: 'm-2', blocked: true}});
  assert.deepEqual(await tap('Unblock'),
                   {path: '/block', body: {id: 'm-3', blocked: false}});

  // Reset password was appended to that same .acts row. Pointed at /block it
  // locks the classmate out instead of letting them back in, and deleted
  // altogether it takes the only recovery path off the screen.
  assert.deepEqual(await tap('Reset password'), {path: '/reset', body: {id: 'm-2'}});
  assert.equal(drawn.filter(b => b.textContent === 'Reset password').length, 1,
               'no reset button on a row that has never set a password');
  // The whole reason the password is stored as typed: an admin reads it back
  // down the phone. Blank it and the screen loses the one thing it is for.
  assert.ok(byId.members.text.includes('password member-password'),
            'a set password must be readable off the member row');

  // A report carries two ids and only one of them is a file that can go.
  assert.deepEqual(await tap('Remove'), {path: '/remove', body: {id: 'mat-9'}},
                   'Remove takes down the material, not the report row');
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_admin_panel_wires_every_button_to_what_it_says(tmp_path):
    """The panel is the only place roles and access change at all, and until
    this ran under node its whole script was untested: making Reject approve
    the person sitting next to Approve left the suite green."""
    f = tmp_path / "admin.js"
    f.write_text(STUB + ADMIN_SCRIPT + CHECKS)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ------------------------------------------- what the join button does meanwhile

JOIN_SCRIPT = re.search(r"<script>\n(.*)\n</script>", notes.JOIN_BODY, re.S).group(1)

JOIN_STUB = """
const assert = require('node:assert');
const btn = {disabled: false, textContent: 'Join'};
const form = {onsubmit: null, querySelector: () => btn};
const err = {textContent: ''};
const boxes = {};
const document = {getElementById: id => id === 'f' ? form : id === 'err' ? err
                                      : (boxes[id] = boxes[id] || {value: ''})};
let reloaded = 0;
const location = {reload: () => { reloaded++; }};
// The request is held open, because the whole point is what the button says
// while it is in flight.
let land, reply;
const fetch = () => new Promise(res => { land = () => res(reply); });
const answer = (ok, body) => ({ok, status: ok ? 200 : 409, json: async () => body});
"""

JOIN_CHECKS = """
(async () => {
  const first = form.onsubmit({preventDefault() {}});
  assert.equal(btn.disabled, true, 'a second tap during /join races the first');
  assert.notEqual(btn.textContent, 'Join', 'and the button must say it is working');

  // The 409 the double tap used to produce, and the only one that accuses a
  // brand new joiner of already existing.
  reply = answer(false, {error: 'that roll number is already registered'});
  land(); await first;
  assert.equal(btn.disabled, false, 'a refusal has to hand the button back');
  assert.equal(btn.textContent, 'Join');
  assert.match(err.textContent, /already registered/);
  assert.equal(reloaded, 0);

  const second = form.onsubmit({preventDefault() {}});
  reply = answer(true, {});
  land(); await second;
  assert.equal(reloaded, 1, 'and a good answer still goes through');
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_join_button_says_something_while_it_waits(tmp_path):
    """/join opens a database connection before it answers. It is the one
    screen every student sees and the only slow action with no busy state."""
    f = tmp_path / "join.js"
    f.write_text(JOIN_STUB + JOIN_SCRIPT + JOIN_CHECKS)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
