# The look, in Tailwind

How this app should look, and how to get there from `notes.py` without a day
where the suite is red. Line numbers are `notes.py` on the commit that added
this file; the `<style>` block is 569–1238, `GATE_PAGE`'s is 6168–6412.

Read the comments in that style block before touching it. They are the reasons,
and the reasons are the valuable part: admin ink is not red *because* red
already means error (575–579); `--warn` is amber *because* below-75% is a fact
about a term still running, not a bug (584–586); `--err` is `#c62b41` *because*
`#e5484d` was 3.7:1 on the paper (590–594); `--hang:31px` is where a row's
words begin so anything standing in for a row lines up (597–599); labels not
icons in the tab bar *because* a webfont is a download and a glyph is a guess
(1148–1151). Every one of those survives this rewrite. What does not survive is
the scale they are written on.

---

## 1. What is actually wrong

Counted over the 670 lines of `PAGE` CSS:

**Eleven type sizes doing the work of five.** 13px×44, 16px×36, 20px×11,
26px×4, 17px×3, 14px×3, 12px×3, 11px×2, 30px, 15px, 10px.
- 17px exists three times and only three times: `.cal .mon b` (736), `.week b`
  (742), `.tile b` (756). Three different things, one accidental size, none of
  them a step on any scale. All three want 20px.
- 14px vs 13px vs 12px is three sizes for "small print": `.blank` 14px (687),
  `.tl .ev b` 14px (769), `.rename button` 14px (785) against 44 uses of 13px
  and `.tl .ev small` / `.tl .free` / `.tl .lunch` at 12px (770, 774, 776).
  One of these is the small size. The other two are drift.
- `.dock button:not(.primary)` is 15px (1046) sitting next to `.primary` at
  16px in the same row of buttons — two sizes deciding one thing that weight
  already decides.
- 10px (`.tile small`, 754) and 11px (`.week small` 741, `.tl .hr span` 763)
  are below the floor; 11px is the floor and 10px should not exist.
- 30px is one glyph, the `+` in `#fab` (1069). That is a glyph size, not a type
  step — it belongs as a one-off `leading-none text-[30px]`, not a scale entry.

**Five weights.** 650×26, 700×12, 600×11, 500×9, 400. 650 and 600 appear in the
same kinds of places for no stated reason — `.row .name b` is 600 (666) and
`.rank .name b` is 600 (868) while `.ann h3` is 650 (889) and `.row.att .name
b` is 650 (811). Pick 600 for emphasis and 700 for a heading, and delete 650
and 500 as separate ideas.

**Eleven radii.** 11px×32 is clearly the radius; 10px×7, 12px×6, 16px×3, 14px,
8px, 7px×4, 6px, 5px×3, 4px, 2px×4 are the rest. `.cal` 16px (733), `.mine`
12px (837), `.blank` 14px (686) and `.job` 12px (1122) are four cards with four
different corners. `.code` 7px (640) and `.tag` 6px (989) and `.badge` 7px
(850) are three chips with two.

**Spacing off any scale.** Gaps are 2,3,4,6,8,9,10,11,12,22px. Paddings include
`11px 12px` (656, 950, 965, 1122), `9px 15px` (1050), `13px 15px` (1143),
`22px 20px` (686), `3px 9px` (640, 820) and `3px 8px` (872, 989). `.blank`'s
22/20 and `.mine`'s 16 are the same object — a card — at two paddings.

**Borders competing with fills.** `.vote` (704) draws a `--line` border *and* a
`--surface` fill; so does `.blank` (686) and `.slot select` (1184) and `#q`
(631). A fill already separates the control from the page; the border on top of
it is a second, weaker edge that only muddies it. Meanwhile `.row-del` (715)
and `.rename button` (785) and `.mark button` (793) draw a border on *nothing*,
which is the right treatment — so the page has two conventions and no rule.

