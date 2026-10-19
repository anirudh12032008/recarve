// ---- THE TUTOR: a teacher for the whole syllabus, topic by topic. ---------
// Spliced into the page's one <script> at __TUTOR_JS__ (see notes.py), so it
// shares every helper and global the page has: nav, view, go, render, line,
// quiet, waitline, failed, busy, busyDone, oops, mdSafe, typeset, agoSpan,
// hue, subjectOf, atLeast, ROLE, live. Plain ES2020, top-level declarations,
// and every name here starts with `tu` or `tutor` so nothing collides.
//
// Four levels, all URLs (route() parses them into view.tutor):
//   #tutor                     Home: next up, subjects, recent lessons
//   #tutor/CY1107              a subject: the syllabus as a tree
//   #tutor/CY1107/s/<uuid>     a lesson: the conversation, streamed
//   #tutor/CY1107/pyq          the past-paper bank for that subject
//   #tutor/CY1107/p/4/1        practice on unit 4, topic 1 (syllabus places)
//   #tutor/CY1107/l/4/1        the same topic to read: notes and questions, no model
//
// render() wipes #nav on every refresh, so nothing on these screens is held
// in the DOM. What the server said lives in TU_GOT by URL; the reply being
// streamed lives in tuStreamOn; the element it paints into is only a target,
// looked up again on every frame and replaced by every render().

const TU_GOT = {};            // url -> the answer, or {error, status}
const TU_ASKING = new Set();  // urls in flight, so a redraw never asks twice
const TU_STALE = new Set();   // answers kept on screen while a fresh copy is asked for
let tuStreamOn = null;        // {sid, code, text, done, error, ...} or null
let tuLiveEl = null;          // where the live reply is painted, this render
let tuPaintAt = 0, tuPaintTimer = null;
let tuFail = null;            // {sid, path, payload, error}: the last reply that broke
const tuLevel = {};           // code|unit|topic -> mastery, newer than any answer
const tuUnitsOpen = {};       // code -> {unit: true} for the folds left open
const tuPyqFilter = {};       // code -> {unit, topic, year, exam}
const tuPrefs = {};           // code -> {lang, depth}, as set on this screen
const tuScrolled = new Set(); // lessons already scrolled to their last line once
let tuSpend = null;           // the latest spend any answer carried
let tuFollow = false;         // just sent something: the next draw goes to the end

const TU_MODE = {teach: 'Teach', quiz: 'Quiz', pyq: 'PYQs', revise: 'Revise', ask: 'Ask',
                 practice: 'Practice'};
const TU_EXAM = {mini: 'Mini test', mid: 'Mid term', end: 'End term'};
const TU_IMP = {must: 'Must know', high: 'High', medium: 'Medium', low: 'Low'};
const TU_ACTIONS = [
  ['deeper', 'Go deeper'], ['example', 'Example'], ['simpler', 'Simpler'],
  ['quiz', 'Quiz me'], ['pyq', 'Show PYQs'], ['summary', 'Summarise'],
  ['next', 'Next topic'],
];
const tuU = {
  home: () => '/tutor',
  subj: code => '/tutor/subject?code=' + encodeURIComponent(code),
  sess: id => '/tutor/session?id=' + encodeURIComponent(id),
  pyq: code => '/tutor/pyq?code=' + encodeURIComponent(code),
};
const tuKey = (code, unit, topic) => [code, unit || '', topic || ''].join('\u0001');

// ---- Small things -------------------------------------------------------

function tuEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

function tuHeading(parent, label) {
  const h = tuEl('h2', 'sect', label);
  parent.appendChild(h);
  return h;
}

// saying(), but into the tutor's own column rather than straight onto #nav.
function tuBlank(parent, ...lines) {
  const el = tuEl('div', 'blank');
  for (const t of lines) el.appendChild(tuEl('p', null, t));
  parent.appendChild(el);
  return el;
}

function tuFailed(parent, d, what) {
  const box = tuEl('div');
  if (d.status === 403) {
    failed(box, 'The tutor is not open to this account.',
           d.error || 'Reading it needs a trusted member; an admin makes you one.');
  } else failed(box, what, d.error || 'Nothing is lost. Try again in a moment.');
  parent.appendChild(box);
}

const tuWhy = e => {
  const m = String((e && e.message) || e || '');
  return /Failed to fetch|NetworkError|Load failed/i.test(m)
    ? 'The tutor needs the server, and the server did not answer.' : m;
};

// Seconds, whatever the server sent: an ISO string, epoch seconds or ms.
const tuSecs = x => typeof x === 'number' ? (x > 1e12 ? x / 1000 : x)
  : x ? Date.parse(x) / 1000 || 0 : 0;

const tuMoney = v => {
  v = +v || 0;
  return '$' + (v > 0 && v < 0.1 ? v.toFixed(3) : v.toFixed(2));
};

