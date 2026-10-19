"""The AI tutor, server side. notes.py routes every /tutor request here.

One person is taught; everybody the section trusts may read what was taught.
The database says who may write (0056's policies) and this file never tries to
be a second opinion on that -- it only decides what to ask the model, what to
store, and how to say it back. The spec is docs/specs/2026-10-19-ai-tutor-design.md.

The model is reached through exactly one function, model_stream(), so the
tests can swap it for a canned lesson and nothing else needs to know.
"""

import ast
import datetime
import json
import os
import re
import threading
import urllib.parse
import uuid
from pathlib import Path

import psycopg

import notes

HERE = Path(__file__).resolve().parent
# Module globals rather than constants inlined at the call: the tests point
# them at a temporary folder, and a pack written by hand is picked up on the
# next request without a restart.
PACK_DIR = HERE / "tutor" / "packs"
SCHEME_PATH = HERE / "tutor" / "exam-scheme.json"

EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Two teachers. Standard is the everyday one; advanced is the best model
# with the whole unit's past papers and the student's picture from every
# subject in front of it, at a few times the spend. Both think at medium
# effort: high effort on Opus roughly doubled the bill for no visible gain
# in the lessons read on 2026-10-19.
TIERS = ("standard", "advanced")
TIER_DEFAULTS = {"standard": ("claude-sonnet-5-5", "medium"),
                 "advanced": ("claude-opus-5-5", "medium")}
TIER_ENV = {"standard": ("RECARVE_TUTOR_MODEL", "RECARVE_TUTOR_EFFORT"),
            "advanced": ("RECARVE_TUTOR_MODEL_ADVANCED", "RECARVE_TUTOR_EFFORT_ADVANCED")}
DEFAULT_MODEL = TIER_DEFAULTS["standard"][0]
MAX_TOKENS = 16000

MODES = ("teach", "quiz", "pyq", "revise", "ask")
MODE_LABEL = {"teach": "Teach", "quiz": "Quiz", "pyq": "PYQs", "revise": "Revise", "ask": "Ask"}
LANGS = ("english", "hinglish")
DEPTHS = ("basics", "exam", "deep")
IMPORTANCE = ("low", "medium", "high", "must")
EXAM_ORDER = {"end": 0, "mid": 1, "mini": 2}

# Section 5's weights. Unknown sits between low and medium: a topic nobody has
# judged yet is more likely to matter than one somebody called unimportant.
WEIGHT = {"must": 4.0, "high": 3.0, "medium": 2.0, "low": 1.0}
WEIGHT_UNKNOWN = 1.5

# A quick-action tap is stored as the sentence it stands for, so the model --
# and a classmate reading the lesson later -- sees what was asked, not a code.
ACTIONS = {
    "deeper": "Go deeper on this: the reasoning underneath it, the derivation, "
              "and the edge cases an end-term question would push on.",
    "example": "Give me a worked example of this, step by step, the way it "
               "would be asked in the exam.",
    "simpler": "That was too much at once. Explain it again more simply, in "
               "smaller steps, with an everyday example first.",
    "quiz": "Quiz me on this now: one question at a time, and grade each "
            "answer before you ask the next.",
    "pyq": "Show me the past-paper questions on this and walk me through how "
           "to answer one of them for full marks.",
    "summary": "Summarise this topic for my revision: the points I must be able "
               "to write, the formulas, the diagrams, and the traps.",
    "next": "I think I have this one. Wrap it up in two or three lines, tell me "
            "honestly whether I am exam-ready on it, and tell me what to study "
            "next and why.",
}

BODY_CAP = 64 * 1024
IMPORT_CAP = 16 * 1024 * 1024     # a whole subject's papers, typed out
SAY_MAX = 20000                   # one student turn, in characters
PYQ_PROMPT_MAX = 40               # questions in the system prompt, newest first
PYQ_PROMPT_CHARS = 60000
HISTORY_CHARS = 600000            # a lesson longer than this loses its middle
IST = "Asia/Kolkata"              # what "today" means on the spend line


class Refused(Exception):
    """Somebody may read this but not do that: a 403."""


class NotFound(Exception):
    """A 404. Not LookupError: a stray KeyError must stay a 500."""


class Busy(Exception):
    """The tutor is still answering this lesson: a 409."""


def model_name(tier="standard"):
    tier = tier if tier in TIERS else "standard"
    return os.environ.get(TIER_ENV[tier][0], "").strip() or TIER_DEFAULTS[tier][0]


def effort_level(tier="standard"):
    tier = tier if tier in TIERS else "standard"
    got = os.environ.get(TIER_ENV[tier][1], "").strip().lower()
    return got if got in EFFORTS else TIER_DEFAULTS[tier][1]


def tiers():
    """What each tier runs on, for the screen to say before the spend."""
    return {t: {"model": model_name(t), "effort": effort_level(t),
                "price_in": notes.price_of(model_name(t))[0]} for t in TIERS}


def may_write(me):
    """`owner` in every response: may this person make the tutor teach."""
    if not me or me.get("visitor"):
        return False
    return notes.RANK.get(me.get("role"), 0) >= notes.RANK[notes.TUTOR_ROLE]


# ------------------------------------------------------------------ files

_files = {}
_files_lock = threading.Lock()


def _read_json(path):
    """A JSON file, re-read only when its mtime moves. None when it is missing
    or broken -- a half-written pack must not take the tutor down with it."""
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    key = str(path)
    with _files_lock:
        hit = _files.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        notes.log(f"{path.name}: {e}", "tutor")
        value = None
    with _files_lock:
        _files[key] = (stamp, value)
    return value


def load_pack(code):
    if code not in notes.SUBJECTS:
        return None   # also keeps a crafted code out of the path
    got = _read_json(Path(PACK_DIR) / f"{code}.json")
    return got if isinstance(got, dict) else None


def exam_scheme():
    got = _read_json(Path(SCHEME_PATH))
    return got if isinstance(got, dict) else {}


def syllabus(code):
    """(units, estimated). syllabus.json wins; a pack fills in a subject it
    has nothing for (HS1112), and says so."""
    units = notes.SYLLABUS.get(code) or []
    if units:
        return [{"unit": u["unit"], "topics": list(u.get("topics") or [])} for u in units], False
    pack = load_pack(code) or {}
    got = []
    for u in pack.get("units") or []:
        if not isinstance(u, dict) or not u.get("unit"):
            continue
        topics = [t["topic"] for t in u.get("topics") or []
                  if isinstance(t, dict) and isinstance(t.get("topic"), str) and t["topic"]]
        got.append({"unit": str(u["unit"]), "topics": topics})
    return got, bool(got)


def tutor_codes():
    """Every subject the tutor can teach, in the timetable's order."""
    return [c for c in notes.SUBJECTS if syllabus(c)[0]]


def subject_name(code):
    pack = load_pack(code) or {}
    if isinstance(pack.get("name"), str) and pack["name"]:
        return pack["name"]
    return notes.SUBJECTS[code][0].replace("-", " ") if code in notes.SUBJECTS else code


def pack_topic(code, unit, topic):
    """The pack's entry for one topic, matched on the exact names first and
    then without regard to case, because packs are written by hand."""
    pack = load_pack(code)
    if not pack or not topic:
        return None
    loose = None
    for u in pack.get("units") or []:
        if not isinstance(u, dict):
            continue
        for t in u.get("topics") or []:
            if not isinstance(t, dict) or not isinstance(t.get("topic"), str):
                continue
            if t["topic"] == topic and (unit is None or u.get("unit") == unit):
                return t
            if loose is None and t["topic"].strip().lower() == topic.strip().lower():
                loose = t
    return loose


def place(code, unit, topic):
    """(unit, topic) checked against the syllabus, with the unit found when
    only the topic is named. ValueError for anything not on it."""
    units, _ = syllabus(code)
    if topic:
        for u in units:
            if (unit is None or u["unit"] == unit) and topic in u["topics"]:
                return u["unit"], topic
        raise ValueError(f"{topic!r} is not on the {code} syllabus"
                         + (f" under {unit!r}" if unit else ""))
    if unit:
        if any(u["unit"] == unit for u in units):
            return unit, None
        raise ValueError(f"{unit!r} is not a unit of {code}")
    return None, None


# ------------------------------------------------------------- the tags

CHECK_RE = re.compile(
    r"<check\b([^>]*)>(.*?)(?:</check\s*>|(?=<check\b)|(?=<state\b)|\Z)", re.S | re.I)