**Rows that read at one weight.** `.row .name b` at 600 over `.row .name small`
at 13px/`--mut` is correct and is the app's best idea. It is not applied in
`.job` (1122), `.slot` (1183) or `.tl .ev` (765), where the primary and
secondary text land within 1–2px and one grey of each other.

**Two headings, both 20px, different weights.** `.shead h2` 700 (649) and
`.ann h3` 650 (889) and `#doubts h2` 650 (925) and `article h2` 650 (1014) —
four selectors, one job.

---

## 2. The theme

`tailwind/tailwind.config.js`, replacing `theme: { extend: {} }`. Everything
colour-shaped stays a CSS variable behind the token, because the light/dark
swap already lives in `:root`/`@media` and `--h` is set from JS at runtime
(`el.style.setProperty('--h', hue(s.code))`, 12 call sites: 1495, 1567, 1631,
1724, 1734, 1944, 2403, 2930, 2959, 3185, 3235, 3504). Tailwind cannot know a
subject's hue at build time, so **the `:root` blocks at 570–605 stay exactly as
they are** and Tailwind only names them.

```js
module.exports = {
  content: ['../notes.py'],
  darkMode: 'media',
  theme: {
    extend: {
      colors: {
        bg:      'var(--bg)',
        surface: 'var(--surface)',
        fg:      'var(--fg)',
        mut:     'var(--mut)',
        line:    'var(--line)',
        accent:  { DEFAULT: 'var(--accent)', fg: 'var(--accent-fg)' },
        admin:   { DEFAULT: 'var(--admin)', fg: 'var(--admin-fg)' },
        warn:    { DEFAULT: 'var(--warn)', bg: 'var(--warn-bg)' },
        err:     'var(--err)',
        // The subject hue, composed at runtime from --h and the theme's
        // saturation/lightness. Never hard-code a subject colour.
        hue:      'hsl(var(--h) var(--sat) var(--lum))',
        'hue-chip':     'hsl(var(--h) var(--sat) var(--chip-lum))',
        'hue-chip-ink': 'hsl(var(--h) var(--sat) var(--chip-text))',
      },
      fontSize: {
        // line-heights included: a size is never chosen without one.
        micro: ['11px', { lineHeight: '1.3', letterSpacing: '.04em' }],
        small: ['13px', { lineHeight: '1.45' }],
        base:  ['16px', { lineHeight: '1.65' }],
        head:  ['20px', { lineHeight: '1.3', letterSpacing: '-.015em' }],
        big:   ['26px', { lineHeight: '1.2', letterSpacing: '-.022em' }],
      },
      fontWeight: { normal: '400', medium: '500', semibold: '600', bold: '700' },
      spacing: {
        // 4px grid. 'tap' is the floor for anything a thumb lands on.
        tap:  'var(--tap)',   // 44px
        hang: 'var(--hang)',  // 31px, where a row's words begin
      },
      borderRadius: {
        chip: '7px',    // code, tag, badge, flag
        DEFAULT: '11px',// every control and every row
        card: '14px',   // .blank, .mine, .cal, .job, #lock, #rec
        sheet: '18px',  // #sheet .card, #panel — the top corners of a sheet
      },
      maxWidth: { read: '70ch' },
    },
  },
};
```

Sizes not on the scale (`#fab`'s 30px glyph, `.tile small`'s 10px) become
arbitrary values at their one call site, so the scale stays five entries.

**Spacing rule:** only `p-*`/`gap-*` values on the 4px grid — 1, 2, 3, 4, 5, 6
(4/8/12/16/20/24px). Today's 9, 11, 13, 22 round to the nearest. The two
exceptions that keep an odd number are the safe-area paddings
(`max(10px,env(safe-area-inset-top))`, 625, 1154, 1193) and the 142px
bottom clearances, which are load-bearing arithmetic and fenced by tests
(see §5).

---

## 3. What "minimal" means here