// What a session's last line looks like in a list: the words, not the tags.
const tuPlain = s => String(s || '').replace(/<state>[\s\S]*?<\/state>/g, '')
  .replace(/<[^>]+>/g, ' ').replace(/[#*_`>$]+/g, '').replace(/\s+/g, ' ').trim();

// One markdown string the model wrote, made safe and typeset. The maths is
// lifted out first and put back after: marked reads `\,` and `\\` as escapes
// and `_` as emphasis, which is half of every formula the tutor writes. What
// goes back in is escaped text, and KaTeX reads it back out of the text node.
const TU_MATH = /\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$(?![\s$])[^$\n]+?\$/g;
const tuEsc = s => s.replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
function tuSafe(src) {
  const maths = [];
  const held = String(src || '').replace(TU_MATH, m => 'TUMATHX' + (maths.push(m) - 1) + 'X');
  return mdSafe(held).replace(/TUMATHX(\d+)X/g, (m, i) => maths[+i] === undefined ? m : tuEsc(maths[+i]));
}
function tuMd(src, cls) {
  const el = tuEl('div', cls || 'md tu-md');
  el.innerHTML = tuSafe(src);
  typeset(el);
  return el;
}

// An option or a label: the same escape, without the paragraph round it.
function tuInline(src) {
  const el = tuEl('span', 'tu-inl');
  el.innerHTML = tuSafe(src).trim().replace(/^<p>([\s\S]*)<\/p>$/, '$1');
  return el;
}

async function tuJSON(url, payload) {
  const r = await fetch(url, payload === undefined ? undefined : {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(d.error || 'the server said ' + r.status),
                                 {status: r.status});
  return d;
}

// The cache every screen reads from. Undefined means "not here yet, and now
// asked for"; a stale answer is returned as it is while the fresh one comes.
function tuNeed(url) {
  const have = TU_GOT[url];
  if ((have === undefined || TU_STALE.has(url)) && !TU_ASKING.has(url)) {
    TU_ASKING.add(url);
    tuJSON(url).then(d => { TU_GOT[url] = d; }, e => {
      // A refresh that fails leaves the answer we had: it is still true-ish,
      // and a lesson vanishing under a flaky signal is worse.
      if (!TU_GOT[url] || TU_GOT[url].error)
        TU_GOT[url] = {error: tuWhy(e), status: e.status || 0};
    }).finally(() => {
      TU_ASKING.delete(url);
      TU_STALE.delete(url);
      tuRender();
    });
  }
  return have;
}
const tuStale = url => { if (TU_GOT[url] !== undefined) TU_STALE.add(url); };

// render(), keeping the composer's focus and caret: the textarea is rebuilt
// with everything else, and a reply landing must not steal the keyboard.
function tuRender() {
  if (view.tab !== 'tutor') return;
  const a = document.activeElement;
  const keep = a && a.id === 'tu-say' ? [a.selectionStart, a.selectionEnd] : null;
  render();
  if (keep) {
    const t = document.getElementById('tu-say');
    if (t) { t.focus({preventScroll: true}); try { t.setSelectionRange(...keep); } catch (e) {} }
  }
}

// Whether this reader may make the tutor teach. The server says it on /tutor
// and on a session; a subject screen reached by a link asks /tutor for it.
function tuOwner(d) {
  if (d && typeof d.owner === 'boolean') return d.owner;
  const h = tuNeed(tuU.home());
  return !!(h && h.owner);
}

// ---- Mastery, as the newest thing anybody said about it -----------------

function tuTopicView(code, unit, topic) {
  const views = [];
  const sv = TU_GOT[tuU.subj(code)];
  if (sv && sv.units) views.push(sv);
  const h = TU_GOT[tuU.home()];
  if (h && h.subjects) views.push(...h.subjects.filter(x => x.code === code));
  for (const v of views)
    for (const u of v.units || [])
      if (!unit || u.unit === unit)
        for (const t of u.topics || []) if (t.topic === topic) return t;
  return null;
}

function tuMastery(list) {
  for (const m of [].concat(list || [])) {
    if (!m) continue;
    const code = m.code || m.subject_code;
    if (m.level == null) continue;
    tuLevel[tuKey(code, m.unit, m.topic)] = +m.level;
    const t = tuTopicView(code, m.unit, m.topic);
    if (t) {
      t.level = +m.level;
      if (m.attempts != null) t.attempts = m.attempts;
      if (m.correct != null) t.correct = m.correct;
    }
  }
}

function tuTopicLevel(code, unit, topic, fallback) {
  const k = tuKey(code, unit, topic);
  if (k in tuLevel) return tuLevel[k];
  return +fallback || 0;
}

function tuLessonLevel(d) {
  const s = d.session || {};
  const k = tuKey(s.code, s.unit, s.topic);
  if (k in tuLevel) return tuLevel[k];
  if (d.mastery && d.mastery.level != null) return +d.mastery.level;
  for (let i = (d.messages || []).length - 1; i >= 0; i--) {
    const st = d.messages[i].meta && d.messages[i].meta.state;
    if (st && st.level != null) return +st.level;
  }
  const t = tuTopicView(s.code, s.unit, s.topic);
  return t ? +t.level || 0 : 0;
}

// Five ticks, filled to the level. Said in words to a screen reader, and on
// a long press, because five small bars are a picture and not a number.
function tuMeter(level) {
  const n = Math.max(0, Math.min(5, Math.round(+level || 0)));
  const el = tuEl('span', 'tu-meter');
  el.setAttribute('role', 'img');
  const says = n === 0 ? 'Not started' : n === 5 ? 'Exam-ready, 5 of 5' : 'Mastery ' + n + ' of 5';
  el.setAttribute('aria-label', says);
  el.title = says;
  el.dataset.level = n;
  for (let k = 0; k < 5; k++) el.appendChild(tuEl('i', k < n ? 'on' : null));
  return el;
}

// The progress ring: a track in the hairline, the arc in the accent, the
// percentage in words inside it.
function tuRing(frac) {
  const f = Math.max(0, Math.min(1, +frac || 0));
  const r = 21, c = 2 * Math.PI * r;
  const el = tuEl('span', 'tu-ring');
  el.setAttribute('role', 'img');
  el.setAttribute('aria-label', Math.round(f * 100) + '% of the syllabus');
  el.innerHTML = '<svg viewBox="0 0 52 52" aria-hidden="true">'
    + '<circle cx="26" cy="26" r="' + r + '" class="tu-track"/>'
    + (f > 0 ? '<circle cx="26" cy="26" r="' + r + '" class="tu-arc" stroke-dasharray="'
      + (c * f).toFixed(2) + ' ' + c.toFixed(2) + '" transform="rotate(-90 26 26)"/>' : '')
    + '</svg><b>' + Math.round(f * 100) + '<small>%</small></b>';
  return el;
}

function tuImportance(t) {
  if (!t.importance) return null;
  const b = tuEl('button', 'tu-imp ' + t.importance);
  b.appendChild(tuEl('span', null, TU_IMP[t.importance] || t.importance));
  const src = t.importance_source === 'pyq'
    ? 'From past papers: ' + (t.pyq_count || 0) + ' question' + (t.pyq_count === 1 ? '' : 's')
      + (t.pyq_marks ? ', ' + t.pyq_marks + ' marks' : '')
    : t.importance_source === 'tutor' ? "The tutor's own call"
    : 'An estimate, until past papers are imported';
  const says = src + (t.why ? '. ' + t.why : '');
  b.title = says;
  b.setAttribute('aria-label', (TU_IMP[t.importance] || t.importance) + ' importance. ' + says);
  b.onclick = e => { e.stopPropagation(); busyDone(says); };
  return b;
}

const tuSourceNote = (src, n) => src === 'pyq'
  ? 'Importance from ' + n + ' past-paper question' + (n === 1 ? '' : 's')
  : src === 'estimate' ? 'Importance estimated, no past papers yet'
  : src === 'tutor' ? "Importance is the tutor's own call" : 'No importance yet';

const tuPyqTotal = sv => (sv.units || []).reduce((a, u) =>
  a + (u.topics || []).reduce((b, t) => b + (+t.pyq_count || 0), 0), 0);

// ---- Settings: language and depth, per subject --------------------------

function tuPref(code) {
  if (tuPrefs[code]) return tuPrefs[code];
  const sv = TU_GOT[tuU.subj(code)];
  const prof = (sv && sv.profile) || {};
  let kept = {};
  try { kept = JSON.parse(localStorage.getItem('tutor:prefs:' + code) || '{}') || {}; } catch (e) {}
  return {lang: prof.lang || kept.lang || 'english', depth: prof.depth || kept.depth || 'exam',
          tier: prof.tier || kept.tier || 'standard', mix: prof.mix || kept.mix || 'questions'};
}

async function tuSetPref(code, field, value) {
  const was = tuPref(code);
  tuPrefs[code] = Object.assign({}, was, {[field]: value});
  try { localStorage.setItem('tutor:prefs:' + code, JSON.stringify(tuPrefs[code])); } catch (e) {}
  tuRender();
  try {
    const d = await tuJSON('/tutor/settings', {code, [field]: value});
    const p = d.profile || {};
    tuPrefs[code] = {lang: p.lang || tuPrefs[code].lang, depth: p.depth || tuPrefs[code].depth,
                     tier: p.tier || tuPrefs[code].tier, mix: p.mix || tuPrefs[code].mix};
  } catch (e) {
    tuPrefs[code] = was;
    oops('Not saved', e);
  }
  tuRender();
}

// What the chosen tier runs on and what it costs, from /tutor's `tiers`.
const TU_TIER = {standard: 'Standard', advanced: 'Advanced'};
function tuTierNote(tier) {
  const home = TU_GOT[tuU.home()] || {};
  const t = (home.tiers || {})[tier] || {};
  const other = (home.tiers || {})[tier === 'advanced' ? 'standard' : 'advanced'] || {};
  const times = t.price_in && other.price_in ? Math.round(t.price_in / other.price_in) : 0;
  if (tier === 'advanced') {
    return 'A stronger model marks your written answers and takes your doubts'
      + (t.model ? ' (' + t.model + ')' : '')
      + (times > 1 ? ', about ' + times + '\u00d7 the spend.' : '.');
  }
  return 'A fast, cheap model marks your written answers and takes your doubts'
    + (t.model ? ' (' + t.model + ')' : '') + '. MCQs and Show solution cost nothing.';
}

function tuSeg(label, options, value, pick) {
  const wrap = tuEl('div', 'tu-pref');
  wrap.appendChild(tuEl('span', 'tu-plabel', label));
  const seg = tuEl('div', 'seg');
  seg.setAttribute('role', 'group');
  seg.setAttribute('aria-label', label);
  for (const [v, text] of options) {
    const b = tuEl('button', null, text);
    b.setAttribute('aria-pressed', String(v === value));
    b.onclick = () => { if (v !== value) pick(v); };
    seg.appendChild(b);
  }
  wrap.appendChild(seg);
  return wrap;
}

// ---- The header the page draws, for these four screens ------------------

function tutorTitle() {
  const t = view.tutor || {};
  if (t.session) {
    const d = TU_GOT[tuU.sess(t.session)];
    const s = d && d.session;
    return s ? s.topic || s.title || 'Lesson' : 'Lesson';
  }
  if (t.pyq) return 'Past-paper questions';
  if (t.practice || t.learn) {
    const p = t.practice || t.learn;
    const at = tuPlace(t.code, p[0], p[1]);
    return at ? at.topic : 'Practice';
  }
  if (t.code) {
    const d = TU_GOT[tuU.subj(t.code)];
    return (d && d.name) || (subjectOf(t.code) || {}).name || t.code;
  }
  return 'Tutor';
}

function tutorUp() {
  const t = view.tutor || {};
  return t.session || t.pyq || t.practice || t.learn
    ? ['‹ ' + t.code, 'Back to ' + t.code + ' in the tutor']
    : ['‹ Tutor', 'Back to the tutor'];
}

// ---- The screen ---------------------------------------------------------

function renderTutor() {
  const t = view.tutor || {};
  tuLiveEl = null;
  const root = tuEl('div', 'tutor' + (t.session ? ' tu-lesson-on' : ''));
  nav.appendChild(root);
  if (ROLE === null) {
    if (live) return void tuBlank(root, 'The tutor needs somebody signed in.',
      'This server is running without accounts, so there is nobody for the tutor to teach.');
    return void root.appendChild(waitline('Opening the tutor…'));
  }
  if (!atLeast('trusted')) {
    return void tuBlank(root, 'The tutor is for trusted members.',
      'It teaches the syllabus one topic at a time and says what the exam asks. '
      + 'An admin makes you a trusted member: ask in your class group.');
  }
  if (t.session) tuLesson(root, t.code, t.session);
  else if (t.practice) tuPractice(root, t.code, t.practice[0], t.practice[1]);
  else if (t.learn) tuLearn(root, t.code, t.learn[0], t.learn[1]);
  else if (t.pyq) tuBank(root, t.code);
  else if (t.code) tuSubject(root, t.code);
  else tuHome(root);
}

// ---- Home ---------------------------------------------------------------

function tuHome(root) {
  const d = tuNeed(tuU.home());
  if (d === undefined) return void root.appendChild(waitline('Opening the tutor…'));
  if (d.error) return void tuFailed(root, d, 'The tutor did not open.');
  if (d.spend) tuSpend = d.spend;
  const owner = !!d.owner;

  const lead = tuEl('div', 'tu-lead');
  lead.appendChild(tuEl('p', null, owner
    ? 'One topic at a time, checked as you go, with what the exam asks said out loud.'
    : 'You can read every lesson here. Only the person the tutor belongs to can start one.'
      + (d.learner && d.learner.name ? ' The progress below is ' + d.learner.name + '\u2019s.' : '')));
  const sp = tuSpend || {};
  const bits = [];
  if (sp.total != null) bits.push(tuMoney(sp.total) + ' spent in all');
  if (sp.today != null) bits.push(tuMoney(sp.today) + ' today');
  const tiers = d.tiers || {};
  if (tiers.standard && tiers.advanced) {
    bits.push('Standard ' + tiers.standard.model + ' · Advanced ' + tiers.advanced.model);
  } else if (d.model) bits.push(d.model);
  if (bits.length) lead.appendChild(tuEl('p', 'tu-spend', bits.join(' · ')));
  root.appendChild(lead);

  const plan = d.plan || [];
  tuHeading(root, 'Next up');
  if (plan.length) {
    const rows = tuEl('div', 'rows');
    plan.forEach(p => rows.appendChild(tuPlanRow(p, owner)));
    root.appendChild(rows);
  } else root.appendChild(quiet('Nothing planned yet. Open a subject and start on any topic.'));

  // Subjects with a syllabus to teach first, in the order the server gave;
  // the labs and the ones with nothing to teach from yet after them.
  const teachable = sv => (sv.units || []).some(u => (u.topics || []).length);
  const subs = (d.subjects || []).filter(teachable)
    .concat((d.subjects || []).filter(sv => !teachable(sv)));
  tuHeading(root, 'Subjects');
  if (subs.length) {
    const grid = tuEl('div', 'tu-subs');
    subs.forEach(sv => grid.appendChild(tuSubCard(sv)));
    root.appendChild(grid);
  } else root.appendChild(quiet('No subjects yet.'));

  const recent = d.recent || [];
  if (recent.length) {
    tuHeading(root, 'Recent lessons');
    const rows = tuEl('div', 'rows');
    recent.slice(0, 8).forEach(x => rows.appendChild(tuSessionRow(x, true)));
    root.appendChild(rows);
  }
}

function tuPlanRow(p, owner) {
  const b = document.createElement('button');
  line(p.topic || p.unit || p.code, [p.code, p.reason].filter(Boolean).join(' · '),
       b, hue(p.code));
  b.classList.add('tu-plan');
  const t = tuTopicView(p.code, p.unit, p.topic);
  b.appendChild(tuMeter(tuTopicLevel(p.code, p.unit, p.topic, t && t.level)));
  if (owner) b.appendChild(tuEl('span', 'tu-go', TU_MODE[p.suggested_mode] || 'Teach'));
  b.onclick = owner && p.topic
    ? () => tuOpenTopic(p.code, p.unit, p.topic) || go('tutor', p.code)
    : () => go('tutor', p.code);
  return b;
}

function tuSubCard(sv) {
  const b = tuEl('button', 'tu-sub');
  b.style.setProperty('--h', hue(sv.code));
  b.appendChild(tuRing(sv.progress));
  const txt = tuEl('span', 'tu-subt');
  txt.appendChild(tuEl('span', 'sj-code', sv.code));
  txt.appendChild(tuEl('b', null, sv.name || (subjectOf(sv.code) || {}).name || sv.code));
  const topics = (sv.units || []).flatMap(u => u.topics || []);
  const started = topics.filter(t => (t.level || 0) > 0 || (t.attempts || 0) > 0).length;
  const lines = [];
  if (topics.length) {
    lines.push(started + ' of ' + topics.length + ' topics started');
    lines.push(tuSourceNote(sv.importance_source, sv.pyq_count || tuPyqTotal(sv)));
  } else lines.push('No syllabus to teach from yet');
  if (+sv.spend > 0) lines.push(tuMoney(sv.spend) + ' spent');
  txt.appendChild(tuEl('small', null, lines.join(' · ')));
  b.appendChild(txt);
  b.onclick = () => go('tutor', sv.code);
  return b;
}

function tuSessionRow(x, withCode) {
  const b = document.createElement('button');
  const sub = [withCode ? x.code : '', TU_MODE[x.mode] || x.mode,
               tuPlain(x.last_line)].filter(Boolean).join(' · ');
  line(x.title || x.topic || 'Lesson', sub, b, hue(x.code || 'XX'));
  b.classList.add('tu-sess');
  const m = tuEl('span', 'meta');
  if (x.status === 'open') m.appendChild(tuEl('span', 'tu-open', 'Open'));
  m.appendChild(agoSpan(tuSecs(x.updated_at || x.created_at)));
  b.appendChild(m);
  b.onclick = () => tuOpenLesson(x);
  return b;
}

// A practice session opens on its topic's practice screen; an older lesson
// opens as the conversation it was.
function tuOpenLesson(x) {
  if (x.mode === 'practice' && tuOpenTopic(x.code, x.unit, x.topic)) return;
  go('tutor', x.code, 's', x.id);
}

// ---- One subject --------------------------------------------------------

function tuSubject(root, code) {
  const d = tuNeed(tuU.subj(code));
  if (d === undefined) return void root.appendChild(waitline('Reading the syllabus…'));
  if (d.error) return void tuFailed(root, d, 'This subject did not open.');
  const owner = tuOwner(d);
  const ps = d.pyq_summary || {};
  const sessions = (d.sessions || []).slice()
    .sort((a, b) => tuSecs(b.updated_at) - tuSecs(a.updated_at));

  // Where you stand, in one strip: the ring, and what the importance rests on.
  const top = tuEl('div', 'tu-summary');
  top.style.setProperty('--h', hue(code));
  top.appendChild(tuRing(d.progress));
  const words = tuEl('div', 'tu-subt');
  const topics = (d.units || []).flatMap(u => (u.topics || []).map(t => [u.unit, t]));
  const ready = topics.filter(([u, t]) => tuTopicLevel(code, u, t.topic, t.level) >= 4).length;
  words.appendChild(tuEl('b', null, topics.length
    ? ready + ' of ' + topics.length + ' topics exam-ready' : 'No syllabus for this subject'));
  words.appendChild(tuEl('small', null, tuSourceNote(d.importance_source, ps.count || tuPyqTotal(d))
    + (d.syllabus_estimated ? ' · syllabus estimated' : '')));
  top.appendChild(words);
  root.appendChild(top);

  if (owner) {
    const pref = tuPref(code);
    const prefs = tuEl('div', 'tu-prefs w');
    prefs.appendChild(tuSeg('Language', [['english', 'English'], ['hinglish', 'Hinglish']],
      pref.lang, v => tuSetPref(code, 'lang', v)));
    prefs.appendChild(tuSeg('Depth', [['basics', 'Basics'], ['exam', 'Exam'], ['deep', 'Deep']],
      pref.depth, v => tuSetPref(code, 'depth', v)));
    prefs.appendChild(tuSeg('Tutor', [['standard', 'Standard'], ['advanced', 'Advanced']],
      pref.tier, v => tuSetPref(code, 'tier', v)));
    prefs.appendChild(tuSeg('Lesson', TU_MIX, pref.mix, v => tuSetPref(code, 'mix', v)));
    prefs.appendChild(tuEl('p', 'tu-tiernote', tuTierNote(pref.tier)));
    root.appendChild(prefs);
  }

  const open = sessions.find(x => x.status === 'open');
  if (open) {
    const c = tuEl('button', 'tu-continue');
    c.style.setProperty('--h', hue(code));
    c.appendChild(tuEl('span', 'tu-k', 'Continue'));
    c.appendChild(tuEl('b', null, open.title || open.topic || 'Your last lesson'));
    const last = tuPlain(open.last_line);
    if (last) c.appendChild(tuEl('small', null, last));
    c.onclick = () => tuOpenLesson(open);
    root.appendChild(c);
  }

  if (d.plan && d.plan.length) {
    tuHeading(root, 'Next up');
    const rows = tuEl('div', 'rows');
    d.plan.slice(0, 3).forEach(p => rows.appendChild(tuPlanRow(p, owner)));
    root.appendChild(rows);
  }

  const bank = document.createElement('button');
  line('Past-paper questions',
       ps.count ? ps.count + ' questions' + (ps.years && ps.years.length
         ? ', ' + Math.min(...ps.years) + '–' + Math.max(...ps.years) : '')
       : 'None imported yet', bank, hue(code));
  bank.onclick = () => go('tutor', code, 'pyq');
  const rows = tuEl('div', 'rows tu-bankrow');
  rows.appendChild(bank);
  root.appendChild(rows);

  tuHeading(root, 'Syllabus');
  const opened = tuUnitsOpen[code] || (tuUnitsOpen[code] = {});
  const tree = tuEl('div', 'tu-tree');
  (d.units || []).forEach((u, i) => {
    const det = tuEl('details', 'tu-unit');
    const was = opened[u.unit];
    det.open = was === undefined ? i === 0 : was;
    det.ontoggle = () => { opened[u.unit] = det.open; };
    const sum = tuEl('summary');
    const ts = u.topics || [];
    const done = ts.filter(t => tuTopicLevel(code, u.unit, t.topic, t.level) > 0).length;
    const pyqs = ts.reduce((a, t) => a + (+t.pyq_count || 0), 0);
    const name = tuEl('span', 'tu-uname');
    name.appendChild(tuEl('b', null, u.unit));
    name.appendChild(tuEl('small', null, done + ' of ' + ts.length + ' started'
      + (pyqs ? ' · ' + pyqs + ' PYQ' + (pyqs === 1 ? '' : 's') : '')));
    sum.appendChild(name);
    const bar = tuEl('span', 'tu-ubar');
    bar.setAttribute('aria-hidden', 'true');
    const fill = tuEl('i');
    fill.style.width = Math.round(100 * Math.max(0, Math.min(1, +u.progress || 0))) + '%';
    bar.appendChild(fill);
    sum.appendChild(bar);
    det.appendChild(sum);
    const list = tuEl('div', 'tu-topics');
    ts.forEach(t => list.appendChild(tuTopicRow(code, u.unit, t, owner)));
    det.appendChild(list);
    tree.appendChild(det);
  });
  root.appendChild(tree);

  if (sessions.length) {
    tuHeading(root, 'Recent lessons');
    const rows2 = tuEl('div', 'rows');
    sessions.slice(0, 10).forEach(x => rows2.appendChild(tuSessionRow(x, false)));
    root.appendChild(rows2);
  }
}

function tuTopicRow(code, unit, t, owner) {
  const el = tuEl('div', 'tu-topic');
  const head = tuEl('div', 'tu-th');
  head.appendChild(tuMeter(tuTopicLevel(code, unit, t.topic, t.level)));
  const name = tuEl('span', 'tu-tname');
  name.appendChild(tuEl('b', null, t.topic));
  const facts = [];
  if (t.pyq_count) facts.push(t.pyq_count + ' PYQ' + (t.pyq_count === 1 ? '' : 's')
                              + (t.pyq_marks ? ', ' + t.pyq_marks + ' marks' : ''));
  if (t.attempts) facts.push(t.correct + '/' + t.attempts + ' right');
  if (t.sessions) facts.push(t.sessions + ' lesson' + (t.sessions === 1 ? '' : 's'));
  if (facts.length) name.appendChild(tuEl('small', null, facts.join(' · ')));
  head.appendChild(name);
  const imp = tuImportance(t);
  if (imp) head.appendChild(imp);
  el.appendChild(head);

  const acts = tuEl('div', 'tu-acts');
  if (owner) {
    const teach = tuEl('button', 'w tu-btn primary', t.sessions ? 'Continue' : 'Teach');
    teach.onclick = () => tuOpenTopic(code, unit, t.topic);
    acts.append(teach);
  }
  const learn = tuEl('button', 'w tu-btn', 'Learn');
  learn.onclick = () => tuOpenTopic(code, unit, t.topic, 'l');
  acts.appendChild(learn);
  if (t.pyq_count) {
    const pyq = tuEl('button', 'tu-btn', 'PYQs ' + t.pyq_count);
    pyq.onclick = () => {
      tuPyqFilter[code] = {unit, topic: t.topic, year: '', exam: ''};
      go('tutor', code, 'pyq');
    };
    acts.appendChild(pyq);
  }
  if (acts.childElementCount) el.appendChild(acts);
  return el;
}

// ---- Starting a lesson, and talking in one ------------------------------

function tuStart(o) {
  if (tuStreamOn) return void busyDone('The tutor is still writing. One lesson at a time.');
  const pref = tuPref(o.code);
  busy('Starting the lesson…', true);
  tuStream('/tutor/start', Object.assign({lang: pref.lang, depth: pref.depth, tier: pref.tier}, o),
           {sid: null, code: o.code, start: true});
}

function tuSay(sid, payload) {
  if (tuStreamOn) return;
  const d = TU_GOT[tuU.sess(sid)];
  if (!d || !d.session) return;
  if (d.messages) d.messages.push({id: 'mine-' + Date.now(), role: 'student',
    kind: payload.kind || 'say', body: payload.body,
    meta: payload.action ? {action: payload.action} : {}, created_at: Date.now() / 1000});
  tuFail = null;
  tuFollow = true;
  tuStream('/tutor/say', Object.assign({session_id: sid}, payload),
           {sid, code: d.session.code});
}

// The ndjson stream: one JSON object per line, read as it arrives. Every
// delta is appended to the reply in module state and a paint is scheduled;
// the paint finds the live element afresh, so a render() in the middle of a
// reply loses nothing -- it draws the reply so far and the next frame carries
// on into the new element.
async function tuStream(path, payload, st) {
  Object.assign(st, {text: '', done: false, error: null});
  tuStreamOn = st;
  tuRender();
  try {
    const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                 body: JSON.stringify(payload)});
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw Object.assign(new Error(d.error || 'the server said ' + r.status), {status: r.status});
    }
    const take = ln => {
      if (!ln.trim()) return;
      let ev;
      try { ev = JSON.parse(ln); } catch (e) { return; }
      tuTake(st, ev);
    };
    if (r.body && r.body.getReader) {
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const {value, done} = await reader.read();
        if (done) break;
        buf += dec.decode(value, {stream: true});
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          take(buf.slice(0, nl));
          buf = buf.slice(nl + 1);
        }
      }
      take(buf + dec.decode());
    } else (await r.text()).split('\n').forEach(take);
    if (!st.done && !st.error) st.error = 'The reply stopped before it finished.';
  } catch (e) {
    st.error = tuWhy(e);
  }
  if (tuStreamOn === st) tuStreamOn = null;
  clearTimeout(tuPaintTimer);
  tuPaintTimer = null;
  if (st.start && !st.sid) busyDone();
  if (st.error) {
    if (!st.sid) oops('The lesson did not start', new Error(st.error));
    else {
      tuFail = {sid: st.sid, error: st.error, payload: st.start
        ? {kind: 'say', body: 'Please start the lesson.'}
        : tuWithout(payload, 'session_id')};
      tuStale(tuU.sess(st.sid));
    }
  }
  tuRender();
}

