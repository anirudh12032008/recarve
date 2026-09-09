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


def test_a_name_with_no_spaces_in_it_cannot_push_the_gate_sideways():
    """Names are whatever the joiner typed, up to 80 characters, no format
    check -- and an email address in the Name box is the ordinary way one
    arrives with no break in it."""
    assert "overflow-wrap:break-word" in rule("body")


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
const el = () => ({
  textContent: '', innerHTML: '', className: '', value: '', selected: false,
  disabled: false, onclick: null, onchange: null,
  setAttribute() {}, appendChild(c) { return c; }, append() {},
  querySelector: () => el(),
});
const byId = {list: el(), members: el()};
const document = {getElementById: id => byId[id] || el(), createElement: el};
// null hangs up the way a dead server does: fetch itself rejects.
let reply = {ok: true, status: 200, json: async () => ({pending: [], members: [], me: 'x'})};
const fetch = () => (reply ? Promise.resolve(reply)
                           : Promise.reject(new TypeError('Failed to fetch')));
const answer = (ok, body) => ({ok, status: ok ? 200 : 503, json: async () => body});
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
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_refused_pending_leaves_the_admin_something_to_read(tmp_path):
    f = tmp_path / "admin.js"
    f.write_text(STUB + ADMIN_SCRIPT + CHECKS)
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