1. **A fill or a border, never both.** A thing that sits *on* the page and can
   be pressed gets `bg-surface` and no border (`#q`, `.vote`, `.slot select`,
   `.days button`, `.qdock button`). A thing that sits *in* a row and must not
   pull the eye gets `border border-line bg-transparent` (`.row-del`,
   `.mark button`, `.rename button`). Cards get `bg-surface` and no border —
   except `.blank.bad`, where the border *is* the signal (696–697).
2. **Hierarchy by weight first, then by grey, then by size.** Two text sizes in
   one component is the maximum: `base` and `small`. If a row needs a third
   level, it gets `font-semibold` or `text-mut`, not a fourth size. Only a
   screen's own name (`head`) and a score (`big`) leave the two.
3. **Colour may carry meaning and may never carry it alone.** Every coloured
   state already says its own name in words — "Below 75%" beside `.row.low`
   (`f.textContent = 'Below 75%'`), "Hidden" inside `.ann.gone`, "Failed" in
   `.job.failed`, the count inside `.vote.on`. Keep it that way: if you add a
   colour state, add the word. `--err` is the only red and means *the app
   failed*; `--admin` is the only ink and means *only an admin may press this*;
   `--accent` is every member's action; `--warn` is a fact, not a fault.
4. **4.5:1 on every text pair, 3:1 on every control edge**, light and dark.
   These are asserted (§5) — `test_the_ink_reads_in_both_themes`,
   `test_every_control_has_a_boundary_you_can_see`. Never state a state in
   `opacity` on a container: it dims the word that explains the state
   (`test_every_way_in_is_locked_rather_than_missing`). `opacity` on `:active`
   is fine; `opacity` as a permanent state is not.
5. **Air: one step per level.** Section heading to its rows: `pt-5`. Between
   rows: none — a row is 44px tall and that is the gap. Between sections:
   `pt-6`. Screen edge: `px-4`. Card inside a screen: `m-4 p-5`. A screen that
   has more than these four numbers in it is wrong.
6. **44px, everywhere, still.** `min-h-tap` on anything tappable. This is the
   one rule that is never traded for looks.
7. **No new colour, no new size, no new radius without deleting one.**

---

## 4. Per screen, most valuable first

1. **Rows (`.row`, 654–684 — every screen).** One component, used by Classes,
   subject, Home, attendance, day view. Collapse to:
   `flex items-center gap-3 w-full min-h-tap px-3 py-2 rounded text-base`,
   name `font-semibold` + `text-small text-mut` underneath, meta `text-small
   text-mut`. Delete the 650/600 split. Biggest single win in the app: it is
   the thing you see most.
2. **Home (`renderHome`, `todayBlock`, `.cal`/`.tl`/`.tile`).** Three sizes
   (17px, 14px, 12px, 11px, 10px) become `head`/`small`/`micro`. `.cal` 16px
   radius → `rounded-card`, `.tile` 10px → `rounded`. `.tl .ev` gets the row
   treatment: `font-semibold` title, `text-small text-mut` under it, so the
   timeline reads at a glance instead of as two greys.
3. **Note + doubts (`article`, `#doubts`, `.dbt`, `.askbox`).** The reading
   column is the app's best surface; it needs the least. Keep `max-w-read`,
   move `article h1/h2/h3` onto `big`/`head`/`base+bold`, make `.ans`'s
   left rule the only ornament, drop the 650s.
4. **Classes + subject header (`.group`, `.shead`, `.sect`, `.code`).** `.code`
   keeps `bg-hue-chip text-hue-chip-ink rounded-chip` — the hue mechanism is
   untouched. `.shead h2` → `text-head font-bold`. `.sect` keeps its uppercase
   tracking; that is a label, not a heading, and it earns its 13px.
5. **Attendance (`.row.att`, `.mark`, `.off`, `.flag`, 790–824).** `.row.att
   .name b` at 20px/650 → `text-head font-semibold`; `.mark button` keeps
   border-on-nothing; `.flag` → `rounded-chip`. `.row.low` keeps `--warn` *and*
   keeps the word.