const tuWithout = (o, k) => { const c = Object.assign({}, o); delete c[k]; return c; };

function tuTake(st, ev) {
  if (ev.t === 'session' && ev.session) {
    const s = ev.session;
    st.sid = s.id;
    st.code = s.code || st.code;
    if (ev.resumed) tuStale(tuU.sess(s.id));   // reopened: load what was said
    else {
      TU_GOT[tuU.sess(s.id)] = {session: s, messages: [], pyqs: {}, owner: true};
      tuScrolled.add(s.id);       // a new lesson is read from its top
    }
    busyDone();
    if (view.tab === 'tutor') {
      // From Home, step through the subject, so Back climbs to it.
      if (location.hash === '#tutor') history.pushState(null, '', hashOf('tutor', st.code));
      go('tutor', st.code, 's', s.id);
    } else {
      busyDone('Your lesson has started', {act: ['Open', () => go('tutor', st.code, 's', s.id)]});
    }
  } else if (ev.t === 'delta') {
    st.text += ev.text || '';
    tuSchedule();
  } else if (ev.t === 'done') {
    st.done = true;
    const d = TU_GOT[tuU.sess(st.sid)];
    if (d) {
      if (ev.message) d.messages.push(ev.message);
      if (ev.session) d.session = Object.assign({}, d.session, ev.session);
      const cited = ((ev.message && ev.message.meta && ev.message.meta.pyqs) || [])
        .concat([...String((ev.message && ev.message.body) || st.text)
          .matchAll(/<pyq\b[^>]*\bid="([^"]+)"/g)].map(m => m[1]));
      if (cited.some(id => !(d.pyqs || {})[id])) tuStale(tuU.sess(st.sid));
    }
    tuMastery(ev.mastery);
    if (ev.spend) tuSpend = ev.spend;
    tuStale(tuU.home());
    if (st.code) tuStale(tuU.subj(st.code));
  } else if (ev.t === 'error') {
    st.error = ev.error || 'The tutor stopped.';
  }
}

function tuSchedule() {
  if (tuPaintTimer) return;
  const wait = Math.max(0, 80 - (Date.now() - tuPaintAt));
  tuPaintTimer = setTimeout(() => {
    tuPaintTimer = null;
    tuPaintAt = Date.now();
    tuPaintLive();
  }, wait);
}

// Where the reading column ends: the top of the dock, or the screen's edge.
function tuDockTop() {
  const dock = document.querySelector('.tu-dock');
  return dock ? dock.getBoundingClientRect().top : window.innerHeight;
}

function tuPaintLive() {
  const st = tuStreamOn, el = tuLiveEl;
  if (!st || !el || !el.isConnected || st.done) return;
  const limit = tuDockTop();
  const before = el.getBoundingClientRect().bottom;
  tuFill(el, st.text, tuCtx(st.sid), true);
  // Follow the writing only if the reader was following it: somebody who has
  // scrolled up to re-read the last paragraph is not yanked back down.
  const after = el.getBoundingClientRect().bottom;
  if (before <= limit + 32 && after > limit - 12) window.scrollBy(0, after - limit + 16);
  tuLiveState(st);
}

// <state> is never shown, but the level in it moves the header's meter now.
function tuLiveState(st) {
  const all = [...st.text.matchAll(/<state>([\s\S]*?)<\/state>/g)];
  if (!all.length) return;
  let s;
  try { s = JSON.parse(all[all.length - 1][1]); } catch (e) { return; }
  const d = TU_GOT[tuU.sess(st.sid)];
  if (!s || s.level == null || !d || !d.session) return;
  const k = tuKey(d.session.code, d.session.unit, d.session.topic);
  if (tuLevel[k] === +s.level) return;
  tuLevel[k] = +s.level;
  const m = document.querySelector('#sname .tu-meter');
  if (m) m.replaceWith(tuMeter(s.level));
}

function tuCtx(sid) {
  const d = TU_GOT[tuU.sess(sid)] || {};
  return {sid, owner: tuOwner(d), pyqs: d.pyqs || {}, code: d.session && d.session.code};
}

// ---- The tutor's language: markdown with three tags in it ---------------
// <check id kind>{json}</check>, <pyq id="..."/>, <state>{json}</state>.
// Split into pieces in the order they stand. While a reply is still being
// written, a tag that has opened but not closed is a "being written" card
// (or nothing, for <state>), and half a tag at the very end is held back
// rather than flashed as text.
const TU_TAG = /<check\b([^>]*)>([\s\S]*?)<\/check>|<pyq\b([^>]*?)\/?>(?:\s*<\/pyq>)?|<state>([\s\S]*?)<\/state>/g;

function tuAttrs(s) {
  const out = {};
  for (const m of String(s || '').matchAll(/([\w-]+)\s*=\s*"([^"]*)"/g)) out[m[1]] = m[2];
  return out;
}

function tuSegments(text, live) {
  const segs = [];
  const md = s => { if (s.trim()) segs.push({k: 'md', s, sig: 'md:' + s}); };
  let at = 0;
  text = String(text || '');
  for (const m of text.matchAll(TU_TAG)) {
    md(text.slice(at, m.index));
    at = m.index + m[0].length;
    if (m[0].startsWith('<check')) {
      const a = tuAttrs(m[1]);
      segs.push({k: 'check', id: a.id || 'c' + segs.length, kind: a.kind || 'mcq',
                 body: m[2], sig: 'check:' + m[0]});
    } else if (m[0].startsWith('<pyq')) {
      const a = tuAttrs(m[3]);
      segs.push({k: 'pyq', id: a.id, sig: 'pyq:' + a.id});
    } else segs.push({k: 'state', s: m[4], sig: 'state:' + m[4]});
  }
  let rest = text.slice(at);
  const open = rest.search(/<(check|state)\b/);
  if (open >= 0 && (live || /^<state/.test(rest.slice(open)))) {
    md(rest.slice(0, open));
    if (live && rest.startsWith('<check', open)) segs.push({k: 'wait', sig: 'wait'});
  } else {
    // Half a tag at the end of what has arrived so far: hold it back.
    if (live) rest = rest.replace(/<\/?[a-z]{0,6}(\s[^<>]*)?$/i, m =>
      /^<\/?(c|ch|che|chec|check|p|py|pyq|s|st|sta|stat|state)?(\s|$)/i.test(m) ? '' : m);
    md(rest);
  }
  return segs;
}

// Draw a turn into `el`, keeping every piece that has not changed. A check
// card somebody is halfway through answering survives the next 80ms frame
// because its signature is the same; only the paragraph still being written
// is drawn again.
function tuFill(el, text, ctx, live) {
  const segs = tuSegments(text, live);
  const think = el.querySelector(':scope > .tu-think');
  if (think && segs.length) think.remove();
  const have = el.tuSegs || (el.tuSegs = []);
  let i = 0;
  while (i < segs.length && i < have.length && have[i].sig === segs[i].sig) i++;
  for (let k = i; k < have.length; k++) have[k].node.remove();
  have.length = i;
  for (; i < segs.length; i++) {
    const node = tuSegNode(segs[i], ctx, live);
    el.appendChild(node);
    have.push({sig: segs[i].sig, node});
  }
  if (live) {
    let pen = el.querySelector(':scope > .tu-pen');
    if (!pen) pen = tuEl('span', 'tu-pen');
    pen.setAttribute('aria-hidden', 'true');
    el.appendChild(pen);     // always last: the nib, where the words are arriving
  }
}

function tuSegNode(seg, ctx, live) {
  if (seg.k === 'md') return tuMd(seg.s);
  if (seg.k === 'check') return tuCheck(seg, ctx);
  if (seg.k === 'pyq') {
    const p = ctx.pyqs[seg.id];
    if (p) return tuPyqCard(p, ctx, false);
    // Not in hand yet: fetching while the turn is still being written or the
    // lesson is being re-read; otherwise the tutor named an id the archive
    // does not have, and a card that says so beats one that waits forever.
    const url = tuU.sess(ctx.sid);
    const soon = live || TU_ASKING.has(url) || TU_STALE.has(url);
    const c = tuEl('div', 'tu-pyq' + (soon ? ' tu-pending' : ' tu-missing'));
    c.appendChild(tuEl('p', 'tu-k', 'Past paper'));
    c.appendChild(tuEl('p', 'tu-note', soon ? 'Fetching this question from the archive…'
                                           : 'This past-paper question is not in the archive.'));
    return c;
  }
  if (seg.k === 'wait') {
    const c = tuEl('div', 'tu-check tu-pending');
    c.appendChild(tuEl('p', 'tu-k', 'Check yourself'));
    c.appendChild(tuEl('p', 'tu-note', 'Writing you a question…'));
    return c;
  }
  const hid = tuEl('span');          // <state>: there, and never shown
  hid.hidden = true;
  return hid;
}

// ---- Check cards --------------------------------------------------------
// What was picked or written is kept per lesson on this phone, so a card you
// answered yesterday still shows your answer -- the server keeps the mark.

function tuChecks(sid) {
  try { return JSON.parse(localStorage.getItem('tutor:checks:' + sid) || '{}') || {}; }
  catch (e) { return {}; }
}
function tuCheckSet(sid, id, v) {
  const all = tuChecks(sid);
  all[id] = Object.assign(all[id] || {}, v);
  try { localStorage.setItem('tutor:checks:' + sid, JSON.stringify(all)); } catch (e) {}
  (tuCheckMem[sid] || (tuCheckMem[sid] = {}))[id] = all[id];
}
const tuCheckMem = {};   // the same, for a phone whose storage refuses
const tuCheckGet = (sid, id) => tuChecks(sid)[id] || (tuCheckMem[sid] || {})[id] || null;

function tuCheck(seg, ctx) {
  const card = tuEl('div', 'tu-check');
  let q;
  try { q = JSON.parse(seg.body.trim()); } catch (e) { q = null; }
  if (!q || typeof q !== 'object') {
    card.appendChild(tuEl('p', 'tu-k', 'Check yourself'));
    card.appendChild(tuMd(seg.body));
    return card;
  }
  const draw = () => {
    card.innerHTML = '';
    if (seg.kind === 'short') tuShort(card, q, seg.id, ctx, draw);
    else tuMcq(card, q, seg.id, ctx, draw);
    typeset(card);
  };
  draw();
  return card;
}

const TU_LETTER = 'ABCDEFGH';

function tuMcq(card, q, id, ctx, draw) {
  const opts = Array.isArray(q.options) ? q.options : [];
  const ans = Number.isInteger(+q.answer) && q.answer !== null && +q.answer < opts.length
    ? +q.answer : null;
  const st = tuCheckGet(ctx.sid, id);
  const head = tuEl('p', 'tu-k', 'Check yourself');
  card.appendChild(head);
  card.appendChild(tuMd(q.q || ''));
  const list = tuEl('div', 'tu-opts');
  list.setAttribute('role', 'group');
  list.setAttribute('aria-label', 'Options');
  opts.forEach((o, i) => {
    const b = tuEl('button', 'tu-opt');
    b.appendChild(tuEl('span', 'tu-letter', TU_LETTER[i] || String(i + 1)));
    b.appendChild(tuInline(o));
    if (st) {
      b.disabled = true;
      if (ans !== null && i === ans) b.classList.add('right');
      else if (i === st.picked) b.classList.add('wrong');
      if (i === st.picked) b.setAttribute('aria-pressed', 'true');
    } else b.onclick = () => tuPick(q, id, i, ans, ctx, draw);
    list.appendChild(b);
  });
  card.appendChild(list);
  if (!st) return;
  const right = ans !== null && st.picked === ans;
  const verdict = tuEl('p', 'tu-verdict ' + (ans === null ? '' : right ? 'right' : 'wrong'),
    ans === null ? 'Sent to the tutor to mark.'
    : right ? 'Right.' : 'Not quite. It is ' + TU_LETTER[ans] + '.');
  card.appendChild(verdict);
  if (q.why && ans !== null) card.appendChild(tuMd(q.why, 'md tu-md tu-why'));
  if (ctx.owner && ans !== null) {
    const ex = tuEl('button', 'w tu-btn', st.sent ? 'Asked' : right ? 'Explain anyway' : 'Explain');
    ex.disabled = !!st.sent || !!tuStreamOn;
    ex.onclick = () => {
      tuCheckSet(ctx.sid, id, {sent: true});
      tuSay(ctx.sid, {kind: 'answer', check_id: id, body: tuPickWords(q, st.picked, ans)});
    };
    const row = tuEl('div', 'tu-acts');
    row.appendChild(ex);
    card.appendChild(row);
  }
}

const tuPickWords = (q, picked, ans) => 'For "' + tuPlain(q.q) + '" I picked '
  + TU_LETTER[picked] + ' (' + tuPlain(q.options[picked]) + ')'
  + (ans === null ? '.' : picked === ans ? ', which was right. Explain why the others are wrong.'
     : '; the answer was ' + TU_LETTER[ans] + ' (' + tuPlain(q.options[ans])
       + '). Explain where my thinking went wrong.');

async function tuPick(q, id, i, ans, ctx, draw) {
  tuCheckSet(ctx.sid, id, {picked: i});
  draw();
  if (!ctx.owner) return;              // a reader may test themselves; it is not marked
  if (ans === null) {
    tuCheckSet(ctx.sid, id, {sent: true});
    return tuSay(ctx.sid, {kind: 'answer', check_id: id, body: tuPickWords(q, i, null)});
  }
  try {
    const d = await tuJSON('/tutor/mark', {session_id: ctx.sid, check_id: id,
                                           picked: i, correct: i === ans});
    tuMastery(d.mastery);
    const s = (TU_GOT[tuU.sess(ctx.sid)] || {}).session;
    const m = document.querySelector('#sname .tu-meter');
    if (s && m && d.mastery && d.mastery.level != null) m.replaceWith(tuMeter(d.mastery.level));
  } catch (e) {
    oops('Your answer was not marked', e);
  }
}

function tuShort(card, q, id, ctx) {
  const st = tuCheckGet(ctx.sid, id);
  const head = tuEl('p', 'tu-k', 'Check yourself');
  if (q.marks) head.appendChild(tuEl('span', 'tu-marks', '[' + q.marks + ' mark' + (q.marks == 1 ? '' : 's') + ']'));
  card.appendChild(head);
  card.appendChild(tuMd(q.q || ''));
  if (st && st.sent) {
    const mine = tuEl('p', 'said tu-mine', st.text || '');
    card.appendChild(mine);
    card.appendChild(tuEl('p', 'tu-note', 'Sent. The tutor marks it below.'));
    return;
  }
  if (!ctx.owner) {
    card.appendChild(tuEl('p', 'tu-note', 'Answered in the lesson below, if it was.'));
    return;
  }
  const box = tuEl('div', 'tu-short w');
  const ta = tuEl('textarea');
  ta.rows = 3;
  ta.maxLength = 4000;
  ta.placeholder = 'Write it the way you would in the exam';
  ta.setAttribute('aria-label', 'Your answer');
  const key = 'tutor:draft:' + ctx.sid + ':' + id;
  ta.value = draftGet(key);
  ta.oninput = () => draftSet(key, ta.value);
  const go2 = tuEl('button', 'tu-btn primary', 'Submit');
  go2.disabled = !!tuStreamOn;
  go2.onclick = () => {
    const text = ta.value.trim();
    if (!text) return void ta.focus();
    draftSet(key, '');
    tuCheckSet(ctx.sid, id, {sent: true, text});
    tuSay(ctx.sid, {kind: 'answer', check_id: id, body: text});
  };
  box.append(ta, go2);
  card.appendChild(box);
}

// ---- Past-paper question cards ------------------------------------------

function tuPyqHead(p) {
  return [TU_EXAM[p.exam] || p.exam,
          p.year ? p.year + '–' + String(+p.year + 1).slice(2) : '',
          p.qno ? 'Q' + p.qno : '', p.section ? 'Section ' + p.section : '']
    .filter(Boolean).join(' · ') || 'Past paper';
}

function tuPyqCard(p, ctx, inBank) {
  const c = tuEl('div', 'tu-pyq');
  const head = tuEl('p', 'tu-k', inBank ? tuPyqHead(p) : 'Past paper · ' + tuPyqHead(p));
  if (p.marks) head.appendChild(tuEl('span', 'tu-marks',
    '[' + (+p.marks) + ' mark' + (+p.marks === 1 ? '' : 's') + ']'));
  c.appendChild(head);
  c.appendChild(tuMd(p.question_md || ''));
  if (p.figure) {
    // The figure as printed, cut from the scanned paper: the question is
    // whole without the PDF.
    const img = tuEl('img', 'tu-fig');
    img.src = p.figure;
    img.alt = 'The figure printed with this question';
    img.loading = 'lazy';
    c.appendChild(img);
  }
  const det = tuEl('details', 'tu-ans');
  det.appendChild(tuEl('summary', null, 'Answer'));
  det.appendChild(p.answer_md ? tuMd(p.answer_md)
                              : tuEl('p', 'tu-note', 'No answer written for this one yet.'));
  c.appendChild(det);
  const foot = tuEl('div', 'tu-pfoot');
  if (inBank && (p.unit || p.topic)) foot.appendChild(tuEl('span', 'tu-where',
    [p.topic, p.unit].filter(Boolean).join(' · ')));
  if (p.title) {
    const src = p.path ? tuEl('a', 'tu-src') : tuEl('span', 'tu-src');
    src.textContent = p.title;
    if (p.path) { src.href = p.path; src.target = '_blank'; src.rel = 'noopener'; }
    foot.appendChild(src);
  }
  if (inBank && ctx.owner) {
    const t = tuEl('button', 'w tu-btn primary', 'Teach me this');
    t.disabled = !!tuStreamOn;
    t.onclick = () => (p.topic && tuOpenTopic(p.code || ctx.code, p.unit, p.topic))
      || tuStart({code: p.code || ctx.code, unit: p.unit || null,
                  topic: p.topic || null, mode: 'pyq', pyq_id: p.id});
    foot.appendChild(t);
  }
  if (foot.childElementCount) c.appendChild(foot);
  return c;
}

// ---- The lesson ---------------------------------------------------------

function tuLesson(root, code, sid) {
  const d = tuNeed(tuU.sess(sid));
  if (d === undefined) return void root.appendChild(waitline('Opening the lesson…'));
  if (d.error) return void tuFailed(root, d, 'This lesson did not open.');
  const s = d.session || {};
  const ctx = tuCtx(sid);
  const st = tuStreamOn && tuStreamOn.sid === sid && !tuStreamOn.done ? tuStreamOn : null;

  // The topic's mastery, live, beside its name in the header.
  sname.appendChild(document.createTextNode(' '));
  sname.appendChild(tuMeter(tuLessonLevel(d)));

  const col = tuEl('div', 'tu-lesson');
  const meta = tuEl('div', 'tu-lmeta');
  const facts = [s.unit, TU_MODE[s.mode] || s.mode,
                 s.tier === 'advanced' ? 'Advanced' : '',
                 s.lang === 'hinglish' ? 'Hinglish' : s.lang ? 'English' : '',
                 s.depth ? s.depth[0].toUpperCase() + s.depth.slice(1) + ' depth' : '',
                 tuMoney(s.cost_usd) + ' this lesson'].filter(Boolean);
  meta.appendChild(tuEl('span', null, facts.join(' · ')));
  if (ctx.owner && s.status === 'open' && !st) {
    const fin = tuEl('button', 'w tu-link', 'Finish');
    fin.onclick = () => tuClose(sid, fin);
    meta.appendChild(fin);
  } else if (s.status === 'done') meta.appendChild(tuEl('span', 'tu-done', 'Finished'));
  col.appendChild(meta);

  if (!ctx.owner) {
    const who = s.owner_name || s.by || d.owner_name || d.by;
    col.appendChild(quiet(who ? 'You are reading ' + who + "'s lesson."
                              : 'You are reading somebody else’s lesson.'));
  }

  const log = tuEl('div', 'tu-log');
  const msgs = d.messages || [];
  msgs.forEach(m => log.appendChild(tuMsg(m, ctx)));
  if (st) {
    const turn = tuEl('div', 'tu-turn tu-live');
    turn.setAttribute('aria-live', 'polite');
    turn.setAttribute('aria-busy', 'true');
    if (st.text) tuFill(turn, st.text, ctx, true);
    else {
      const w = tuEl('div', 'tu-think');
      waiting(w, 'The tutor is thinking\u2026');
      turn.appendChild(w);
    }
    tuLiveEl = turn;
    log.appendChild(turn);
  } else if (!msgs.length) {
    log.appendChild(quiet('Nothing said in this lesson yet.'));
  }
  col.appendChild(log);

  if (tuFail && tuFail.sid === sid && !st) {
    const bad = tuEl('div');
    failed(bad, 'The tutor stopped before it finished.', tuFail.error);
    if (ctx.owner) {
      const again = tuEl('button', 'w tu-btn primary', 'Send again');
      const payload = tuFail.payload;
      again.onclick = () => { tuFail = null; tuSay(sid, payload); };
      bad.appendChild(again);
    }
    col.appendChild(bad);
  }

  if (ctx.owner) col.appendChild(tuDock(sid, !!st));
  root.appendChild(col);

  // The first look at a lesson starts at its last line, which is where it
  // was left; a new one starts at the top, which is where it begins.
  if ((msgs.length && !tuScrolled.has(sid)) || tuFollow) {
    tuScrolled.add(sid);
    tuFollow = false;
    requestAnimationFrame(() => window.scrollTo(0, document.documentElement.scrollHeight));
  }
  // At least a screen tall, so the dock stands at the foot of the screen on a
  // short lesson rather than halfway up it under the first paragraph.
  requestAnimationFrame(() => {
    if (!col.isConnected) return;
    const below = parseFloat(getComputedStyle(nav).paddingBottom) || 0;
    col.style.minHeight = Math.max(0, window.innerHeight - (col.getBoundingClientRect().top
                                       + window.scrollY) - below) + 'px';
  });
}

function tuMsg(m, ctx) {
  if (m.role === 'student') {
    if (m.kind === 'action') {
      const a = (m.meta && m.meta.action) || '';
      const word = (TU_ACTIONS.find(x => x[0] === a) || [])[1] || tuPlain(m.body);
      return tuEl('p', 'tu-asked', word);
    }
    const el = tuEl('div', 'tu-me' + (m.kind === 'answer' ? ' answer' : ''));
    if (m.kind === 'answer') el.appendChild(tuEl('span', 'tu-k', 'Your answer'));
    el.appendChild(tuEl('p', 'said', m.body));
    return el;
  }
  const turn = tuEl('div', 'tu-turn');
  tuFill(turn, m.body, ctx, false);
  return turn;
}

function tuDock(sid, writing) {
  const dock = tuEl('div', 'tu-dock w');
  const quick = tuEl('div', 'tu-quick');
  quick.setAttribute('role', 'group');
  quick.setAttribute('aria-label', 'Ask the tutor to');
  for (const [action, label] of TU_ACTIONS) {
    const b = tuEl('button', 'tu-chip', label);
    b.disabled = writing;
    b.onclick = () => tuSay(sid, {kind: 'action', action, body: label});
    quick.appendChild(b);
  }
  dock.appendChild(quick);

  const box = tuEl('div', 'tu-compose');
  const ta = tuEl('textarea');
  ta.id = 'tu-say';
  ta.rows = 1;
  ta.maxLength = 8000;
  ta.placeholder = writing ? 'The tutor is writing…' : 'Ask, or answer the question';
  ta.setAttribute('aria-label', 'Say something to the tutor');
  ta.setAttribute('enterkeyhint', 'send');
  const key = 'tutor:draft:' + sid;
  ta.value = draftGet(key);
  const grow = () => {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight + 2, 200) + 'px';
  };
  const send = tuEl('button', 'tu-send', 'Send');
  const fire = () => {
    const body = ta.value.trim();
    if (!body || tuStreamOn) return;
    draftSet(key, '');
    ta.value = '';
    tuSay(sid, {kind: 'say', body});
  };
  ta.oninput = () => { draftSet(key, ta.value); grow(); };
  ta.onkeydown = e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); fire(); }
  };
  send.disabled = writing;
  send.onclick = fire;
  box.append(ta, send);
  dock.appendChild(box);
  requestAnimationFrame(grow);
  return dock;
}