PYQ_RE = re.compile(r"<pyq\b([^>]*?)/?>", re.I)
STATE_RE = re.compile(r"<state\b[^>]*>(.*?)(?:</state\s*>|\Z)", re.S | re.I)
ATTR_RE = re.compile(r"""([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>/]+))""")
# LaTeX commands whose first letter is a JSON escape: inside a JSON string
# "\frac" parses -- as a form feed and "rac". Doubled before parsing. The
# list is the ones that turn up in first-year papers, not every command TeX has.
_LATEX = re.compile(
    r"(?<!\\)\\(?=(?:frac|dfrac|beta|bar|binom|bigg?|boldsymbol|bullet|bot|bmod|because"
    r"|backslash|nabla|neq|ne|nu|not|nonumber|ni|rho|right|rightarrow|rightleftharpoons"
    r"|rangle|rm|rfloor|rceil|theta|tau|times|text|textbf|tan|tanh|to|top|triangle"
    r"|tilde|therefore|underline|uparrow|upsilon|underbrace|forall|frown)\b)")
_BAD_ESCAPE = re.compile(r'(?<!\\)\\(?!["\\/bfnrtu])')


def _attrs(raw):
    return {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None)
            for m in ATTR_RE.finditer(raw or "")}


def loose_json(text):
    """A dict out of what the model meant to be JSON, or None.

    Strict first; then the usual ways a model's JSON goes wrong -- a code
    fence round it, prose round it, curly quotes, a trailing comma, LaTeX
    backslashes nobody doubled, single quotes, a turn cut off before the
    closing brace."""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    if not s:
        return None
    base = [s]
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        base.append(s[i:j + 1])
    elif i != -1:
        base.append(s[i:] + "}")
    tries = []
    for c in base:
        latex = _BAD_ESCAPE.sub(r"\\\\", _LATEX.sub(r"\\\\", c))
        tries += [latex, c]
        fixed = (latex.replace("“", '"').replace("”", '"')
                 .replace("‘", "'").replace("’", "'"))
        tries.append(re.sub(r",\s*([}\]])", r"\1", fixed))
    for c in tries:
        try:
            got = json.loads(c)
        except ValueError:
            continue
        if isinstance(got, dict):
            return got
    for c in tries:
        py = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False",
                                                re.sub(r"\bnull\b", "None", c)))
        try:
            got = ast.literal_eval(py)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
        if isinstance(got, dict):
            return got
    return None


def _clean_check(attrs, raw, n):
    data = loose_json(raw) or {}
    out = {k: v for k, v in data.items() if isinstance(k, str)}
    cid = attrs.get("id") or out.get("id") or f"c{n}"
    kind = str(attrs.get("kind") or out.get("kind")
               or ("mcq" if isinstance(out.get("options"), list) else "short")).lower()
    out["id"] = str(cid)[:40]
    out["kind"] = kind if kind in ("mcq", "short") else "short"
    if out["kind"] == "mcq":
        opts = out.get("options")
        out["options"] = [str(o) for o in opts] if isinstance(opts, list) else []
        ans = out.get("answer")
        try:
            ans = int(ans)
        except (TypeError, ValueError):
            ans = None
        out["answer"] = ans if ans is not None and 0 <= ans < len(out["options"]) else None
    if not isinstance(out.get("q"), str) or not out["q"].strip():
        # Unreadable JSON still becomes a card with the words in it, rather
        # than a question the student was promised and never shown.
        out["q"] = (raw or "").strip()[:2000]
        out["broken"] = True
    return out


def _clean_state(data):
    if not isinstance(data, dict):
        return None
    out = {}
    if "level" in data:
        try:
            out["level"] = max(0, min(5, int(round(float(data["level"])))))
        except (TypeError, ValueError):
            pass
    imp = str(data.get("importance") or "").strip().lower()
    if imp in IMPORTANCE:
        out["importance"] = imp
    for key, cap in (("why", 300), ("note", 500), ("profile", 1500), ("next", 200)):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()[:cap]
    if "done" in data:
        v = data["done"]
        out["done"] = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes")
    return out


def parse_turn(text):
    """{"state", "checks", "pyqs"} out of one tutor turn (spec section 3).

    Never raises: a turn the model garbled is still a turn the student read.
    The last <state> wins, because the rule is that it comes last."""
    text = text or ""
    checks = [_clean_check(_attrs(m.group(1)), m.group(2), n)
              for n, m in enumerate(CHECK_RE.finditer(text), 1)]
    pyqs = []
    for m in PYQ_RE.finditer(text):
        got = _attrs(m.group(1)).get("id", "")
        try:
            got = str(uuid.UUID(got))
        except ValueError:
            continue
        if got not in pyqs:
            pyqs.append(got)
    states = STATE_RE.findall(text)
    state = _clean_state(loose_json(states[-1])) if states else None
    return {"state": state, "checks": checks, "pyqs": pyqs}


def strip_tags(text):
    """What a person sees of a turn, roughly: for previews, not rendering."""
    text = STATE_RE.sub("", text or "")
    text = CHECK_RE.sub(" [question] ", text)
    return PYQ_RE.sub(" [past paper] ", text)


def last_line(text):
    lines = [re.sub(r"\*\*|`", "", ln).strip(" #>*-\t")
             for ln in strip_tags(text).splitlines()]
    lines = [ln for ln in lines if ln and ln not in ("[question]", "[past paper]")]
    return (lines[-1][:140] if lines else "")


# ---------------------------------------------------------------- shapes

def _iso(v):
    return v.isoformat() if isinstance(v, (datetime.datetime, datetime.date)) else v


def _num(v):
    return float(v) if v is not None else None