6. **Me (`.mine`, `.tally`, `.rank`, `.badge`, 837–880).** `.mine` →
   `rounded-card p-5`; `.score` → `big`; `.tally b` 20px → `head`; `.rank .pts`
   20px → `head`; `.badge` → `rounded-chip`. `.rank.you` keeps its inset accent
   rule — that is colour plus position, not colour alone.
7. **Campus (`.ann`, 884–920).** `.ann h3` → `text-head font-bold`; the `.md`
   block keeps its own spacing (it is generated markdown and the rules are
   already tight); `.ann.gone` keeps its grey left rule and its worded flag.
8. **Day view / timetable editor (`.days`, `.slot`, `.save`, `.dpick`).**
   `.slot select` drops its border for `bg-surface` — but only if the 3:1 edge
   test for that selector stays satisfied on the gate's own copy (§5); on
   `PAGE` there is no such test, and the fill carries the edge.
9. **The add sheet (`#sheet`, `#rec`, `#prog`, `#batchName`, 1075–1110).**
   Four inner cards at 12px/16px radius → `rounded-card`; the sheet's own
   `16px 16px 0 0` → `rounded-sheet rounded-b-none`. `.opt` keeps its two-line
   shape: `font-semibold` title, `text-small text-mut` span.
10. **Admin panel (`.compose`, `.pform`, `.row.adm`, `.tag`, `#jobs`).** Ink
    stays ink. `.tag` 6px → `rounded-chip`. `.job` gets the row treatment and
    `rounded-card`. Lowest on the list because two people see it.

---

## 5. Fences — do not move these

**The gate's landing keeps its own hand-written CSS.** `GATE_PAGE`'s style
block (6168–6412) and everything scoped under `.land` (6277–6405) is a separate,
deliberate, dark design. It is not converted, not scanned, not touched.
`test_auth.py:447` asserts `class="land"` is absent from `/login`. All of
`test_gate_page.py` reads that block by `rule(selector)` — string-splitting on
`"selector{"` — so these literal selectors and declarations must survive
verbatim in `GATE_PAGE`:

- `button[disabled]{` — contains no `opacity`, and its `color:var(--x)` /
  `background:var(--x)` pair must be ≥4.5:1 (both themes).
- `button.adm{` and `button.ghost{` must appear **before** `button[disabled]{`.
- `button{`, `input{`, `.row select{` each contain `font:inherit`.
- `.row select{` and `input{` each contain `border:1px solid var(--token)` at
  ≥3:1 on `--bg`.
- `main>p>a{` contains `min-height:44px`.
- `body{` contains `overflow-wrap:break-word`; `.row b{` contains
  `overflow-wrap:anywhere`.
- `.hint{` contains `var(--mut)` (`test_login.py:98`).
- Exactly two `:root{...}` blocks, light then dark, carrying
  `--accent/--accent-fg/--fg/--bg/--mut/--err/--admin/--admin-fg`.
- No `font:` shorthand anywhere containing `inherit` as a family.

**`PAGE` substrings asserted verbatim** (`test_page.py`):
- `#nav{padding-bottom:calc(142px + env(safe-area-inset-bottom))}` (1521)
- `#doubts{max-width:70ch;margin:0 auto;padding:0 18px 142px}` (1526)
- `#fab{...}` must contain `bottom:calc(76px + env(safe-area-inset-bottom))`
  and `height:58px` (1527)
- `#busy{...}` contains `pointer-events:none` and **no** `top:` (1647)
- `body.reading #fab` must NOT appear anywhere (1651)
- `.top{...z-index:N}` > `#ask{...z-index:N}`, and `#ask`'s < `#fab`'s
  (1659, 1671)
- `body:has(#ask.on) #fab{display:none}` (1674)
- `body.reading .tabs{display:none}` and `body.reading .tabs{display:flex}`
  (1513, 1516) — both, the second inside the 760px block