async function tuClose(sid, btn) {
  btn.disabled = true;
  try {
    await tuJSON('/tutor/session/close', {id: sid});
    const d = TU_GOT[tuU.sess(sid)];
    if (d && d.session) d.session.status = 'done';
    tuStale(tuU.home());
    if (d && d.session) tuStale(tuU.subj(d.session.code));
    busyDone('Lesson finished. It stays here to read.');
  } catch (e) {
    btn.disabled = false;
    oops('Not finished', e);
  }
  tuRender();
}

// ---- The past-paper bank ------------------------------------------------

function tuSelect(label, value, options, pick) {
  const wrap = tuEl('label', 'tu-pick');
  wrap.appendChild(tuEl('span', 'tu-plabel', label));
  const sel = tuEl('select');
  for (const [v, text] of options) {
    const o = tuEl('option', null, text);
    o.value = v;
    sel.appendChild(o);
  }
  sel.value = value;
  sel.onchange = () => pick(sel.value);
  wrap.appendChild(sel);
  return wrap;
}

function tuBank(root, code) {
  const d = tuNeed(tuU.pyq(code));
  if (d === undefined) return void root.appendChild(waitline('Reading the past papers…'));
  if (d.error) return void tuFailed(root, d, 'The past papers did not open.');
  const qs = d.questions || [];
  const owner = tuOwner(null);
  if (!qs.length) {
    return void tuBlank(root, 'No past-paper questions for ' + code + ' yet.',
      'They are read out of the archive’s question papers on the machine that keeps '
      + 'them. There, run: notes.py tutor pyq extract --code ' + code
      + ', then notes.py tutor pyq answer to write the answers.',
      'Or open Claude Code on that machine and use the pyq-extract skill: it reads the '
      + 'papers itself and imports them with notes.py tutor pyq import.',
      'Until then the tutor marks what is important as an estimate, and says so.');
  }
  const f = tuPyqFilter[code] || (tuPyqFilter[code] = {unit: '', topic: '', year: '', exam: ''});
  const set = (k, v) => {
    f[k] = v;
    if (k === 'unit') f.topic = '';
    tuRender();
  };

  const bar = tuEl('div', 'tu-filters');
  const exams = ['mini', 'mid', 'end'].filter(e => qs.some(x => x.exam === e));
  if (exams.length > 1) {
    const seg = tuEl('div', 'seg');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', 'Exam');
    for (const [v, text] of [['', 'All']].concat(exams.map(e => [e, {mini: 'Mini', mid: 'Mid', end: 'End'}[e]]))) {
      const b = tuEl('button', null, text);
      b.setAttribute('aria-pressed', String(f.exam === v));
      b.onclick = () => set('exam', v);
      seg.appendChild(b);
    }
    bar.appendChild(seg);
  }
  const counts = d.counts || {};
  const units = Object.keys(counts).length ? Object.keys(counts)
    : [...new Set(qs.map(x => x.unit).filter(Boolean))];
  const tally = u => Object.values(counts[u] || {}).reduce((a, n) => a + n, 0);
  const picks = tuEl('div', 'tu-picks');
  if (units.length) picks.appendChild(tuSelect('Unit', f.unit,
    [['', 'Every unit']].concat(units.map(u => [u, u + (tally(u) ? ' (' + tally(u) + ')' : '')])),
    v => set('unit', v)));
  if (f.unit) {
    const topics = Object.keys(counts[f.unit] || {}).length ? Object.keys(counts[f.unit])
      : [...new Set(qs.filter(x => x.unit === f.unit).map(x => x.topic).filter(Boolean))];
    if (topics.length) picks.appendChild(tuSelect('Topic', f.topic,
      [['', 'Every topic']].concat(topics.map(t => [t, t + ((counts[f.unit] || {})[t]
        ? ' (' + counts[f.unit][t] + ')' : '')])), v => set('topic', v)));
  }
  const years = [...new Set(qs.map(x => x.year).filter(Boolean))].sort((a, b) => b - a);
  if (years.length > 1) picks.appendChild(tuSelect('Year', f.year,
    [['', 'Every year']].concat(years.map(y => [String(y), y + '–' + String(y + 1).slice(2)])),
    v => set('year', v)));
  bar.appendChild(picks);
  root.appendChild(bar);

  const shown = qs.filter(x => (!f.exam || x.exam === f.exam) && (!f.unit || x.unit === f.unit)
    && (!f.topic || x.topic === f.topic) && (!f.year || String(x.year) === f.year))
    .sort((a, b) => (b.year || 0) - (a.year || 0));
  root.appendChild(tuEl('p', 'quiet tu-count', shown.length === qs.length
    ? qs.length + ' question' + (qs.length === 1 ? '' : 's') + ', newest first'
    : shown.length + ' of ' + qs.length + ' questions'));
  if (!shown.length) {
    const clear = tuEl('button', 'tu-btn', 'Show every question');
    clear.onclick = () => { tuPyqFilter[code] = {unit: '', topic: '', year: '', exam: ''}; tuRender(); };
    const b = tuBlank(root, 'Nothing matches those filters.');
    b.appendChild(clear);
    return;
  }
  const list = tuEl('div', 'tu-bank');
  const ctx = {owner, code, sid: null, pyqs: {}};
  shown.forEach(p => list.appendChild(tuPyqCard(p, ctx, true)));
  root.appendChild(list);
}