def _uuid(v, what="id"):
    try:
        return str(uuid.UUID(str(v)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{what} is not an id")


SESSION_SQL = (
    "select s.id, s.subject_code, s.unit, s.topic, s.mode, s.lang, s.depth, s.title,"
    " s.status, s.turns, s.cost_usd, s.created_at, s.updated_at, s.owner_id, p.name,"
    " s.model, s.tier, (select m.body from tutor_messages m where m.session_id = s.id"
    "           order by m.id desc limit 1)"
    " from tutor_sessions s left join profiles p on p.id = s.owner_id")


def _session(row, me_id=None):
    return {"id": str(row[0]), "code": row[1], "unit": row[2], "topic": row[3],
            "mode": row[4], "lang": row[5], "depth": row[6], "title": row[7],
            "status": row[8], "turns": row[9], "cost_usd": _num(row[10]),
            "created_at": _iso(row[11]), "updated_at": _iso(row[12]),
            "owner_id": str(row[13]), "owner_name": row[14], "model": row[15],
            "tier": row[16],
            "mine": me_id is not None and str(row[13]) == str(me_id),
            "last_line": last_line(row[17]) if row[17] else ""}


def _message(row):
    return {"id": row[0], "role": row[1], "kind": row[2], "body": row[3],
            "meta": row[4] or {}, "cost_usd": _num(row[5]), "created_at": _iso(row[6])}


MESSAGE_SQL = ("select id, role, kind, body, meta, cost_usd, created_at"
               " from tutor_messages")

PYQ_SQL = (
    "select q.id, q.subject_code, q.exam, q.year, q.set_for_section, q.qno, q.marks,"
    " q.unit, q.topic, q.question_md, q.answer_md, d.title, d.file_key, q.figure"
    " from pyq_questions q left join archive_documents d on d.id = q.document_id")
PYQ_ORDER = (" order by q.year desc nulls last,"
             " case q.exam when 'end' then 0 when 'mid' then 1 when 'mini' then 2 else 3 end,"
             " length(q.qno), q.qno, q.created_at")


def _pyq(row):
    return {"id": str(row[0]), "code": row[1], "exam": row[2], "year": row[3],
            "section": row[4], "qno": row[5], "marks": _num(row[6]), "unit": row[7],
            "topic": row[8], "question_md": row[9], "answer_md": row[10],
            "title": row[11], "path": ("/" + row[12].lstrip("/")) if row[12] else None,
            "figure": ("/" + row[13].lstrip("/")) if row[13] else None}


MASTERY_COLS = ("subject_code, unit, topic, level, attempts, correct, importance,"
                " importance_why, note, last_seen")


def _mastery(row):
    if not row:
        return None
    return {"code": row[0], "unit": row[1], "topic": row[2], "level": row[3],
            "attempts": row[4], "correct": row[5], "importance": row[6],
            "importance_why": row[7], "note": row[8], "last_seen": _iso(row[9])}


# ---------------------------------------------------------------- db_*
# Each takes a connection already acting as somebody; RLS decides the rest.

def db_session(conn, session_id, me_id=None):
    row = conn.execute(SESSION_SQL + " where s.id = %s", (session_id,)).fetchone()
    if not row:
        raise NotFound("no such lesson")
    return _session(row, me_id)


def db_sessions(conn, me_id=None, code=None, owner_id=None, limit=20):
    where, args = [], []
    if code:
        where.append("s.subject_code = %s")
        args.append(code)
    if owner_id:
        where.append("s.owner_id = %s")
        args.append(owner_id)
    sql = SESSION_SQL + (" where " + " and ".join(where) if where else "")
    sql += " order by s.updated_at desc limit %s"
    return [_session(r, me_id) for r in conn.execute(sql, (*args, limit)).fetchall()]


def db_messages(conn, session_id):
    return [_message(r) for r in conn.execute(
        MESSAGE_SQL + " where session_id = %s order by id", (session_id,)).fetchall()]


def db_add_message(conn, session_id, role, kind, body, meta=None, usage=None, cost=0.0):
    usage = usage or {}
    row = conn.execute(
        "insert into tutor_messages (session_id, role, kind, body, meta, input_tokens,"
        " output_tokens, cache_read_tokens, cache_write_tokens, cost_usd)"
        " values (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)"
        " returning id, role, kind, body, meta, cost_usd, created_at",
        (session_id, role, kind, body, json.dumps(meta or {}),
         usage.get("input_tokens"), usage.get("output_tokens"),
         usage.get("cache_read_tokens"), usage.get("cache_write_tokens"),
         round(cost, 6))).fetchone()
    return _message(row)


def db_create_session(conn, owner_id, code, unit, topic, mode, lang, depth, tier, title, model):
    row = conn.execute(
        "insert into tutor_sessions (owner_id, subject_code, unit, topic, mode, lang,"
        " depth, tier, title, model) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " returning id",
        (owner_id, code, unit, topic, mode, lang, depth, tier, title[:200], model)).fetchone()
    return str(row[0])


def db_bump_session(conn, session_id, cost, turn=True):
    conn.execute(
        "update tutor_sessions set turns = turns + %s, cost_usd = cost_usd + %s,"
        " updated_at = now() where id = %s",
        (1 if turn else 0, round(cost, 6), session_id))


def db_profile(conn, owner_id, code):
    row = conn.execute(
        "select lang, depth, tier, notes, updated_at from tutor_profile"
        " where owner_id = %s and subject_code = %s", (owner_id, code)).fetchone()
    if not row:
        return {"code": code, "lang": "english", "depth": "exam", "tier": "standard",
                "notes": None, "updated_at": None}
    return {"code": code, "lang": row[0], "depth": row[1], "tier": row[2], "notes": row[3],
            "updated_at": _iso(row[4])}


def db_profile_notes(conn, owner_id, but=None):
    """The tutor's picture of this learner in every other subject: how they
    learn carries across, even when what they know does not."""
    return [{"code": r[0], "notes": r[1]} for r in conn.execute(
        "select subject_code, notes from tutor_profile where owner_id = %s"
        " and notes is not null and notes <> '' and subject_code <> coalesce(%s, '')"
        " order by updated_at desc", (owner_id, but)).fetchall()]


def db_set_profile(conn, owner_id, code, lang=None, depth=None, tier=None, notes_text=None):
    if lang is not None and lang not in LANGS:
        raise ValueError(f"lang is one of {', '.join(LANGS)}")
    if depth is not None and depth not in DEPTHS:
        raise ValueError(f"depth is one of {', '.join(DEPTHS)}")
    if tier is not None and tier not in TIERS:
        raise ValueError(f"tier is one of {', '.join(TIERS)}")
    conn.execute(
        "insert into tutor_profile (owner_id, subject_code, lang, depth, tier, notes)"
        " values (%s, %s, coalesce(%s, 'english'), coalesce(%s, 'exam'),"
        " coalesce(%s, 'standard'), %s)"
        " on conflict (owner_id, subject_code) do update set"
        " lang = coalesce(%s, tutor_profile.lang), depth = coalesce(%s, tutor_profile.depth),"
        " tier = coalesce(%s, tutor_profile.tier),"
        " notes = coalesce(%s, tutor_profile.notes), updated_at = now()",
        (owner_id, code, lang, depth, tier, notes_text, lang, depth, tier, notes_text))
    return db_profile(conn, owner_id, code)


def db_topic_mastery(conn, owner_id, code, unit, topic):
    return _mastery(conn.execute(
        f"select {MASTERY_COLS} from tutor_mastery where owner_id = %s and"
        " subject_code = %s and unit = %s and topic = %s",
        (owner_id, code, unit, topic)).fetchone())


def db_mastery(conn, owner_id, code=None):
    """{(code, unit, topic): TopicMastery} for one learner."""
    sql = f"select {MASTERY_COLS} from tutor_mastery where owner_id = %s"
    args = [owner_id]
    if code:
        sql += " and subject_code = %s"
        args.append(code)
    return {(m["code"], m["unit"], m["topic"]): m
            for m in map(_mastery, conn.execute(sql, args).fetchall())}


def db_apply_state(conn, owner_id, code, unit, topic, state):
    """Fold a turn's <state> into the topic's row. Always stamps last_seen:
    a turn about the topic is the topic being seen, state or no state."""
    state = state or {}
    conn.execute(
        "insert into tutor_mastery (owner_id, subject_code, unit, topic, level,"
        " importance, importance_why, note, last_seen)"
        " values (%s, %s, %s, %s, coalesce(%s, 0), %s, %s, %s, now())"
        " on conflict (owner_id, subject_code, unit, topic) do update set"
        " level = coalesce(%s, tutor_mastery.level),"
        " importance = coalesce(%s, tutor_mastery.importance),"
        " importance_why = coalesce(%s, tutor_mastery.importance_why),"
        " note = coalesce(%s, tutor_mastery.note), last_seen = now()",
        (owner_id, code, unit, topic, state.get("level"), state.get("importance"),
         state.get("why"), state.get("note"), state.get("level"),
         state.get("importance"), state.get("why"), state.get("note")))
    return db_topic_mastery(conn, owner_id, code, unit, topic)


def db_set_level(conn, owner_id, code, unit, topic, level):
    """The student's own override ("I know this"). last_seen is left alone:
    saying you know a topic is not studying it."""
    try:
        level = int(level)
    except (TypeError, ValueError):
        raise ValueError("level is a number from 0 to 5")
    if not 0 <= level <= 5:
        raise ValueError("level is a number from 0 to 5")
    conn.execute(
        "insert into tutor_mastery (owner_id, subject_code, unit, topic, level)"
        " values (%s, %s, %s, %s, %s) on conflict (owner_id, subject_code, unit, topic)"
        " do update set level = excluded.level",
        (owner_id, code, unit, topic, level))
    return db_topic_mastery(conn, owner_id, code, unit, topic)


def db_mark(conn, owner_id, session, check_id, picked, correct):
    """One MCQ answered on the card. Returns (TopicMastery or None, message).

    Recorded as a student message as well as a count, so the tutor sees what
    was picked when it reads the lesson back, and so the streak below has a
    history to read. The level rule, kept simple on purpose: two results the
    same way in a row move the level one step (up on two right, down on two
    wrong), and the run then starts again. A check already marked is not
    counted twice.
    """
    check_id = str(check_id or "").strip()[:40]
    if not check_id:
        raise ValueError("check_id is missing")
    marks = conn.execute(
        "select meta from tutor_messages where session_id = %s and role = 'student'"
        " and meta->>'mark' = 'true' order by id desc", (session["id"],)).fetchall()
    marks = [r[0] for r in marks]
    if any(m.get("check_id") == check_id for m in marks):
        mastery = (db_topic_mastery(conn, owner_id, session["code"], session["unit"],
                                    session["topic"]) if session["topic"] else None)
        return mastery, None

    # The card is graded on the phone; the answer it was graded against is in
    # the tutor's own turn, so the server's word is the stored one when it has it.
    check = None
    for (meta,) in conn.execute(
            "select meta from tutor_messages where session_id = %s and role = 'tutor'"
            " order by id desc", (session["id"],)).fetchall():
        check = next((c for c in (meta or {}).get("checks") or []
                      if c.get("id") == check_id), None)
        if check:
            break
    label = str(picked)
    if check and isinstance(picked, int) and 0 <= picked < len(check.get("options") or []):
        label = f"{'ABCD'[picked] if picked < 4 else picked + 1}, “{check['options'][picked]}”"
        if isinstance(check.get("answer"), int):
            correct = picked == check["answer"]
    correct = bool(correct)

    prev = marks[0] if marks else None
    streak = prev.get("streak", 1) + 1 if prev and bool(prev.get("correct")) == correct \
        and prev.get("streak", 1) % 2 else 1
    nudge = (1 if correct else -1) if streak == 2 else 0
    body = (f"(Check {check_id}, answered on the card: I picked {label}, which the "
            f"card marked {'right' if correct else 'wrong'}.)")
    msg = db_add_message(conn, session["id"], "student", "answer", body,
                         {"mark": True, "check_id": check_id, "picked": picked,
                          "correct": correct, "streak": streak})
    if not session["topic"]:
        return None, msg
    conn.execute(
        "insert into tutor_mastery (owner_id, subject_code, unit, topic, level, attempts,"
        " correct, last_seen) values (%s, %s, %s, %s, greatest(0, %s), 1, %s, now())"
        " on conflict (owner_id, subject_code, unit, topic) do update set"
        " attempts = tutor_mastery.attempts + 1,"
        " correct = tutor_mastery.correct + excluded.correct,"
        " level = greatest(0, least(5, tutor_mastery.level + %s)), last_seen = now()",
        (owner_id, session["code"], session["unit"], session["topic"], nudge,
         int(correct), nudge))
    return db_topic_mastery(conn, owner_id, session["code"], session["unit"],
                            session["topic"]), msg


def db_pyqs(conn, code, unit=None, topic=None, year=None, exam=None, ids=None, limit=1000):
    where, args = [], []
    if code:
        where.append("q.subject_code = %s")
        args.append(code)
    if unit:
        where.append("q.unit = %s")
        args.append(unit)
    if topic:
        where.append("q.topic = %s")
        args.append(topic)
    if year is not None:
        where.append("q.year = %s")
        args.append(year)
    if exam:
        where.append("q.exam = %s")
        args.append(exam)
    if ids is not None:
        where.append("q.id = any(%s::uuid[])")
        args.append(list(ids))
    sql = PYQ_SQL + (" where " + " and ".join(where) if where else "") + PYQ_ORDER
    return [_pyq(r) for r in conn.execute(sql + " limit %s", (*args, limit)).fetchall()]


def db_pyq_stats(conn, codes=None):
    """{code: {(unit, topic): {"count", "marks", "exams": {exam: n}}}}, with
    unplaced questions under (None, None) and friends."""
    sql = ("select subject_code, unit, topic, exam, count(*), coalesce(sum(marks), 0)"
           " from pyq_questions")
    args = ()
    if codes:
        sql += " where subject_code = any(%s)"
        args = (list(codes),)
    out = {}
    for code, unit, topic, exam, n, marks in conn.execute(
            sql + " group by 1, 2, 3, 4", args).fetchall():
        st = out.setdefault(code, {}).setdefault(
            (unit, topic), {"count": 0, "marks": 0.0, "exams": {}})
        st["count"] += n
        st["marks"] += float(marks)
        if exam:
            st["exams"][exam] = st["exams"].get(exam, 0) + n
    return out


def db_session_counts(conn, owner_id):
    return {(c, u, t): n for c, u, t, n in conn.execute(
        "select subject_code, unit, topic, count(*) from tutor_sessions"
        " where owner_id = %s and topic is not null group by 1, 2, 3",
        (owner_id,)).fetchall()}


def db_spend(conn, owner_id):
    """What the learner's lessons have cost: in all, today and this month,
    with the day and month in Bhopal's time rather than the server's."""
    row = conn.execute(
        "select coalesce(sum(m.cost_usd), 0),"
        " coalesce(sum(m.cost_usd) filter (where (m.created_at at time zone %s)::date"
        "   = (now() at time zone %s)::date), 0),"
        " coalesce(sum(m.cost_usd) filter (where date_trunc('month', m.created_at at time zone %s)"
        "   = date_trunc('month', now() at time zone %s)), 0)"
        " from tutor_messages m join tutor_sessions s on s.id = m.session_id"
        " where s.owner_id = %s", (IST, IST, IST, IST, owner_id)).fetchone()
    return {"total": round(float(row[0]), 6), "today": round(float(row[1]), 6),
            "month": round(float(row[2]), 6)}


def db_subject_spend(conn, owner_id):
    return {c: round(float(v), 6) for c, v in conn.execute(
        "select subject_code, sum(cost_usd) from tutor_sessions where owner_id = %s"
        " group by 1", (owner_id,)).fetchall()}


def db_learner(conn, me, write, who=None):
    """Whose progress a page shows. An owner sees their own; a reader sees
    the person the tutor has most recently taught (or ?who=), because a
    reader has no lessons of their own and an empty tree helps nobody."""
    if who:
        who = _uuid(who, "who")
    elif write:
        who = me["id"]
    else:
        row = conn.execute("select owner_id from tutor_sessions"
                           " order by updated_at desc limit 1").fetchone()
        who = str(row[0]) if row else me["id"]
    row = conn.execute("select name from profiles where id = %s", (who,)).fetchone()
    return {"id": str(who), "name": row[0] if row else None}


# ------------------------------------------------- importance and the plan

def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _exams_phrase(exams):
    parts = sorted(exams.items(), key=lambda kv: (EXAM_ORDER.get(kv[0], 9), kv[0]))
    return ", ".join(f"{n} {e}" for e, n in parts)


def rank_importance(stats):
    """{(unit, topic): (importance, why)} from one subject's PYQ counts.

    A topic's weight is its question count plus its marks over five, so a
    10-mark derivation outweighs a 2-mark definition asked as often. Ranked
    across the subject's placed topics by quartile: the top quarter is must,
    then high, medium, low. Ties share the better rung."""
    placed = [(k, v) for k, v in (stats or {}).items()
              if k[0] and k[1] and v.get("count")]
    if not placed:
        return {}
    score = {k: v["count"] + v.get("marks", 0) / 5 for k, v in placed}
    order = sorted(score.values(), reverse=True)
    out = {}
    for k, v in placed:
        q = order.index(score[k]) / len(order)
        imp = "must" if q < .25 else "high" if q < .5 else "medium" if q < .75 else "low"
        why = _plural(v["count"], "past-paper question")
        if v.get("exams"):
            why += f" ({_exams_phrase(v['exams'])})"
        if v.get("marks"):
            why += f", {v['marks']:g} marks in all"
        out[k] = (imp, why)
    return out


def subject_view(code, mastery=None, stats=None, session_counts=None, spend=None):
    """SubjectView (spec section 2) from what the queries returned. Pure, so
    the plan and the importance rules can be tested without a database."""
    mastery, stats, session_counts = mastery or {}, stats or {}, session_counts or {}
    units, estimated = syllabus(code)
    ranked = rank_importance(stats)
    any_pyq = any_judged = False
    out_units, levels = [], []
    for u in units:
        topics = []
        for t in u["topics"]:
            m = mastery.get((code, u["unit"], t)) or {}
            st = stats.get((u["unit"], t)) or {}
            entry = pack_topic(code, u["unit"], t)
            imp = why = src = None
            if (u["unit"], t) in ranked:
                (imp, why), src = ranked[(u["unit"], t)], "pyq"
                any_pyq = True
            elif m.get("importance") in IMPORTANCE:
                imp, why, src = m["importance"], m.get("importance_why"), "tutor"
            elif entry and entry.get("importance") in IMPORTANCE:
                imp, why, src = entry["importance"], entry.get("why"), "estimate"
            any_judged = any_judged or src is not None
            level = m.get("level") or 0
            levels.append(level)
            topics.append({
                "topic": t, "level": level, "attempts": m.get("attempts") or 0,
                "correct": m.get("correct") or 0, "last_seen": m.get("last_seen"),
                "importance": imp, "importance_source": src, "why": why,
                "note": m.get("note"),
                "pyq_count": st.get("count", 0), "pyq_marks": st.get("marks", 0.0),
                "pyq_exams": st.get("exams", {}), "has_pack": entry is not None,
                "sessions": session_counts.get((code, u["unit"], t), 0)})
        unit_levels = [x["level"] for x in topics]
        out_units.append({"unit": u["unit"], "topics": topics,
                          "progress": round(sum(unit_levels) / (5 * len(unit_levels)), 4)
                          if unit_levels else 0.0})
    pack = load_pack(code) or {}
    return {"code": code, "name": subject_name(code), "syllabus_estimated": estimated,
            "progress": round(sum(levels) / (5 * len(levels)), 4) if levels else 0.0,
            "importance_source": "pyq" if any_pyq else "estimate" if any_judged else "none",
            "has_pack": bool(pack), "spend": spend or 0.0,
            "pyq_count": sum(v.get("count", 0) for v in stats.values()),
            "units": out_units}


def _when(v):
    if isinstance(v, str):
        try:
            v = datetime.datetime.fromisoformat(v)
        except ValueError:
            return None
    if isinstance(v, datetime.datetime) and v.tzinfo is None:
        v = v.replace(tzinfo=datetime.timezone.utc)
    return v if isinstance(v, datetime.datetime) else None


def _reason(t):
    src, imp = t.get("importance_source"), t.get("importance")
    if src == "pyq":
        exams = t.get("pyq_exams") or {}
        n = t.get("pyq_count", 0)
        if len(exams) == 1 and sum(exams.values()) == n:
            label = {"end": "end-term", "mid": "mid-term", "mini": "mini-test"}.get(
                next(iter(exams)), next(iter(exams)))
            what = _plural(n, f"{label} question")
        else:
            what = _plural(n, "past-paper question")
            if exams:
                what += f" ({_exams_phrase(exams)})"
    elif src == "tutor":
        what = f"the tutor rates it {imp}" + (f": {t['why']}" if t.get("why") else "")
    elif src == "estimate":
        what = f"{imp} by the pack's estimate"
    else:
        what = "on the syllabus"
    level = t.get("level") or 0
    if not t.get("last_seen") and not level:
        return f"{what}, not started yet"
    return f"{what}, you are at level {level}"


def _mode_for(t):
    level = t.get("level") or 0
    if level == 0:
        return "teach"
    if level <= 2:
        return "revise" if t.get("last_seen") else "teach"
    return "pyq" if t.get("pyq_count") else "quiz"


def plan(views, now=None, limit=8):
    """[PlanItem], best first: importance x what is left to learn x recency
    (spec section 5). Ties go to topics earlier in their syllabus, taken
    across subjects in turn, so an empty plan opens every subject's first
    topic rather than eight of one subject's."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    items = []
    for si, v in enumerate(views):
        pos = 0
        for u in v.get("units") or []:
            for t in u.get("topics") or []:
                pos += 1
                w = WEIGHT.get(t.get("importance"), WEIGHT_UNKNOWN)
                level = t.get("level") or 0
                seen = _when(t.get("last_seen"))
                recency = 1.0 if not seen else (
                    0.6 if now - seen < datetime.timedelta(days=2) else 0.8)
                score = w * (1 - level / 5) * recency
                if score <= 0:
                    continue
                items.append(((-score, pos, si), {
                    "code": v["code"], "unit": u["unit"], "topic": t["topic"],
                    "reason": _reason(t), "score": round(score, 4),
                    "suggested_mode": _mode_for(t)}))
    items.sort(key=lambda kv: kv[0])
    return [it for _, it in items[:limit]]


def subject_views(conn, learner_id, codes=None):
    codes = codes or tutor_codes()
    mastery = db_mastery(conn, learner_id)
    stats = db_pyq_stats(conn, codes)
    counts = db_session_counts(conn, learner_id)
    spend = db_subject_spend(conn, learner_id)
    return [subject_view(c, mastery, stats.get(c), counts, spend.get(c)) for c in codes]


# --------------------------------------------------------------- prompts

PERSONA = r"""You are the tutor inside recarve, a study app for first-year B.Tech students at MANIT Bhopal (Maulana Azad National Institute of Technology). You are teaching one student, one to one. Usually they missed the lecture, or sat through it and did not follow, and there is no recording. They want to understand the subject and to be ready for the mini test, the mid term and the end term. You are their teacher for this hour: patient, exact, and honest with them.

## How you teach

- Small steps. One idea per turn: say it plainly, show it once (a worked line, a concrete number, a picture in words), then check it landed. Do not pour the whole topic into one turn. A teaching turn is usually 120 to 350 words; go longer only for a worked example, a derivation or a summary the student asked for.
- Start from where the student is. If you do not know yet, ask one quick diagnostic question, or begin from the most basic piece and let their answers tell you to speed up.
- Ask one question at a time, then stop and wait. Never stack several questions in one turn.
- End every teaching turn with either a <check> (below) or a single one-line question to the student. A turn that only answers a direct question may end with one line offering the next step instead.
- Grade honestly. Right: say so in a line, say why it is right, move on. Partly right: say exactly which part is right and which part is missing. Wrong: say plainly that it is wrong, name the specific mistake, re-teach that one piece a different way, and check it again. Never call a wrong answer "almost right" to be kind; the examiner will not.
- When the student is stuck, change the explanation, not the volume: a smaller step, a different angle, a concrete example from daily life, then the formal statement again.
- Exam craft is part of the subject. Say how a 2-mark answer differs from a 5-mark one, which definitions must be word-perfect, which diagram the examiner expects, which units to write, and where students usually lose marks.
- Quick actions ("go deeper", "give me an example", "simpler", "quiz me", "show PYQs", "summarise", "next topic") arrive as an ordinary sentence from the student. Do what it asks, in the same small-step way.
- A message in brackets saying a check was "answered on the card" means the app graded an MCQ already. Do not grade it again; build on it: if it was wrong, find out why; if it was right twice, go harder.

## Say what matters for the exam, and why

- Say explicitly what is important and why, as you teach: "This is a must for the end term: asked in 3 of the last 4 papers, usually for 5 marks." The past-paper questions (PYQs) you are given below are your evidence.
- Where there is no PYQ evidence, you may still judge importance from the syllabus, the topic pack and how such papers are usually set, but call it an estimate: "Probably important (my estimate; no past papers on this have been extracted yet)."
- Never invent a past-paper question, a year, or marks. Cite a PYQ only with the <pyq id="..."/> tag and only with an id you were given. Questions in the topic pack are practice questions written for this app, not past papers; never present them as PYQs.

## Language and format

- Write in the language the session asks for. English: clear, plain textbook English. Hinglish: romanized Hindi mixed with English, the way a good senior explains in the hostel ("dekho, yahan force do components mein toot jaata hai"), but every technical term, definition, law, formula, unit and exam-ready sentence stays in English exactly as the textbook writes it, so the student can write it in the paper. Never use Devanagari.
- All mathematics in LaTeX: $...$ inline and $$...$$ for display. Never write maths as plain text such as x^2, sqrt(2) or (a+b)/c. Chemical formulae too: $\mathrm{H_2SO_4}$.
- Markdown for phones: short paragraphs, **bold** for the one phrase that matters, numbered lists for steps, no heading bigger than ###, no wide tables.
- If you are not sure of a fact, say so. If the topic pack disagrees with the standard textbook, follow the textbook and say so in a line.
- Stay with the subject. If the student drifts, answer briefly and kindly, then bring them back.

## The three tags

The app turns three tags in your text into interface. Put each on its own line(s), exactly in this form. The JSON inside must be valid: double quotes, no trailing commas, no comments, and every backslash doubled, so LaTeX inside JSON is written "$\\frac{a}{b}$".

1. A check, which the app shows as a card:

<check id="c1" kind="mcq">
{"q": "Which organelle makes most of the cell's ATP?", "options": ["Nucleus", "Mitochondrion", "Ribosome", "Golgi body"], "answer": 1, "why": "Oxidative phosphorylation happens on the inner mitochondrial membrane; ribosomes make proteins, not ATP."}
</check>

<check id="c2" kind="short">
{"q": "In two lines, state Euler's theorem for homogeneous functions.", "marks": 2}
</check>

   - mcq: exactly four options; "answer" is the 0-based index of the right one; "why" is shown after the student picks. Make the wrong options the mistakes students actually make.
   - short: the student types an answer and it comes back to you to grade. Give "marks" as the exam would.
   - Ids are unique within the lesson: c1, c2, c3 ..., carrying on from the highest id already used in the conversation.
   - At most one check per turn, after the teaching. The card shows the question, so do not also write the question out in your prose.

2. A past-paper question, shown as a card where the tag sits:

<pyq id="UUID"/>

   Only ids from the PYQ list below. The card shows the question and its answer, so do not copy them out; talk about the question: what it is testing, how to set out the answer, where the marks are.

3. Your private notes, hidden from the student. The very last thing in the turn, at most one:

<state>{"level": 2, "importance": "high", "why": "asked in 4 of 6 end terms", "note": "confuses mitosis and meiosis phases", "profile": "prefers a worked example before the rule; weak on terminology", "next": "Cell growth and division", "done": false}</state>

   - Every field is optional; leave out what has not changed. Include a <state> whenever anything changed, which is nearly every turn.
   - level: your estimate of the student's mastery of THIS topic, 0 (never seen) to 5 (could write a full-marks answer unaided). Move it on evidence from their answers, not on how much you have explained.
   - importance: low, medium, high or must, for this topic in the exam; why: one line of evidence (PYQ count and marks when you have them, otherwise "estimate: ...").
   - note: one line about this student and this topic.
   - profile: your running picture of how this student learns, across topics, at most three sentences. It replaces the previous one, so carry forward what still holds.
   - next: the topic to study after this one, by its exact syllabus name. done: true when this topic is finished for now.
"""

DEPTH_RULES = {
    "basics": "basics: build intuition first. Go slowly, plain words before symbols, "
              "derivations only when the exam demands them.",
    "exam": "exam: what the mini, mid and end term papers ask, taught to full marks. "
            "Definitions, derivations, numericals and diagrams in the form the examiner wants.",
    "deep": "deep: beyond the exam. Derivations with the reasoning behind each step, "
            "why the result holds, where it breaks; still say what the exam needs.",
}
LANG_RULES = {
    "english": "English.",
    "hinglish": "Hinglish (romanized Hindi with English), technical terms, formulae "
                "and exam-ready sentences in English.",
}


def scheme_block(code):
    """Section 2 of the system prompt: the marks, the books, the syllabus.
    Stable per subject, so it sits before the cache breakpoint."""
    scheme = exam_scheme()
    units, estimated = syllabus(code)
    pack = load_pack(code) or {}
    out = [f"# The subject: {code} {subject_name(code)}", ""]
    theory = scheme.get("theory") or {}
    if theory:
        out.append("How a theory subject is marked at MANIT (first year):")
        for key, v in theory.items():
            if isinstance(v, dict):
                note = f" ({v['note']})" if v.get("note") else ""
                out.append(f"- {key.replace('_', ' ')}: {v.get('marks')} marks{note}")
        if scheme.get("grading"):
            out.append(f"- grading: {scheme['grading']}")
        out.append("")
    books = (scheme.get("books") or {}).get(code)
    if books:
        out.append("What the seniors recommend for this subject:")
        out += [f"- {b}" for b in books]
        out.append("")
    if pack.get("exam_notes"):
        out += ["How this paper is usually set:", str(pack["exam_notes"]), ""]
    out.append("The syllabus" + (" (estimated: the institute's syllabus file has none "
                                 "for this subject, so these units come from the topic pack)"
                                 if estimated else "") + ", unit by unit:")
    for u in units:
        out.append(f"- {u['unit']}: " + "; ".join(u["topics"]))
    return "\n".join(out)


def _pack_entry_text(entry, unit, topic):
    out = [f"## Topic pack: {topic} ({unit})"]
    if entry.get("importance"):
        out.append(f"Importance (the pack's estimate): {entry['importance']}"
                   + (f": {entry['why']}" if entry.get("why") else ""))
    if entry.get("asked_as"):
        out.append(f"How it is usually asked: {entry['asked_as']}")
    if entry.get("gist"):
        out += ["", "### Gist", str(entry["gist"])]
    for key, title in (("key_points", "Key points"), ("formulas", "Formulas"),
                       ("traps", "Traps")):
        items = entry.get(key) or []
        if isinstance(items, list) and items:
            out += ["", f"### {title}"] + [f"- {x}" for x in items]
    qs = entry.get("questions") or []
    if isinstance(qs, list) and qs:
        out += ["", "### Practice questions (written for this app; NOT past papers, never cite them as PYQs)"]
        for n, q in enumerate(qs, 1):
            if not isinstance(q, dict):
                continue
            head = f"{n}. [{q.get('kind', 'short')}" + (f", {q['marks']} marks" if q.get("marks") else "") + "]"
            out.append(f"{head} {q.get('q', '')}")
            if q.get("options"):
                out.append("   Options: " + " | ".join(map(str, q["options"]))
                           + (f" (answer index {q['answer_index']})"
                              if q.get("answer_index") is not None else ""))
            if q.get("a"):
                out.append(f"   Answer: {q['a']}")
    return "\n".join(out)


def _pyq_text(rows, scope):
    if not rows:
        return (f"## Past-paper questions on {scope}\n"
                "None have been extracted for this yet. You have no PYQ ids, so emit no "
                "<pyq> tags; any importance you state is an estimate and you say so.")
    out = [f"## Past-paper questions on {scope} (newest first; cite only these ids)"]
    used = len(out[0])
    for r in rows[:PYQ_PROMPT_MAX]:
        bits = [r["exam"] or "exam?", str(r["year"] or "year?")]
        if r["section"]:
            bits.append(f"section {r['section']}")
        if r["qno"]:
            bits.append(f"Q{r['qno']}")
        if r["marks"] is not None:
            bits.append(f"{r['marks']:g} marks")
        if r["topic"]:
            bits.append(f"{r['unit']} / {r['topic']}")
        item = f"\n- id {r['id']} | " + " | ".join(bits) + f"\n  Question: {r['question_md']}"
        if r["answer_md"]:
            item += f"\n  Answer: {r['answer_md']}"
        if used + len(item) > PYQ_PROMPT_CHARS:
            break
        out.append(item)
        used += len(item)
    return "".join(out)


def topic_block(code, unit, topic, pyqs):
    """Section 3: what to teach from. The last stable block; the breakpoint
    goes here, so a lesson's second turn reads persona, syllabus and pack
    from the cache."""
    out = []
    if topic:
        entry = pack_topic(code, unit, topic)
        out.append(_pack_entry_text(entry, unit, topic) if entry else
                   f"## Topic pack: {topic} ({unit})\nNo pack has been written for this "
                   "topic; teach it from the syllabus and the standard textbook.")
        scope = topic
    else:
        pack = load_pack(code) or {}
        lines = []
        for u in pack.get("units") or []:
            if not isinstance(u, dict) or (unit and u.get("unit") != unit):
                continue
            for t in u.get("topics") or []:
                if isinstance(t, dict) and t.get("topic"):
                    lines.append(f"- {u.get('unit')} / {t['topic']}: importance "
                                 f"{t.get('importance') or '?'} (estimate)"
                                 + (f": {t['why']}" if t.get("why") else ""))
        out.append(("## The pack's importance estimates\n" + "\n".join(lines)) if lines
                   else "## Topic pack\nNo pack covers this; teach from the syllabus.")
        scope = unit or subject_name(code)
    out.append(_pyq_text(pyqs, scope))
    return "\n\n".join(out)


TIER_RULES = {
    "standard": "standard: the everyday lesson. Keep turns short and move on evidence.",
    "advanced": "advanced: the student chose the best teacher and is paying for it. Use "
                "the room: if you do not know them yet, open with one sharp diagnostic "
                "question before teaching; anticipate the mistakes their profile and the "
                "past papers predict; prefer a worked example to a restatement; when they "
                "are wrong, find the misconception underneath, not just the slip; tie "
                "every step to how the unit's past papers ask it.",
}


def learner_block(session, profile, mastery, unit_mastery, others=()):
    """Section 4: the student as of this turn. After the breakpoint, because
    it changes every turn."""
    out = ["# This student, now",
           f"Lesson: {session['title']} (mode {session['mode']}).",
           f"Language: {LANG_RULES.get(session['lang'], session['lang'])}",
           f"Depth: {DEPTH_RULES.get(session['depth'], session['depth'])}",
           f"Tier: {TIER_RULES.get(session.get('tier'), TIER_RULES['standard'])}",
           "What you have learnt about how they learn (your own earlier <state> profile): "
           + ((profile or {}).get("notes") or "nothing yet; find out.")]
    if others:
        out.append("How they learn in other subjects, from your notes there (the habits carry "
                   "over; the knowledge does not): " + " ".join(
                       f"[{o['code']}] {o['notes']}" for o in others))
    if session["topic"]:
        m = mastery or {}
        out.append(
            f"This topic: level {m.get('level', 0)}/5, {m.get('correct', 0)} of "
            f"{m.get('attempts', 0)} card checks right"
            + (f", your flag {m['importance']}" if m.get("importance") else "")
            + (f", your note: {m['note']}" if m.get("note") else "")
            + ("" if m.get("last_seen") else ", never studied here before") + ".")
    others = [m for m in unit_mastery if m["topic"] != session["topic"]]
    if others:
        out.append("Elsewhere in this unit: " + "; ".join(
            f"{m['topic']} level {m['level']}" for m in others) + ".")
    return "\n".join(out)


def opener(session):
    """The first user turn, never stored: rebuilt from the session each call
    so the lesson always starts the same way for the model."""
    name = subject_name(session["code"])
    where = (f"{session['topic']} ({session['unit']}, {name})" if session["topic"]
             else f"the unit {session['unit']} of {name}" if session["unit"] else name)
    mode = session["mode"]
    if mode == "teach":
        text = (f"Teach me {where}. Start from what I need first, tell me how much the "
                "exam cares about it, and check I have understood each step before moving on.")
    elif mode == "quiz":
        text = (f"Quiz me on {where}. One question at a time, mixing quick MCQs with short "
                "written answers the way the exam asks; grade each one before the next.")
    elif mode == "pyq":
        text = (f"Take me through the past-paper questions on {where}, one at a time: what "
                "each is testing, how to set out a full-marks answer, and the traps. If none "
                "have been extracted, say so and use exam-style practice questions instead.")
    elif mode == "revise":
        text = (f"Revise {where} with me quickly: the must-know points first, then test me "
                "to find my weak spots, and re-teach only those.")
    else:
        text = (f"I have questions about {where}. Say hello in one line and ask me what I "
                "want to work on.")
    return text + " (Reply in " + ("Hinglish" if session["lang"] == "hinglish" else "English") + ".)"


def build_messages(session, history):
    """The conversation for the API: the opener, then every stored turn,
    tutor turns verbatim (tags and all, so the model sees what it asked).
    Same-role neighbours are joined -- a mark followed by a typed answer is
    one student turn -- and an over-long lesson loses its oldest middle."""
    out = [{"role": "user", "content": opener(session)}]
    for m in history:
        role = "assistant" if m["role"] == "tutor" else "user"
        text = m["body"]
        if not text.strip():
            continue
        if out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    while sum(len(m["content"]) for m in out) > HISTORY_CHARS and len(out) > 3:
        del out[1:3]
    return out


def build_system(session, profile, mastery, unit_mastery, pyqs, others=()):
    return [
        {"type": "text", "text": PERSONA},
        {"type": "text", "text": scheme_block(session["code"])},
        {"type": "text", "text": topic_block(session["code"], session["unit"],
                                             session["topic"], pyqs),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": learner_block(session, profile, mastery, unit_mastery, others)},
    ]


# ------------------------------------------------------------- the model

def model_stream(system_blocks, messages, model, effort):
    """Text deltas from the model, as a generator; its return value (the
    StopIteration's) is the usage. The one place the API is called.

    Fallbacks first, the way make_notes does it, and the plain stream for an
    SDK or account without the beta. Only the opening can fall back: once a
    word has reached the phone there is no second request to make."""
    import anthropic

    client = anthropic.Anthropic()
    kwargs = dict(model=model, max_tokens=MAX_TOKENS, system=system_blocks,
                  messages=messages, output_config={"effort": effort})
    try:
        manager = client.beta.messages.stream(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        stream = manager.__enter__()
    except (TypeError, anthropic.BadRequestError):
        manager = client.messages.stream(**kwargs)
        stream = manager.__enter__()
    try:
        for text in stream.text_stream:
            yield text
        final = stream.get_final_message()
    finally:
        manager.__exit__(None, None, None)
    u = final.usage
    return {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", None) or 0,
            "cache_write_tokens": getattr(u, "cache_creation_input_tokens", None) or 0,
            "stop_reason": final.stop_reason, "model": getattr(final, "model", None) or model}


def cost_of(model, usage):
    """Dollars for one call: PRICES plus the cache rates (write 1.25x input,
    read 0.1x input), per million tokens."""
    rate_in, rate_out = notes.price_of(model)
    u = usage or {}
    return (u.get("input_tokens", 0) * rate_in + u.get("output_tokens", 0) * rate_out
            + u.get("cache_write_tokens", 0) * 1.25 * rate_in
            + u.get("cache_read_tokens", 0) * 0.1 * rate_in) / 1e6


class Stream:
    """ndjson over chunked transfer, one flushed line per event.

    A phone that hangs up mid-lesson does not stop the lesson: the rest is
    still read from the model and stored, so it is there on the next open
    and the spend it caused is on the record."""

    def __init__(self, handler):
        self.h = handler
        self.gone = False

    def open(self):
        h = self.h
        h.send_response(200)
        h.send_header("Content-Type", "application/x-ndjson")
        h.send_header("Transfer-Encoding", "chunked")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("X-Accel-Buffering", "no")
        h.end_headers()
        h.close_connection = True

    def _write(self, data):
        if self.gone:
            return
        try:
            self.h.wfile.write(data)
            self.h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.gone = True

    def send(self, obj):
        line = (json.dumps(obj) + "\n").encode()
        self._write(b"%x\r\n%s\r\n" % (len(line), line))

    def close(self):
        self._write(b"0\r\n\r\n")


_busy = set()
_busy_lock = threading.Lock()


def _claim(session_id):
    with _busy_lock:
        if session_id in _busy:
            raise Busy("the tutor is still answering this lesson")
        _busy.add(session_id)


def _release(session_id):
    with _busy_lock:
        _busy.discard(session_id)


def _context(conn, session):
    """Everything the system prompt needs about this session's learner."""
    owner = session["owner_id"]
    profile = db_profile(conn, owner, session["code"])
    mastery = (db_topic_mastery(conn, owner, session["code"], session["unit"], session["topic"])
               if session["topic"] else None)
    unit_mastery = (list(db_mastery(conn, owner, session["code"]).values())
                    if session["unit"] else [])
    unit_mastery = [m for m in unit_mastery if m["unit"] == session["unit"]]
    advanced = session.get("tier") == "advanced"
    # Advanced reads the whole unit's past papers, not only this topic's: the
    # question that spans two topics is the one students are not ready for.
    pyqs = db_pyqs(conn, session["code"], session["unit"],
                   None if advanced else session["topic"], limit=PYQ_PROMPT_MAX)
    others = db_profile_notes(conn, owner, session["code"]) if advanced else ()
    return build_system(session, profile, mastery, unit_mastery, pyqs, others)


def _tutor_kind(session, action, parsed):
    if action == "summary":
        return "summary"
    if (session["mode"] == "pyq" or action == "pyq") and parsed["pyqs"]:
        return "pyq"
    return "lesson"


def run_turn(handler, me, session, system, messages, action=None, first_line=None):
    """Stream one tutor turn and store it. The response is already ours from
    here on: every failure becomes an error line, never a second status."""
    tier = session.get("tier") or "standard"
    model = session.get("model") or model_name(tier)
    out = Stream(handler)
    out.open()
    try:
        if first_line:
            out.send(first_line)
        pieces = []
        gen = model_stream(system, messages, model, effort_level(tier))
        while True:
            try:
                piece = next(gen)
            except StopIteration as stop:
                usage = stop.value or {}
                break
            if piece:
                pieces.append(piece)
                out.send({"t": "delta", "text": piece})
        text = "".join(pieces)
        priced = usage.get("model") or model
        cost = cost_of(priced, usage)
        if usage.get("stop_reason") == "refusal" or not text.strip():
            # Nothing to keep, but the call was paid for, and the bill is
            # the one number this screen promises to get right.
            with notes.db(me["id"]) as conn:
                db_bump_session(conn, session["id"], cost, turn=False)
            out.send({"t": "error", "error": (
                "The tutor declined to answer that. Try rephrasing, or ask about "
                "another part of the topic." if usage.get("stop_reason") == "refusal"
                else "The tutor had nothing to say. Try again.")})
            return
        parsed = parse_turn(text)
        with notes.db(me["id"]) as conn:
            with conn.transaction():
                if parsed["pyqs"]:
                    # "Never invent a PYQ" is the prompt's rule; this is the
                    # server's: an id that is not in the table is not cited.
                    real = {r[0] for r in conn.execute(
                        "select id::text from pyq_questions where id = any(%s::uuid[])",
                        (parsed["pyqs"],)).fetchall()}
                    parsed["pyqs"] = [p for p in parsed["pyqs"] if p in real]
                meta = dict(parsed)
                if usage.get("stop_reason") == "max_tokens":
                    meta["truncated"] = True
                if priced != model:
                    meta["model"] = priced
                if action:
                    meta["action"] = action
                msg = db_add_message(conn, session["id"], "tutor",
                                     _tutor_kind(session, action, parsed),
                                     text[:60000], meta, usage, cost)
                db_bump_session(conn, session["id"], cost)
                state = parsed["state"] or {}
                mastery = []
                if session["topic"]:
                    mastery.append(db_apply_state(conn, session["owner_id"], session["code"],
                                                  session["unit"], session["topic"], state))
                if state.get("profile"):
                    db_set_profile(conn, session["owner_id"], session["code"],
                                   notes_text=state["profile"])
            row = db_session(conn, session["id"], me["id"])
            spend = db_spend(conn, session["owner_id"])
        out.send({"t": "done", "message": msg, "mastery": mastery, "session": row,
                  "spend": spend})
    except Exception as e:  # noqa: BLE001 -- the status line is long gone
        notes.log(f"{session['id']}: {type(e).__name__}: {e}", "tutor")
        out.send({"t": "error", "error": _readable(e)})
    finally:
        out.close()


def _readable(e):
    name = type(e).__name__
    if name == "AuthenticationError":
        return "The tutor's API key was refused. The server needs ANTHROPIC_API_KEY."
    if name == "RateLimitError":
        return "The tutor is rate-limited right now. Wait a minute and try again."
    if name in ("APIConnectionError", "APITimeoutError"):
        return "The tutor could not reach the model. Check the connection and try again."
    return f"The tutor stopped: {name}: {e}"


# --------------------------------------------------------------- routes

def _text(body, key, cap=200):
    v = body.get(key)
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        raise ValueError(f"{key} must be text")
    return v.strip()[:cap] or None


def _code(value):
    code = (value or "").strip().upper()
    if code not in tutor_codes():
        raise NotFound(f"the tutor has no syllabus for {code or 'that subject'}")
    return code


def _own_session(conn, me, session_id):
    session = db_session(conn, _uuid(session_id, "session_id"), me["id"])
    if not session["mine"]:
        raise Refused("this lesson is somebody else's; you can read it but not write in it")
    return session


def get_home(h, me, write, q):
    with notes.db(me["id"]) as conn:
        learner = db_learner(conn, me, write, q.get("who"))
        views = subject_views(conn, learner["id"])
        return {"owner": write, "model": model_name(), "tiers": tiers(),
                "role_needed": notes.TUTOR_ROLE,
                "learner": learner, "subjects": views,
                "recent": db_sessions(conn, me["id"], limit=20),
                "plan": plan(views), "spend": db_spend(conn, learner["id"])}


def get_subject(h, me, write, q):
    code = _code(q.get("code"))
    with notes.db(me["id"]) as conn:
        learner = db_learner(conn, me, write, q.get("who"))
        view = subject_views(conn, learner["id"], [code])[0]
        stats = db_pyq_stats(conn, [code]).get(code, {})
        by_unit = {}
        for (unit, _), st in stats.items():
            by_unit[unit or ""] = by_unit.get(unit or "", 0) + st["count"]
        years = [r[0] for r in conn.execute(
            "select distinct year from pyq_questions where subject_code = %s and"
            " year is not null order by year desc", (code,)).fetchall()]
        view.update(
            sessions=db_sessions(conn, me["id"], code=code, limit=50),
            pyq_summary={"count": sum(by_unit.values()), "by_unit": by_unit, "years": years},
            plan=plan([view]), learner=learner, owner=write,
            profile=db_profile(conn, learner["id"], code))
        return view


def get_session(h, me, write, q):
    with notes.db(me["id"]) as conn:
        session = db_session(conn, _uuid(q.get("id")), me["id"])
        messages = db_messages(conn, session["id"])
        cited = []
        for m in messages:
            for p in (m["meta"] or {}).get("pyqs") or []:
                if p not in cited:
                    cited.append(p)
        pyqs = {r["id"]: r for r in db_pyqs(conn, None, ids=cited)} if cited else {}
        mastery = (db_topic_mastery(conn, session["owner_id"], session["code"],
                                    session["unit"], session["topic"])
                   if session["topic"] else None)
        return {"session": session, "messages": messages, "pyqs": pyqs,
                "mastery": mastery, "owner": write and session["mine"]}


def get_pyq(h, me, write, q):
    code = _code(q.get("code"))
    year = q.get("year")
    if year:
        try:
            year = int(year)
        except ValueError:
            raise ValueError("year is a number")
    exam = q.get("exam") or None
    if exam and exam not in EXAM_ORDER:
        raise ValueError("exam is mini, mid or end")
    with notes.db(me["id"]) as conn:
        questions = db_pyqs(conn, code, q.get("unit") or None, q.get("topic") or None,
                            year or None, exam)
        counts = {}
        for (unit, topic), st in db_pyq_stats(conn, [code]).get(code, {}).items():
            counts.setdefault(unit or "", {})
            counts[unit or ""][topic or ""] = counts[unit or ""].get(topic or "", 0) + st["count"]
        return {"questions": questions, "counts": counts}


def post_start(h, me, body):
    code = _code(body.get("code"))
    mode = body.get("mode") or "teach"
    if mode not in MODES:
        raise ValueError(f"mode is one of {', '.join(MODES)}")
    unit, topic = place(code, _text(body, "unit"), _text(body, "topic"))
    lang, depth = body.get("lang") or None, body.get("depth") or None
    tier = body.get("tier") or None
    if lang is not None and lang not in LANGS:
        raise ValueError(f"lang is one of {', '.join(LANGS)}")
    if depth is not None and depth not in DEPTHS:
        raise ValueError(f"depth is one of {', '.join(DEPTHS)}")
    if tier is not None and tier not in TIERS:
        raise ValueError(f"tier is one of {', '.join(TIERS)}")
    title = f"{MODE_LABEL[mode]} · {topic or unit or subject_name(code)}"
    with notes.db(me["id"]) as conn:
        profile = db_profile(conn, me["id"], code)
        tier = tier or profile["tier"]
        sid = db_create_session(conn, me["id"], code, unit, topic, mode,
                                lang or profile["lang"], depth or profile["depth"],
                                tier, title, model_name(tier))
        session = db_session(conn, sid, me["id"])
        system = _context(conn, session)
    _claim(sid)
    try:
        run_turn(h, me, session, system, build_messages(session, []),
                 first_line={"t": "session", "session": session})
    finally:
        _release(sid)


def post_say(h, me, body):
    action = body.get("action") or None
    kind = body.get("kind") or ("action" if action else "say")
    text = body.get("body") or ""
    if not isinstance(text, str):
        raise ValueError("body must be text")
    text = text.strip()
    if action:
        if action not in ACTIONS:
            raise ValueError(f"action is one of {', '.join(ACTIONS)}")
        kind = "action"
        text = ACTIONS[action] + (f"\n\n{text}" if text else "")
    if kind not in ("say", "answer", "action"):
        raise ValueError("kind is say, answer or action")
    if not text:
        raise ValueError("say something")
    if len(text) > SAY_MAX:
        raise ValueError(f"keep it under {SAY_MAX} characters")
    with notes.db(me["id"]) as conn:
        session = _own_session(conn, me, body.get("session_id"))
        _claim(session["id"])
        try:
            db_add_message(conn, session["id"], "student", kind, text,
                           {"action": action} if action else {})
            system = _context(conn, session)
            messages = build_messages(session, db_messages(conn, session["id"]))
        except BaseException:
            _release(session["id"])
            raise
    try:
        run_turn(h, me, session, system, messages, action=action)
    finally:
        _release(session["id"])


def post_mark(h, me, body):
    picked = body.get("picked")
    if isinstance(picked, bool) or not isinstance(picked, (int, str, type(None))):
        raise ValueError("picked is an option index or its text")
    if not isinstance(body.get("correct"), bool):
        raise ValueError("correct is true or false")
    with notes.db(me["id"]) as conn:
        with conn.transaction():
            session = _own_session(conn, me, body.get("session_id"))
            mastery, msg = db_mark(conn, me["id"], session, body.get("check_id"),
                                   picked, body["correct"])
    return {"mastery": mastery, "message": msg}


def post_mastery(h, me, body):
    code = _code(body.get("code"))
    unit, topic = place(code, _text(body, "unit"), _text(body, "topic"))
    if not topic:
        raise ValueError("name the topic")
    with notes.db(me["id"]) as conn:
        return {"mastery": db_set_level(conn, me["id"], code, unit, topic, body.get("level"))}


def post_settings(h, me, body):
    code = _code(body.get("code"))
    with notes.db(me["id"]) as conn:
        return {"profile": db_set_profile(conn, me["id"], code, body.get("lang") or None,
                                          body.get("depth") or None, body.get("tier") or None)}


def post_close(h, me, body):
    with notes.db(me["id"]) as conn:
        session = _own_session(conn, me, body.get("id"))
        conn.execute("update tutor_sessions set status = 'done', updated_at = now()"
                     " where id = %s", (session["id"],))
    return {"ok": True}


def post_delete(h, me, body):
    with notes.db(me["id"]) as conn:
        session = _own_session(conn, me, body.get("id"))
        conn.execute("delete from tutor_sessions where id = %s", (session["id"],))
    return {"ok": True}


def post_import(h, me, body):
    items = body.get("items")
    if not isinstance(items, list):
        raise ValueError("items is a list")
    try:
        import tutor_pyq
    except ImportError as e:
        return h.reply(500, {"error": f"the PYQ importer is not installed here: {e}"})
    with notes.db(me["id"]) as conn:
        got = tutor_pyq.import_items(conn, items, extracted_by=f"import by {me.get('name')}")
    return {k: got.get(k) for k in ("inserted", "skipped", "mapped", "errors") if k in got}


GETS = {"/tutor": get_home, "/tutor/subject": get_subject,
        "/tutor/session": get_session, "/tutor/pyq": get_pyq}
POSTS = {"/tutor/start": post_start, "/tutor/say": post_say, "/tutor/mark": post_mark,
         "/tutor/mastery": post_mastery, "/tutor/settings": post_settings,
         "/tutor/session/close": post_close, "/tutor/session/delete": post_delete,
         "/tutor/pyq/import": post_import}


def handle(handler, args):
    """Every /tutor request. The role gate in notes.py has already run; this
    asks once more for writes, because that gate keys on the path alone and a
    POST to a read path would otherwise reach here as a trusted reader."""
    path, _, query = handler.path.partition("?")
    path = path.rstrip("/") or "/"
    me = handler.me
    write = may_write(me)
    if handler.command == "GET":
        fn = GETS.get(path)
    elif handler.command == "POST":
        fn = POSTS.get(path)
    else:
        fn = None
    if not fn:
        return handler.reply(404, {"error": f"no tutor page at {path}"})
    try:
        if handler.command == "GET":
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
            result = fn(handler, me, write, q)
        else:
            if not write:
                return handler.reply(403, {"error": f"the tutor teaches {notes.TUTOR_ROLE}s; "
                                                    "you can read its lessons",
                                           "required": notes.TUTOR_ROLE})
            body = handler.body(IMPORT_CAP if path == "/tutor/pyq/import" else BODY_CAP)
            if body is None:
                return None   # 413 already sent
            if not isinstance(body, dict):
                raise ValueError("send a JSON object")
            result = fn(handler, me, body)
    except (Refused, psycopg.errors.InsufficientPrivilege) as e:
        return handler.reply(403, {"error": str(e) if isinstance(e, Refused)
                                   else "that is not yours to change"})
    except NotFound as e:
        return handler.reply(404, {"error": str(e)})
    except Busy as e:
        return handler.reply(409, {"error": str(e)})
    except (ValueError, psycopg.errors.IntegrityError, psycopg.errors.DataError) as e:
        return handler.reply(400, {"error": str(e).split("\n")[0]})
    except psycopg.OperationalError as e:
        notes.log(f"{path}: {e}", "tutor")
        return handler.reply(503, {"error": "the library is offline"})
    except Exception as e:  # noqa: BLE001
        notes.log(f"{path}: {type(e).__name__}: {e}", "tutor")
        return handler.reply(500, {"error": f"{type(e).__name__}: {e}"})
    if result is not None:
        handler.reply(200, result)
    return None