- `.ann.gone{...}` contains no `opacity`; `.ann.gone .flag{color:var(--mut)}`
  verbatim (1976, 1979)
- `--admin:`, `--admin-fg:`, `--warn:`, `--err:` defined as 6-digit hex in the
  first `<style>` block; `--admin` ≠ `#e5484d`
- `re.search(r"<style>\n(.*?)\n</style>", notes.PAGE)` is non-greedy and grabs
  the **first** style block. If Tailwind's sheet is inlined as a second
  `<style>`, it must come **after** the token block, and the token block must
  keep its exact `<style>\n … \n</style>` framing.

**Class names asserted through the JS stub** — these are compared by string, and
several by equality, so the token must stay and (where equality) must stay
*alone*:
- `row.className = 'row adm';` (1740) and `notes.PAGE.count("inked(") >= 4`
- `className, 'vote'` / `'vote on'` / `'row-del'` / `'rank you'` /
  `'today has'` / `'compose'` (test_page.py 411–1427)
- `fabEl.className === 'locked'` and `=== ''` (740, 764) — **equality**: `#fab`
  may not carry utility classes in its `class` attribute
- `form.className === 'rename'` (2514) — **equality**
- `e.className === 'lunch'` / `'free'` (2574) — **equality**
- `innerHTML.includes('class="thumb"')` (2466)
- `el.classList.add('low');` and `f.textContent = 'Below 75%';` (1893)

Rule that follows from the equality cases: **utilities go in the stylesheet
under the existing semantic class, via `@apply`, wherever JS assigns
`className` wholesale.** Only markup written as literal HTML in `PAGE` gets
bare utility classes in its `class=` attribute.

**Also unchanged:** every `id`, every `data-tab`, every event handler and its
wiring text; `--h` and the `HUES` map; the four tab labels; the 44px floor.

---

## 6. Order of conversion

Each step is its own commit, each ends with
`./.venv/bin/python -m pytest backend/tests -q` green and `./tailwind/build.sh`
run with `web/app.css` committed alongside.

0. **Wire the sheet in.** `web/app.css` exists (5.6KB) and *nothing reads it* —
   `notes.py` has no reference to it today. Add a `__CSS__` placeholder in a
   second `<style>` **after** the token block, filled in the `.replace(...)`
   chain at 4254 from `pathlib.Path(__file__).parent / "web/app.css"`. No
   markup changes, no class changes: this step should move zero tests. Add one
   test that the served page contains no `<link href="http` and no
   `<script src="…tailwind`, and that the inlined sheet is non-empty — then
   break it by emptying `app.css` to prove it fails.
1. **Theme only.** Land the config above; `@apply` nothing yet. Build, commit,
   confirm `app.css` is still tiny (no classes used = nothing emitted).
2. **The row.** Convert `.row` and its children to `@apply` in the existing
   selectors. Nothing in JS changes. This is the risky one; do it alone.
3. **Chips and cards.** `.code`, `.tag`, `.badge`, `.flag`; `.blank`, `.mine`,
   `.cal`, `.job`, `#lock`, `#rec`, `#batchName`.
4. **Controls.** `.vote`, `.row-del`, `.mark`, `.rename`, `.days`, `.slot`,
   `.save`, `.qdock`, `#q` — applying the fill-or-border rule.
5. **Type.** Sweep the five-step scale over every remaining `font-size` and
   collapse 650/500. After this, `grep -c "font-size:" ` inside the block
   should be near zero.
6. **Home's calendar and timeline**, then **Campus**, then **the add sheet**,
   then **the admin panel** — one commit each.
7. **Delete what is now dead.** Anything left in the token block that is not a
   variable, a `@media`, a `@keyframes`, the fenced literals from §5, or a
   rule Tailwind genuinely cannot express (`:has()`, `env()`, `color-mix`,
   `hsl(var(--h) …)`) should be gone.

At no point is the gate page touched.