// ---- Practice: one topic, question first --------------------------------
// docs/specs/2026-10-19-tutor-practice.md. The content is written ahead of
// time; this screen asks, marks MCQs itself, sends written answers to
// /tutor/answer to be graded, and takes doubts on /tutor/doubt. What happened
// on this visit lives in TU_PX (render() wipes the DOM); what the student has
// done over every visit is the server's `progress`.

const TU_PX = {};
const TU_MIX = [['theory', 'Theory first'], ['mixed', 'Key points first'],
                ['questions', 'Questions first']];
const TU_STATUS = {green: 'Clear', yellow: 'Some gaps', red: 'Needs work'};
const tuPxUrl = (code, unit, topic) => '/tutor/practice?code=' + encodeURIComponent(code)
  + '&unit=' + encodeURIComponent(unit) + '&topic=' + encodeURIComponent(topic);

function tuUnitsOf(code) {
  const sv = TU_GOT[tuU.subj(code)];
  if (sv && sv.units) return sv.units;
  const h = TU_GOT[tuU.home()];
  const v = h && (h.subjects || []).find(x => x.code === code);
  return (v && v.units) || null;
}

function tuPlace(code, u, t) {
  const unit = (tuUnitsOf(code) || [])[u - 1];
  const topic = unit && (unit.topics || [])[t - 1];
  return topic ? {unit: unit.unit, topic: topic.topic} : null;
}

// Opens a topic's practice; false when the syllabus is not loaded to place it.
function tuOpenTopic(code, unit, topic, how) {
  const units = tuUnitsOf(code) || [];
  const ui = units.findIndex(x => !unit || x.unit === unit);
  for (let i = Math.max(ui, 0); i < units.length; i++) {
    const ti = (units[i].topics || []).findIndex(x => x.topic === topic);
    if (ti >= 0) { go('tutor', code, how || 'p', i + 1, ti + 1); return true; }
    if (unit) break;
  }
  return false;
}

function tuPx(code, unit, topic) {
  const k = tuKey(code, unit, topic);
  return TU_PX[k] || (TU_PX[k] = {feed: [], seen: new Set(), basics: false, busy: false,
                                  again: false, visit: {}, lastSub: null, scroll: false,
                                  timer: null, stay: false, doubt: ''});
}

// The subtopic is finished for now: nothing left to ask, or the evidence
// says it is clear (green, or two tries with nothing badly wrong).
function tuSubDone(d, P, s) {
  const asked = new Set([...(P.again ? [] : ((d.progress[s.id] || {}).asked || [])), ...P.seen]);
  if (!(s.questions || []).some(q => !asked.has(q.id))) return true;
  if (P.again) return ((P.visit[s.id] || {}).right || 0) >= 1;
  const p = d.progress[s.id];
  if (!p || !(p.right + p.wrong)) return false;
  return p.status === 'green' || (p.status === 'yellow' && p.right + p.wrong >= 2);
}

function tuNextQ(d, P) {
  const subs = d.subtopics || [];
  const from = P.focus ? Math.max(0, subs.findIndex(s => s.id === P.focus)) : 0;
  for (const s of [...subs.slice(from), ...subs.slice(0, from)]) {
    if (s.id !== P.focus && tuSubDone(d, P, s)) continue;
    const p = d.progress[s.id] || {};
    const asked = new Set([...(P.again ? [] : (p.asked || [])), ...P.seen]);
    // Fast by default: the exam-level question first. Back to basics after a
    // miss at recall level, two struggling subtopics, or in a red one.
    const order = P.basics || p.status === 'red' ? [1, 2, 3] : [2, 3, 1];
    const left = (s.questions || []).filter(q => !asked.has(q.id)).sort((a, b) =>
      order.indexOf(a.level || 2) - order.indexOf(b.level || 2)
      || (b.kind === 'pyq') - (a.kind === 'pyq'));
    if (left.length) return {s, q: left[0]};
  }
  return null;
}

function tuPxAdvance(d, P) {
  const nx = tuNextQ(d, P);
  P.focus = null;
  if (!nx) {
    P.feed.push({k: 'done'});
    P.scroll = true;
    return;
  }
  if (P.lastSub !== nx.s.id) {
    P.lastSub = nx.s.id;
    const mix = tuPref(d.code).mix;
    if (mix !== 'questions') P.feed.push({k: 'intro', s: nx.s, mix});
  }
  P.seen.add(nx.q.id);
  P.feed.push({k: 'q', s: nx.s, q: nx.q, st: 'open', text: ''});
  P.scroll = true;
}

async function tuPxSend(d, P, c, payload) {
  if (P.busy) return;
  P.busy = true;
  c.st = 'sending';
  tuRender();
  try {
    const r = await tuJSON('/tutor/answer', Object.assign({code: d.code, unit: d.unit,
      topic: d.topic, subtopic: c.s.id, qid: c.q.id}, payload));
    d.progress[c.s.id] = r.progress;
    tuMastery(r.mastery);
    if (r.spend) tuSpend = r.spend;
    c.grade = r.grade;
    c.st = payload.reveal ? 'revealed' : 'graded';
    if (payload.picked !== undefined) c.picked = payload.picked;
    const out = r.grade && r.grade.outcome;
    const v = P.visit[c.s.id] || (P.visit[c.s.id] = {right: 0, wrong: 0});
    if (out === 'right') v.right++;
    if (out === 'wrong') {
      v.wrong++;
      const reds = Object.values(d.progress).filter(p => p.status === 'red').length;
      if ((c.q.level || 2) === 1 || reds >= 2) P.basics = true;
      P.feed.push({k: 'teach', s: c.s, q: c.q, idk: !!payload.idk});
    }
    if (out === 'right') tuPxAdvance(d, P);       // no click: the next card comes
    P.scroll = true;
    tuStale(tuU.subj(d.code));
    tuStale(tuU.home());
  } catch (e) {
    c.st = 'open';
    oops('That was not marked', e);
  }
  P.busy = false;
  tuRender();
}

// A doubt, streamed into the feed. Painted in place as it arrives; the full
// render (with the maths typeset) comes at the end.
async function tuPxDoubt(d, P, body) {
  const at = [...P.feed].reverse().find(c => c.k === 'q');
  P.feed.push({k: 'said', body});
  const live = {k: 'reply', body: '', live: true};
  P.feed.push(live);
  P.busy = true;
  P.scroll = true;
  tuRender();
  try {
    const r = await fetch('/tutor/doubt', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: d.code, unit: d.unit, topic: d.topic,
                            subtopic: at ? at.s.id : null, qid: at ? at.q.id : null, body})});
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || 'the server said ' + r.status);
    }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = '';
    const take = ln => {
      if (!ln.trim()) return;
      let ev;
      try { ev = JSON.parse(ln); } catch (e) { return; }
      if (ev.t === 'delta') {
        live.body += ev.text || '';
        const el = document.getElementById('tu-px-live');
        if (el) el.innerHTML = tuSafe(live.body);
      } else if (ev.t === 'error') live.error = ev.error;
      else if (ev.t === 'done' && ev.spend) tuSpend = ev.spend;
    };
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) { take(buf.slice(0, nl)); buf = buf.slice(nl + 1); }
    }
    take(buf + dec.decode());
  } catch (e) {
    live.error = tuWhy(e);
  }
  live.live = false;
  P.busy = false;
  tuRender();
}

// ---- Drawing it

let tuSmilesP = null;
function tuMolecule(f) {
  const fig = tuEl('figure', 'tu-mol');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', f.caption || f.smiles);
  fig.appendChild(svg);
  if (f.caption) fig.appendChild(tuInline(f.caption));
  tuSmilesP = tuSmilesP || new Promise((ok, no) => {
    const el = document.createElement('script');
    el.src = 'https://cdn.jsdelivr.net/npm/smiles-drawer@2.1.7/dist/smiles-drawer.min.js';
    el.onload = () => ok(window.SmilesDrawer);
    el.onerror = no;
    document.head.appendChild(el);
  });
  const dark = document.documentElement.dataset.theme === 'dark'
    || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
  tuSmilesP.then(SD => {
    const sd = new SD.SmiDrawer({width: 300, height: 180, bondThickness: 1.2},
                                {weights: {normalize: true}});
    sd.draw(f.smiles, svg, dark ? 'dark' : 'light');
  }).catch(() => svg.replaceWith(tuEl('code', null, f.smiles)));
  return fig;
}

function tuFigures(s) {
  const figs = (s.figures || []).filter(f => f && f.smiles);
  if (!figs.length) return null;
  const row = tuEl('div', 'tu-mols');
  figs.forEach(f => row.appendChild(tuMolecule(f)));
  return row;
}

function tuList(items, cls, mark) {
  const ul = tuEl('ul', 'tu-list ' + (cls || ''));
  for (const x of items || []) {
    const li = tuEl('li');
    if (mark) li.appendChild(tuEl('span', 'tu-mark', mark));
    li.appendChild(tuInline(x));
    ul.appendChild(li);
  }
  return ul;
}

function tuModel(q, open) {
  const det = tuEl('details', 'tu-ans');
  det.open = !!open;
  det.appendChild(tuEl('summary', null, 'Model answer'));
  if (q.kind === 'mcq') {
    const ans = (q.options || [])[q.answer_index];
    det.appendChild(tuMd('**' + TU_LETTER[q.answer_index] + '.** ' + (ans || '')
                         + (q.why ? '\n\n' + q.why : '')));
  } else det.appendChild(q.model_answer_md ? tuMd(q.model_answer_md)
                         : tuEl('p', 'tu-note', 'No model answer written for this one yet.'));
  return det;
}

function tuQHead(c) {
  const q = c.q;
  const head = tuEl('p', 'tu-k');
  head.appendChild(tuEl('span', null, q.kind === 'pyq'
    ? 'Past paper · ' + (q.sittings || []).slice(0, 3).join(', ')
      + ((q.sittings || []).length > 3 ? ' +' + (q.sittings.length - 3) : '')
    : c.s.title));
  if (q.marks && q.kind !== 'mcq') head.appendChild(tuEl('span', 'tu-marks',
    '[' + (+q.marks) + ' mark' + (+q.marks === 1 ? '' : 's') + ']'));
  return head;
}

function tuPxQ(d, P, c, owner) {
  const q = c.q;
  const card = tuEl('div', 'tu-card');
  card.appendChild(tuQHead(c));
  card.appendChild(tuMd(q.q || ''));
  if (q.figure) {
    const img = tuEl('img', 'tu-fig');
    img.src = q.figure;
    img.alt = 'The figure printed with this question';
    img.loading = 'lazy';
    card.appendChild(img);
  }
  const g = c.grade;
  if (q.kind === 'mcq') {
    const list = tuEl('div', 'tu-opts');
    (q.options || []).forEach((o, i) => {
      const b = tuEl('button', 'tu-opt');
      b.appendChild(tuEl('span', 'tu-letter', TU_LETTER[i]));
      b.appendChild(tuInline(o));
      if (c.st !== 'open') {
        b.disabled = true;
        if (i === q.answer_index && (c.st !== 'sending')) b.classList.add('right');
        else if (i === c.picked) b.classList.add('wrong');
      } else if (owner) b.onclick = () => { c.picked = i; tuPxSend(d, P, c, {picked: i}); };
      list.appendChild(b);
    });
    card.appendChild(list);
  } else if (c.st === 'open' || c.st === 'sending') {
    if (owner) {
      const ta = tuEl('textarea', 'tu-px-ta');
      ta.rows = q.kind === 'long' ? 6 : 3;
      ta.maxLength = 6000;
      ta.placeholder = 'Write it the way you would in the exam';
      ta.setAttribute('aria-label', 'Your answer');
      ta.value = c.text || '';
      ta.disabled = c.st === 'sending';
      ta.oninput = () => { c.text = ta.value; };
      card.appendChild(ta);
    }
  } else if (c.text && c.st === 'graded') {
    card.appendChild(tuEl('p', 'said tu-mine', c.text));
  }
  if (c.st === 'open' && owner) {
    const row = tuEl('div', 'tu-acts');
    if (q.kind !== 'mcq') {
      const check = tuEl('button', 'w tu-btn primary', 'Check my answer');
      check.onclick = () => {
        if (!(c.text || '').trim()) return void card.querySelector('textarea').focus();
        tuPxSend(d, P, c, {answer: c.text});
      };
      row.appendChild(check);
    }
    const idk = tuEl('button', 'w tu-btn', 'I don’t know');
    idk.onclick = () => tuPxSend(d, P, c, {idk: true});
    const sol = tuEl('button', 'w tu-btn', 'Show solution');
    sol.onclick = () => tuPxSend(d, P, c, {reveal: true});
    row.append(idk, sol);
    if (P.busy) row.querySelectorAll('button').forEach(b => { b.disabled = true; });
    card.appendChild(row);
  }
  if (c.st === 'sending') card.appendChild(tuEl('p', 'tu-note',
    q.kind === 'mcq' || c.picked !== undefined ? 'Marking…' : 'Marking your answer…'));
  if (c.st === 'revealed') card.appendChild(tuModel(q, true));
  if (g && c.st === 'graded') {
    const out = g.outcome;
    const word = out === 'right' ? 'Right.' : out === 'partial' ? 'Partly right.' : 'Not quite.';
    if (!(q.kind === 'mcq' && out === 'wrong')) {
      card.appendChild(tuEl('p', 'tu-verdict ' + (out === 'right' ? 'right' : out === 'wrong' ? 'wrong' : 'part'),
        (g.verdict && q.kind !== 'mcq' ? g.verdict : word)
        + (q.kind !== 'mcq' && g.score != null ? ' (' + Math.round(g.score * (+q.marks || 1) * 2) / 2
           + '/' + (+q.marks || 1) + ')' : '')));
    } else card.appendChild(tuEl('p', 'tu-verdict wrong', 'Not quite. It is '
      + TU_LETTER[q.answer_index] + '.'));
    if (q.kind === 'mcq' && q.why && out === 'right') card.appendChild(tuMd(q.why, 'md tu-md tu-why'));
    if ((g.missed || []).length && out !== 'wrong') {
      card.appendChild(tuEl('p', 'tu-k', 'You missed'));
      card.appendChild(tuList(g.missed, 'miss', '✕'));
    }
    if (out !== 'wrong') {
      if (out === 'right' && (c.s.key_points || []).length) {
        card.appendChild(tuEl('p', 'tu-k', 'Remember'));
        card.appendChild(tuList(c.s.key_points, 'keep', '•'));
      }
      if (q.kind !== 'mcq') card.appendChild(tuModel(q, out === 'partial'));
    }
  }
  return card;
}

function tuPxTeach(d, P, c) {
  const card = tuEl('div', 'tu-card teach');
  card.appendChild(tuEl('p', 'tu-k', (c.idk ? 'Here is how it works' : 'Let’s fix this')
                                     + ' · ' + c.s.title));
  if (P.basics && c.s.basics_md) {
    card.appendChild(tuMd(c.s.basics_md));
    card.appendChild(tuEl('hr', 'tu-hr'));
  }
  if (c.s.theory_md) card.appendChild(tuMd(c.s.theory_md));
  const figs = tuFigures(c.s);
  if (figs) card.appendChild(figs);
  card.appendChild(tuModel(c.q, true));
  card.appendChild(tuEl('p', 'tu-note', 'Anything unclear? Ask below, or carry on.'));
  return card;
}

function tuPxIntro(c) {
  const card = tuEl('div', 'tu-card intro');
  card.appendChild(tuEl('p', 'tu-k', c.s.title));
  if (c.mix === 'theory' && c.s.theory_md) card.appendChild(tuMd(c.s.theory_md));
  if ((c.s.key_points || []).length) card.appendChild(tuList(c.s.key_points, 'keep', '•'));
  const figs = c.mix === 'theory' && tuFigures(c.s);
  if (figs) card.appendChild(figs);
  return card;
}

function tuPxDone(d, P) {
  const card = tuEl('div', 'tu-card done');
  const subs = d.subtopics || [];
  const counts = {green: 0, yellow: 0, red: 0};
  subs.forEach(s => { const p = d.progress[s.id]; if (p && counts[p.status] != null) counts[p.status]++; });
  card.appendChild(tuEl('p', 'tu-k', 'Topic done'));
  card.appendChild(tuEl('p', 'tu-lead-line', counts.red
    ? 'Done, with ' + counts.red + ' part' + (counts.red === 1 ? '' : 's') + ' to come back to. '
      + 'The tutor remembers which, and brings them up next time.'
    : counts.yellow ? 'Done. A couple of small gaps, noted for revision.'
    : 'All clear. Every part of this topic, including what the papers ask.'));
  const row = tuEl('div', 'tu-acts');
  if (d.next && d.owner) {
    const nx = tuEl('button', 'w tu-btn primary', 'Next: ' + d.next.topic);
    nx.onclick = () => { P.stay = true; tuOpenTopic(d.code, d.next.unit, d.next.topic); };
    row.appendChild(nx);
    if (!P.stay && !P.timer) {
      P.timer = setTimeout(() => {
        P.timer = null;
        const t = view.tutor || {};
        const at = t.practice && tuPlace(t.code, t.practice[0], t.practice[1]);
        if (!P.stay && at && at.topic === d.topic) tuOpenTopic(d.code, d.next.unit, d.next.topic);
      }, 8000);
      row.appendChild(tuEl('span', 'tu-note', 'Moving on in a few seconds…'));
    }
  }
  const again = tuEl('button', 'w tu-btn', 'Practise this again');
  again.onclick = () => {
    P.stay = true;
    clearTimeout(P.timer);
    P.timer = null;
    Object.assign(P, {again: true, seen: new Set(), visit: {}, lastSub: null,
                      feed: P.feed.filter(x => x.k !== 'done')});
    tuPxAdvance(d, P);
    tuRender();
  };
  if (d.owner) row.appendChild(again);
  if (!P.stay && P.timer) {
    const stay = tuEl('button', 'w tu-btn', 'Stay here');
    stay.onclick = () => { P.stay = true; clearTimeout(P.timer); P.timer = null; tuRender(); };
    row.appendChild(stay);
  }
  card.appendChild(row);
  return card;
}

function tuPxSide(d, P) {
  const side = tuEl('nav', 'tu-side');
  side.setAttribute('aria-label', 'Parts of this topic');
  const subs = d.subtopics || [];
  const cur = [...P.feed].reverse().find(c => c.k === 'q');
  let done = 0;
  for (const s of subs) {
    const p = d.progress[s.id];
    if (p && (p.right + p.wrong) && tuSubDone(Object.assign({}, d), Object.assign({}, P, {again: false}), s)) done++;
    const b = tuEl('button', cur && cur.s.id === s.id ? 'on' : '');
    const dot = tuEl('i', 'tu-dot' + (p && (p.right + p.wrong) ? ' ' + p.status : ''));
    dot.title = p && (p.right + p.wrong) ? TU_STATUS[p.status] : 'Not started';
    b.appendChild(dot);
    b.appendChild(tuInline(s.title));
    if (p && (p.right + p.wrong)) b.appendChild(tuEl('small', null, p.right + '/' + (p.right + p.wrong)));
    if (d.owner) b.onclick = () => {
      if (P.busy) return;
      P.feed = P.feed.filter(c => !(c.k === 'q' && c.st === 'open'));
      P.focus = s.id;
      P.lastSub = null;
      tuPxAdvance(d, P);
      tuRender();
    };
    side.appendChild(b);
  }
  side.prepend(tuEl('p', 'tu-k', done + ' of ' + subs.length + ' done'));
  return side;
}

function tuPractice(root, code, u, t) {
  const sv = tuNeed(tuU.subj(code));
  if (sv === undefined) return void root.appendChild(waitline('Opening the topic…'));
  if (sv.error) return void tuFailed(root, sv, 'This subject did not open.');
  const at = tuPlace(code, u, t);
  if (!at) return void tuBlank(root, 'No such topic.', 'The syllabus has moved; go back and pick it again.');
  const d = tuNeed(tuPxUrl(code, at.unit, at.topic));
  if (d === undefined) return void root.appendChild(waitline('Opening the topic…'));
  if (d.error) return void tuFailed(root, d, 'This topic did not open.');
  const P = tuPx(code, at.unit, at.topic);
  if (!P.feed.length) tuPxAdvance(d, P);

  const head = tuEl('div', 'tu-px-head');
  head.appendChild(tuEl('p', 'tu-k', at.unit));
  const meta = [];
  if (d.source === 'pack') meta.push('From the teaching pack; the full course for this topic is being written');
  if (d.mastery && d.mastery.note && d.mastery.note !== 'clear so far') meta.push(d.mastery.note);
  if (meta.length) head.appendChild(tuEl('p', 'tu-note', meta.join(' · ')));
  if (d.owner) {
    const prefs = tuEl('div', 'tu-prefs w');
    prefs.appendChild(tuSeg('Lesson', TU_MIX, tuPref(code).mix, v => tuSetPref(code, 'mix', v)));
    head.appendChild(prefs);
  }
  root.appendChild(head);

  const grid = tuEl('div', 'tu-px');
  grid.appendChild(tuPxSide(d, P));
  const feed = tuEl('div', 'tu-feed');
  if ((d.doubts || []).length) {
    const old = tuEl('details', 'tu-ans tu-old');
    old.appendChild(tuEl('summary', null, 'Earlier doubts in this topic (' + Math.ceil(d.doubts.length / 2) + ')'));
    for (const m of d.doubts) old.appendChild(m.role === 'student'
      ? tuEl('p', 'said tu-mine', m.body) : tuMd(m.body));
    feed.appendChild(old);
  }
  for (const c of P.feed) {
    if (c.k === 'q') feed.appendChild(tuPxQ(d, P, c, d.owner));
    else if (c.k === 'teach') feed.appendChild(tuPxTeach(d, P, c));
    else if (c.k === 'intro') feed.appendChild(tuPxIntro(c));
    else if (c.k === 'done') feed.appendChild(tuPxDone(d, P));
    else if (c.k === 'said') feed.appendChild(tuEl('p', 'said tu-mine tu-doubt', c.body));
    else if (c.k === 'reply') {
      const r = c.live ? tuEl('div', 'md tu-md tu-reply') : tuMd(c.body || '', 'md tu-md tu-reply');
      if (c.live) { r.id = 'tu-px-live'; r.innerHTML = tuSafe(c.body) || '<p class="tu-note">Thinking…</p>'; }
      feed.appendChild(r);
      if (c.error) feed.appendChild(tuEl('p', 'tu-note bad', c.error));
    }
  }
  const last = P.feed[P.feed.length - 1];
  if (d.owner && last && !(last.k === 'q' && last.st !== 'graded' && last.st !== 'revealed')
      && last.k !== 'done' && !P.busy) {
    const row = tuEl('div', 'tu-acts tu-cont');
    const cont = tuEl('button', 'w tu-btn primary', 'Continue');
    cont.onclick = () => { tuPxAdvance(d, P); tuRender(); };
    row.appendChild(cont);
    feed.appendChild(row);
  }
  if (d.owner) {
    const box = tuEl('div', 'tu-compose tu-px-ask w');
    const ta = tuEl('textarea');
    ta.rows = 1;
    ta.maxLength = 4000;
    ta.placeholder = P.busy ? 'Working…' : 'A doubt? Ask here, or say "explain this again"';
    ta.setAttribute('aria-label', 'Ask a doubt');
    ta.value = P.doubt;
    ta.oninput = () => { P.doubt = ta.value; };
    const send = tuEl('button', 'tu-send', 'Ask');
    const fire = () => {
      const body = ta.value.trim();
      if (!body || P.busy) return;
      P.doubt = '';
      tuPxDoubt(d, P, body);
    };
    ta.onkeydown = e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); fire(); }
    };
    send.disabled = P.busy;
    send.onclick = fire;
    box.append(ta, send);
    feed.appendChild(box);
  }
  grid.appendChild(feed);
  root.appendChild(grid);
  if (P.scroll) {
    P.scroll = false;
    requestAnimationFrame(() => {
      const cards = root.querySelectorAll('.tu-feed > .tu-card, .tu-feed > .tu-reply');
      const el = cards[cards.length - 1];
      if (el) el.scrollIntoView({block: 'start', behavior: 'smooth'});
    });
  }
}

// ---- Learn: the topic to read --------------------------------------------
// The written course as a page: each subtopic's notes, then its questions,
// every one with Show solution and MCQs that mark themselves here. Nothing is
// sent anywhere and nothing is recorded, so it costs nothing and any trusted
// reader can use it.

const TU_SHOWN = new Set();     // question keys whose solution is open
const TU_PICKED = {};           // question key -> option picked

function tuLearnQ(key, q, n) {
  const card = tuEl('div', 'tu-card');
  const head = tuEl('p', 'tu-k');
  head.appendChild(tuEl('span', null, 'Q' + n + (q.kind === 'pyq'
    ? ' · Past paper · ' + (q.sittings || []).slice(0, 3).join(', ') : '')));
  if (q.marks && q.kind !== 'mcq') head.appendChild(tuEl('span', 'tu-marks',
    '[' + (+q.marks) + ' mark' + (+q.marks === 1 ? '' : 's') + ']'));
  card.appendChild(head);
  card.appendChild(tuMd(q.q || ''));
  if (q.figure) {
    const img = tuEl('img', 'tu-fig');
    img.src = q.figure;
    img.alt = 'The figure printed with this question';
    img.loading = 'lazy';
    card.appendChild(img);
  }
  const picked = TU_PICKED[key];
  if (q.kind === 'mcq') {
    const list = tuEl('div', 'tu-opts');
    (q.options || []).forEach((o, i) => {
      const b = tuEl('button', 'tu-opt');
      b.appendChild(tuEl('span', 'tu-letter', TU_LETTER[i]));
      b.appendChild(tuInline(o));
      if (picked !== undefined) {
        b.disabled = true;
        if (i === q.answer_index) b.classList.add('right');
        else if (i === picked) b.classList.add('wrong');
      } else b.onclick = () => { TU_PICKED[key] = i; TU_SHOWN.add(key); tuRender(); };
      list.appendChild(b);
    });
    card.appendChild(list);
  }
  if (TU_SHOWN.has(key)) card.appendChild(tuModel(q, true));
  else {
    const row = tuEl('div', 'tu-acts');
    const sol = tuEl('button', 'w tu-btn', 'Show solution');
    sol.onclick = () => { TU_SHOWN.add(key); tuRender(); };
    row.appendChild(sol);
    card.appendChild(row);
  }
  return card;
}

function tuLearn(root, code, u, t) {
  const sv = tuNeed(tuU.subj(code));
  if (sv === undefined) return void root.appendChild(waitline('Opening the topic…'));
  if (sv.error) return void tuFailed(root, sv, 'This subject did not open.');
  const at = tuPlace(code, u, t);
  if (!at) return void tuBlank(root, 'No such topic.', 'The syllabus has moved; go back and pick it again.');
  const d = tuNeed(tuPxUrl(code, at.unit, at.topic));
  if (d === undefined) return void root.appendChild(waitline('Opening the topic…'));
  if (d.error) return void tuFailed(root, d, 'This topic did not open.');

  const head = tuEl('div', 'tu-px-head');
  head.appendChild(tuEl('p', 'tu-k', at.unit + ' · Learn'));
  head.appendChild(tuEl('p', 'tu-note', 'The notes and every question for this topic. '
    + 'Nothing here is marked or sent anywhere.'));
  const row = tuEl('div', 'tu-acts');
  if (d.owner) {
    const p = tuEl('button', 'w tu-btn primary', 'Practise this topic');
    p.onclick = () => tuOpenTopic(code, at.unit, at.topic, 'p');
    row.appendChild(p);
  }
  if (d.next) {
    const nx = tuEl('button', 'w tu-btn', 'Next: ' + d.next.topic);
    nx.onclick = () => tuOpenTopic(code, d.next.unit, d.next.topic, 'l');
    row.appendChild(nx);
  }
  head.appendChild(row);
  root.appendChild(head);

  const grid = tuEl('div', 'tu-px');
  const side = tuEl('nav', 'tu-side');
  side.setAttribute('aria-label', 'Parts of this topic');
  side.appendChild(tuEl('p', 'tu-k', (d.subtopics || []).length + ' parts'));
  const feed = tuEl('div', 'tu-feed');
  let n = 0;
  (d.subtopics || []).forEach((s, i) => {
    const id = 'tu-l-' + i;
    const b = tuEl('button');
    b.appendChild(tuInline(s.title));
    b.onclick = () => document.getElementById(id)?.scrollIntoView({block: 'start', behavior: 'smooth'});
    side.appendChild(b);
    const notes = tuEl('div', 'tu-card intro');
    notes.id = id;
    notes.appendChild(tuEl('p', 'tu-k', s.title));
    if (s.theory_md) notes.appendChild(tuMd(s.theory_md));
    const figs = tuFigures(s);
    if (figs) notes.appendChild(figs);
    if ((s.key_points || []).length) {
      notes.appendChild(tuEl('p', 'tu-k', 'Remember'));
      notes.appendChild(tuList(s.key_points, 'keep', '•'));
    }
    feed.appendChild(notes);
    for (const q of s.questions || []) feed.appendChild(tuLearnQ(d.code + '|' + s.id + '|' + q.id, q, ++n));
  });
  grid.append(side, feed);
  root.appendChild(grid);
}
