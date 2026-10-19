"""Past papers as questions: the PYQ pipeline behind the tutor.

    python notes.py tutor pyq extract [--code X] [--model M] [--limit N] [--max-cost USD]
    python notes.py tutor pyq answer  [--code X] [--model M] [--limit N] [--max-cost USD]
    python notes.py tutor pyq import FILE.json [--by NAME]
    python notes.py tutor pyq status

archive_documents knows that a PDF is the 2024 End Term for Chemistry. It
does not know what is in it, and "what does the exam ask" is the question
the tutor exists to answer. This file reads the papers once, with the API,
and leaves every question in pyq_questions as text the tutor can cite.

Two routes fill the table and both end in import_items(): `extract` here,
which sends each PDF to the model, and the pyq-extract skill, where a Claude
Code agent on the server reads the PDFs itself and writes a JSON file for
`import`. The fingerprint makes every route re-runnable -- a question read
twice, by either, lands once -- so a run that dies halfway is resumed by
running it again.

An operator's tool, run on the machine that has library/archive/. It talks
to the database as the owner (notes.db(), no RLS), like import_archive.py.
"""

import argparse
import base64
import difflib
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from pathlib import Path

import notes

ROOT = Path(__file__).resolve().parent
PACKS = ROOT / "tutor" / "packs"
DEFAULT_MODEL = "claude-opus-5-5"
EXAMS = ("mini", "mid", "end")
BETAS = ["server-side-fallback-2026-07-01"]
MAX_TOKENS = 16000

# Raw bytes before base64. Past papers are a few pages; anything near this is
# a scan of a whole booklet and better read by the skill a page at a time.
# 23 MB of PDF is 31 MB of base64, and the request cap is 32 MB with the
# prompt still to fit; a real paper is a few hundred KB.
MAX_PAPER_BYTES = 23 * 1024 * 1024
# The API's own ceiling for one image, which is stricter than for a PDF.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}

ANSWER_BATCH = 8
DEFAULT_MAX_COST = 20.0


# ---------------------------------------------------------------------------
# Identity and placement.

def _norm_question(text):
    # NFKC first so a ligature or a full-width digit from a PDF's text layer
    # reads the same as the hand-typed version of the same question.
    return " ".join(unicodedata.normalize("NFKC", text or "").lower().split())


def fingerprint(code, question_md, sitting=None):
    """sha256(code + sitting + normalised question)[:32]: what makes import
    re-runnable.

    Case and whitespace only. A question retyped with a different line break
    is the same question; one with a different number in it is not, and
    anything cleverer than this starts merging questions that differ in the
    one figure a numerical is about.

    The sitting is part of it on purpose. The same five-marker set in 2022,
    2023 and 2024 is three rows, not one, because "asked in three of the
    last four end terms" is exactly the fact the tutor exists to tell the
    student -- a question stored once under its first paper would hide it.
    The sitting is the archive document when there is one, else exam, year
    and section; with none of those, two readings of the same question
    still land once.
    """
    raw = (f"{(code or '').strip().upper()}\n{(sitting or '').strip()}\n"
           f"{_norm_question(question_md)}")
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def sitting_key(doc_id, exam, year, section):
    """What distinguishes one sitting of a paper from another, for the
    fingerprint: the archive row if the question came from one, else the
    exam, year and section the item itself named."""
    if doc_id:
        return f"doc:{doc_id}"
    if exam or year or section:
        return f"{exam or ''}|{year or ''}|{section or ''}"
    return ""


_STOP = {"a", "an", "and", "the", "of", "in", "on", "for", "to", "with", "by",
         "its", "their", "is", "are", "vs", "or", "from", "into", "at", "as"}


def _norm(s):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def _tokens(s):
    out = set()
    for w in _norm(s).split():
        if w in _STOP:
            continue
        # "Lubricants" and "lubricant" are one word to a student.
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def _close(a, b):
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _score(a, b):
    """Token overlap, words compared with difflib so "lubrication" meets
    "lubricant"; and the whole string's difflib ratio, but only as a typo
    catch. Whole-string ratio alone would file "Rolle's theorem" under
    "Stokes' theorem" -- 0.79 alike and nothing in common but "theorem".
    """
    ta, tb = _tokens(a), _tokens(b)
    dice = 0.0
    if ta and tb:
        hit = sum(1 for x in ta if any(_close(x, y) for y in tb))
        hit_b = sum(1 for y in tb if any(_close(x, y) for x in ta))
        dice = (hit + hit_b) / (len(ta) + len(tb))
        # Every word of a short label found in a long syllabus line:
        # "Mechanism of lubrication" is the line that goes on to list the
        # three mechanisms. Two words at least, so "Fuels" cannot do it.
        if len(ta) >= 2 and hit == len(ta):
            dice = max(dice, 0.8)
    ratio = difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()
    return max(dice, ratio if ratio >= 0.85 else 0.0)


def _pick(text, names, threshold=0.6, exact=False):
    """The one name in `names` that `text` means, or None."""
    want = _norm(text)
    if not want or not names:
        return None
    for n in names:
        if _norm(n) == want:
            return n
    if exact:
        return None
    scored = sorted(((_score(text, n), i) for i, n in enumerate(names)), reverse=True)
    top, i = scored[0]
    if top >= threshold:
        # Two names equally good is not an answer: "analysis of coal" is
        # proximate and ultimate both, and guessing one misfiles half.
        if len(scored) > 1 and scored[1][0] == top:
            return None
        return names[i]
    # A short label for a long syllabus line ("Zeolite" for "Zeolite
    # process"), or a long one wrapped round it -- but only when exactly one
    # name fits, so "coal" never picks one of Fuels' four coal topics.
    if len(want) >= 4:
        hits = [n for n in names
                if f" {want} " in f" {_norm(n)} " or
                (len(_norm(n)) >= 4 and f" {_norm(n)} " in f" {want} ")]
        if len(hits) == 1:
            return hits[0]
    return None


def map_topic(code, unit, topic):
    """Free-text unit/topic onto syllabus.json's exact names: (unit, topic).

    Either side may come back None. The topic is looked for inside the named
    unit first and then anywhere in the subject; when it is found elsewhere,
    the topic's own unit wins, because the pair has to be one the syllabus
    actually contains or the screen files the question under nothing.
    """
    units = notes.SYLLABUS.get((code or "").strip().upper()) or []
    if not units:
        return None, None
    unit_names = [u["unit"] for u in units]
    topics_of = {u["unit"]: list(u.get("topics") or []) for u in units}

    u = _pick(unit, unit_names) if unit else None
    t = None
    all_topics = [tp for un in unit_names for tp in topics_of[un]]
    if topic and not _pick(topic, all_topics, exact=True):
        # "Fuels" written in the topic box is the unit, not the one topic
        # line that happens to end in the word "fuels".
        as_unit = _pick(topic, unit_names, exact=True)
        if as_unit:
            return as_unit, None
    if topic:
        if u:
            t = _pick(topic, topics_of[u])
        if t is None:
            pairs = [(un, tp) for un in unit_names for tp in topics_of[un]]
            hit = _pick(topic, all_topics)
            if hit is not None:
                t = hit
                u = next(un for un, tp in pairs if tp == hit)
        if t is None and u is None:
            # "Fuels" written in the topic box is still a unit.
            u = _pick(topic, unit_names)
    return u, t


# ---------------------------------------------------------------------------
# Import: the one door every route goes through.

def _text(v, what, limit=None):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        raise ValueError(f"{what} must be text")
    v = v.strip()
    if not v:
        return None
    if limit and len(v) > limit:
        raise ValueError(f"{what} is {len(v)} characters (limit {limit})")
    return v


def _validate(item):
    if not isinstance(item, dict):
        raise ValueError("not an object")
    code = item.get("code")
    if not isinstance(code, str) or code.strip().upper() not in notes.SUBJECTS:
        raise ValueError(f"unknown subject {code!r}")
    code = code.strip().upper()

    question = _text(item.get("question_md"), "question_md", 20000)
    if not question:
        raise ValueError("question_md is empty")
    answer = _text(item.get("answer_md"), "answer_md", 40000)

    exam = item.get("exam")
    if exam is not None:
        exam = str(exam).strip().lower() or None
        if exam is not None and exam not in EXAMS:
            raise ValueError(f"exam {item.get('exam')!r} is not one of {', '.join(EXAMS)}")

    year = item.get("year")
    if isinstance(year, str) and year.strip().isdigit():
        year = int(year.strip())
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise ValueError(f"year {year!r} must be a whole number")
    if year is not None and not 2000 <= year <= 2100:
        raise ValueError(f"year {year} is outside 2000-2100")

    marks = item.get("marks")
    if marks is not None and (isinstance(marks, bool) or not isinstance(marks, (int, float))):
        raise ValueError(f"marks {marks!r} must be a number")
    if marks is not None and not 0 <= marks < 10000:
        raise ValueError(f"marks {marks} is out of range")

    section = _text(item.get("section"), "section")
    if section is not None:
        section = section.upper()
        if not re.fullmatch(r"[A-J]", section):
            raise ValueError(f"section {item.get('section')!r} is not one letter A-J")

    doc_id = item.get("document_id")
    if doc_id is not None:
        try:
            doc_id = str(uuid.UUID(str(doc_id)))
        except ValueError:
            raise ValueError(f"document_id {doc_id!r} is not a uuid") from None

    return {
        "code": code, "question_md": question, "answer_md": answer,
        "exam": exam, "year": year, "marks": marks, "section": section,
        "qno": _text(item.get("qno"), "qno", 40),
        "unit": _text(item.get("unit"), "unit", 300),
        "topic": _text(item.get("topic"), "topic", 300),
        "document_id": doc_id,
        "path": _text(item.get("path"), "path", 1000),
        "document_title": _text(item.get("document_title"), "document_title", 500),
        "extracted_by": _text(item.get("extracted_by"), "extracted_by", 200),
        "answer_model": _text(item.get("answer_model"), "answer_model", 200),
    }


def _document(conn, it):
    """(id, exam, year, section) of the archive row an item names, or None.

    A path or title that names nothing is refused rather than imported
    unlinked: the fingerprint would then keep the unlinked copy forever, and
    fixing the path in the file would change nothing on the next run.
    """
    cols = "select id, exam, year, set_for_section from archive_documents "
    if it["document_id"]:
        row = conn.execute(cols + "where id = %s and subject_code = %s",
                           (it["document_id"], it["code"])).fetchone()
        if not row:
            raise ValueError(f"no archive document {it['document_id']} for {it['code']}")
        return row
    if it["path"]:
        p = it["path"].lstrip("/")
        keys = [p, "library/" + p, p.removeprefix("library/")]
        row = conn.execute(cols + "where subject_code = %s and file_key = any(%s) limit 1",
                           (it["code"], keys)).fetchone()
        if not row:
            raise ValueError(f"no archive document for {it['code']} at {it['path']!r}")
        return row
    if it["document_title"]:
        rows = conn.execute(cols + "where subject_code = %s and lower(title) = lower(%s)",
                            (it["code"], it["document_title"])).fetchall()
        if len(rows) != 1:
            raise ValueError(f"document_title {it['document_title']!r} matches "
                             f"{len(rows)} {it['code']} documents; give its path instead")
        return rows[0]
    return None


def _savepoints(conn):
    # psycopg's transaction() on an idle non-autocommit connection COMMITS on
    # exit. A caller who handed us a connection mid-transaction owns that
    # transaction, so open it first and every item below is a savepoint.
    if not conn.autocommit and conn.info.transaction_status == 0:
        conn.execute("select 1")


def _import(conn, items, extracted_by=None):
    """import_items plus how many existing rows had their answer filled."""
    import psycopg

    _savepoints(conn)
    out = {"inserted": 0, "skipped": 0, "mapped": 0, "errors": [], "answered": 0}
    if not isinstance(items, list):
        out["errors"].append("items must be a list")
        return out
    for n, raw in enumerate(items, 1):
        try:
            it = _validate(raw)
            unit, topic = map_topic(it["code"], it["unit"], it["topic"])
            with conn.transaction():
                doc = _document(conn, it)
                exam, year, section = it["exam"], it["year"], it["section"]
                doc_id = None
                if doc:
                    # The archive row is filed already; an item that does not
                    # say which sitting it was inherits the paper's.
                    doc_id, d_exam, d_year, d_section = doc
                    exam = exam or d_exam
                    year = year or d_year
                    section = section or d_section
                by = it["extracted_by"] or extracted_by
                answer_by = (it["answer_model"] or by) if it["answer_md"] else None
                fp = fingerprint(it["code"], it["question_md"],
                                 sitting_key(doc_id, exam, year, section))
                row = conn.execute(
                    "insert into pyq_questions (subject_code, document_id, exam, year, "
                    "  set_for_section, qno, marks, unit, topic, question_md, answer_md, "
                    "  answer_model, extracted_by, fingerprint) "
                    "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "on conflict (fingerprint) do nothing returning id",
                    (it["code"], doc_id, exam, year, section, it["qno"], it["marks"],
                     unit, topic, it["question_md"], it["answer_md"], answer_by, by, fp),
                ).fetchone()
                if row:
                    out["inserted"] += 1
                    out["mapped"] += topic is not None
                    continue
                out["skipped"] += 1
                # Somebody wrote the answer after the question was first read:
                # keep it, but never over an answer that is already there.
                if it["answer_md"]:
                    cur = conn.execute(
                        "update pyq_questions set answer_md = %s, answer_model = %s "
                        " where fingerprint = %s and answer_md is null",
                        (it["answer_md"], answer_by, fp))
                    out["answered"] += cur.rowcount
        except (ValueError, psycopg.Error) as e:
            msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            qno = raw.get("qno") if isinstance(raw, dict) else None
            out["errors"].append(f"item {n}{f' ({qno})' if qno else ''}: {msg}")
    return out


def import_items(conn, items, extracted_by=None):
    """Validate, place and insert ImportItems. Idempotent.

    -> {"inserted", "skipped" (already there), "mapped" (inserted rows placed
    on a syllabus topic), "errors" (one line per refused item)}. A refused
    item refuses only itself.
    """
    out = _import(conn, items, extracted_by)
    out.pop("answered")
    return out


# ---------------------------------------------------------------------------
# The model.

def _client(client):
    if client is not None:
        return client
    import anthropic
    return anthropic.Anthropic()


def _create(client, **kwargs):
    """make_notes' call: the fallback beta where the account has it."""
    try:
        import anthropic
        refused = (TypeError, anthropic.BadRequestError)
    except ImportError:
        refused = (TypeError,)
    try:
        return client.beta.messages.create(betas=BETAS, fallbacks="default", **kwargs)
    except refused:
        return client.messages.create(**kwargs)  # account lacks the fallback beta


def _cost(model, usage):
    pin, pout = notes.price_of(model)
    g = lambda k: getattr(usage, k, 0) or 0  # noqa: E731
    return (g("input_tokens") * pin + g("output_tokens") * pout
            + g("cache_creation_input_tokens") * pin * 1.25
            + g("cache_read_input_tokens") * pin * 0.1) / 1e6


# Escapes JSON accepts that a LaTeX command also starts with. \frac parsed as
# JSON is a form feed and "rac", silently; these are the n-words that are
# maths rather than a newline followed by a word.
_N_COMMANDS = {"nabla", "nu", "ne", "neq", "not", "ni", "neg", "newline", "nonumber",
               "nolimits", "nmid", "ncong", "nleq", "ngeq", "nless", "ngtr", "nsim",
               "napprox", "nexists", "natural", "nearrow", "nwarrow", "notin",
               "nsubseteq", "nparallel", "nvdash", "normalsize"}


def _repair_escapes(s):
    """Double the backslash of every LaTeX command a model left single in JSON.

    Models mostly write \\\\frac in JSON as they should. When one does not,
    \\alpha is a parse error and \\frac, \\theta, \\beta, \\right are worse: they
    parse, as control characters. b, f, r and t followed by more letters are
    never a control character in this content, so they are LaTeX; \\n is LaTeX
    only for the commands above, and a newline otherwise.
    """
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] != "\\":
            out.append(s[i])
            i += 1
            continue
        j = i
        while j < n and s[j] == "\\":
            j += 1
        run = s[i:j]
        if (j - i) % 2 == 1 and j < n:
            c = s[j]
            m = re.match(r"[A-Za-z]+", s[j:])
            word = m.group(0) if m else ""
            if c in "\"/":
                double = False
            elif c == "u":
                double = not re.match(r"u[0-9a-fA-F]{4}", s[j:j + 5])
            elif c == "n":
                double = word in _N_COMMANDS
            elif c in "bfrt":
                double = len(word) > 1
            else:
                double = True
            if double:
                run += "\\"
        out.append(run)
        i = j
    return "".join(out)


def parse_json_list(text):
    """A list of objects out of a model's reply, as forgivingly as is honest.

    Fences stripped, LaTeX escapes repaired, a {"questions": [...]} wrapper
    unwrapped, and a reply cut off at max_tokens salvaged up to its last
    complete object rather than thrown away.
    """
    t = (text or "").strip()
    # Only the outer fence: an answer about C carries ```c blocks of its own
    # inside the strings, and a non-greedy match would stop at the first.
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = re.sub(r"\s*```\s*$", "", t)
    t = _repair_escapes(t)

    def unwrap(v):
        if isinstance(v, dict):
            for k in ("questions", "items", "answers"):
                if isinstance(v.get(k), list):
                    return unwrap(v[k])
            return [v]
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        return []

    start = t.find("[")
    obj = t.find("{")
    if start == -1 or (obj != -1 and obj < start):
        try:
            return unwrap(json.loads(t[obj:t.rfind("}") + 1], strict=False)) if obj != -1 else []
        except ValueError:
            if start == -1:
                return []
    try:
        return unwrap(json.loads(t[start:t.rfind("]") + 1], strict=False))
    except ValueError:
        pass
    # Truncated: close the array after each complete-looking object, last first.
    ends = [m.start() for m in re.finditer(r"\}", t[start:])][::-1][:400]
    for e in ends:
        try:
            return unwrap(json.loads(t[start:start + e + 1] + "]", strict=False))
        except ValueError:
            continue
    return []


def _text_of(msg):
    return "".join(getattr(b, "text", "") for b in msg.content
                   if getattr(b, "type", None) == "text")


def subject_name(code):
    return notes.SUBJECTS[code][0].replace("-", " ")


def syllabus_text(code):
    units = notes.SYLLABUS.get(code) or []
    if not units:
        return "(no syllabus on file for this subject: leave unit and topic null)"
    lines = []
    for u in units:
        lines.append(f"Unit: {u['unit']}")
        lines.extend(f"  - {t}" for t in u.get("topics") or [])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Extract.

EXTRACT_PROMPT = """You transcribe a past exam paper into data. The paper is a first-year B.Tech paper from MANIT Bhopal: {code} {name}.

Return ONLY a JSON array, no prose before or after, no code fences:
[{{"qno": "1(a)", "marks": 5, "question_md": "...", "unit": "...", "topic": "..."}}]

Rules:
- One object per thing a candidate answers. Every sub-question (1(a), 1(b), 1(b)(i)) is its own object; a question with no parts is one object.
- OR / internal choice: include every alternative, each its own object, with qno like "2(a)" and "2(a) OR".
- question_md: the question exactly as printed, in Markdown. All maths in LaTeX, $...$ inline and $$...$$ for display; chemical formulas as LaTeX too ($H_2SO_4$). Tables as Markdown tables. A figure you cannot reproduce: one bracketed line, [Figure: what it shows, with its labels and values]. Anything you cannot read: [illegible]. Do not solve, hint or correct.
- When a part depends on a shared stem or data ("Given the following analysis of coal ..."), repeat what it needs inside that object so it reads on its own.
- This is JSON: every backslash in LaTeX is doubled (\\\\frac, \\\\alpha).
- marks: the marks printed for that part, as a number; null when none is printed. Use a stated uniform scheme ("each part carries 2 marks") but never guess.
- unit and topic: copy EXACTLY one unit name and one of that unit's topic names from the syllabus below. If nothing fits, null (a unit alone is fine).
- Leave out the header, the instructions and the roll-number boxes.
- If this is not a question paper or holds no questions, return [].

Syllabus of {code}:
{syllabus}
"""


def paper_file(library, file_key):
    """Where a file_key's bytes are. file_key is relative to the repo root
    (library/archive/...), which is the library's parent."""
    lib = Path(library).resolve()
    first = lib.parent / file_key
    if first.is_file() or not file_key.startswith("library/"):
        return first
    # A library that is not called "library" still holds archive/ itself.
    return lib / file_key.removeprefix("library/")


def _paper_block(path):
    data = path.read_bytes()
    kind = path.suffix.lower()
    if kind == ".pdf":
        src = {"type": "base64", "media_type": "application/pdf",
               "data": base64.standard_b64encode(data).decode()}
        return {"type": "document", "source": src, "title": path.name}
    src = {"type": "base64", "media_type": IMAGE_TYPES[kind],
           "data": base64.standard_b64encode(data).decode()}
    return {"type": "image", "source": src}


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(conn, library, code=None, model=DEFAULT_MODEL, limit=None, client=None,
            log=print, max_cost=None):
    """Read every archived paper that has no questions yet; import its questions.

    Resumable by construction: a paper is done when a question points at it,
    so running again picks up where the last run stopped. -> a summary dict.
    """
    _savepoints(conn)
    rows = conn.execute(
        "select d.id, d.subject_code, d.exam, d.year, d.set_for_section, d.title, d.file_key "
        "  from archive_documents d "
        " where d.kind = 'paper' and (%s::text is null or d.subject_code = %s) "
        "   and not exists (select 1 from pyq_questions q where q.document_id = d.id) "
        " order by d.subject_code, d.year desc nulls last, d.title",
        (code, code)).fetchall()

    # The portal shelves the same End Term under two folders, so the archive
    # holds two rows for one file. Its questions land once, on the first row,
    # and the second would otherwise look unread -- and be paid for -- on
    # every run. Same bytes, same subject, already read: skip it.
    read = {}
    for (sc, key) in conn.execute(
            "select distinct d.subject_code, d.file_key from archive_documents d "
            "  join pyq_questions q on q.document_id = d.id "
            " where (%s::text is null or d.subject_code = %s)", (code, code)):
        p = paper_file(library, key)
        if p.is_file():
            read.setdefault(sc, set()).add(_sha(p))

    # A paper the model read and found nothing in -- a blank scan, an answer
    # key filed as a paper -- would otherwise be sent, and paid for, on every
    # run, because "done" above means "has a question". The ledger beside the
    # library remembers every paper actually read, with what it gave; it is a
    # cache in the sense .explains/ is one, and deleting it costs one re-read.
    ledger_path = Path(library) / ".pyq-read.json"
    try:
        ledger = json.loads(ledger_path.read_text())
    except (OSError, ValueError):
        ledger = {}

    def remember(doc_id, digest, n):
        ledger[str(doc_id)] = {"sha": digest, "questions": n,
                               "at": time.strftime("%Y-%m-%d %H:%M")}
        try:
            ledger_path.write_text(json.dumps(ledger, indent=1, sort_keys=True))
        except OSError:
            pass

    summary = {"papers": 0, "questions": 0, "skipped": 0, "failed": 0, "missing": 0,
               "cost": 0.0, "stopped": False}
    client = None if not rows else _client(client)
    failures_in_a_row = 0
    for doc_id, sc, exam, year, section, title, key in rows:
        if limit is not None and summary["papers"] >= limit:
            break
        if max_cost is not None and summary["cost"] >= max_cost:
            log(f"stopping: spent ${summary['cost']:.2f} of --max-cost ${max_cost:.2f}")
            summary["stopped"] = True
            break
        path = paper_file(library, key)
        label = f"{sc} {title}"
        if not path.is_file():
            summary["missing"] += 1  # shelved here, bytes on another machine
            continue
        kind = path.suffix.lower()
        if kind != ".pdf" and kind not in IMAGE_TYPES:
            log(f"skip {label}: {kind or 'no extension'} is not a PDF or an image")
            summary["skipped"] += 1
            continue
        size = path.stat().st_size
        cap = MAX_PAPER_BYTES if kind == ".pdf" else MAX_IMAGE_BYTES
        if size > cap:
            log(f"skip {label}: {size / 1e6:.1f} MB is over {cap / 1e6:.0f} MB")
            summary["skipped"] += 1
            continue
        digest = _sha(path)
        if digest in read.get(sc, ()):
            log(f"skip {label}: the same file as a paper already read")
            summary["skipped"] += 1
            continue
        seen = ledger.get(str(doc_id))
        if seen and seen.get("sha") == digest:
            log(f"skip {label}: read on {seen.get('at')}, "
                f"{seen.get('questions', 0)} question(s) then")
            summary["skipped"] += 1
            continue

        sitting = ", ".join(x for x in (
            f"{exam} term" if exam in ("mid", "end") else ("mini test" if exam else None),
            f"{year}-{(year + 1) % 100:02d}" if year else None,
            f"set for section {section}" if section else None) if x)
        kwargs = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=EXTRACT_PROMPT.format(code=sc, name=subject_name(sc),
                                         syllabus=syllabus_text(sc)),
            messages=[{"role": "user", "content": [
                _paper_block(path),
                {"type": "text", "text": f"The paper: {title}"
                                         f"{f' ({sitting})' if sitting else ''}. "
                                         "Transcribe every question as the JSON array."},
            ]}],
        )
        try:
            msg = _create(client, **kwargs)
        except Exception as e:  # one bad PDF must not end a run over 300
            summary["failed"] += 1
            failures_in_a_row += 1
            log(f"FAILED {label}: {type(e).__name__}: {str(e)[:200]}")
            if failures_in_a_row >= 3:
                log("stopping: three papers in a row failed; that is the key or the "
                    "network, not the papers")
                summary["stopped"] = True
                break
            continue
        failures_in_a_row = 0
        cost = _cost(model, msg.usage)
        summary["cost"] += cost
        summary["papers"] += 1

        if msg.stop_reason == "refusal":
            log(f"REFUSED {label}  ${cost:.3f}, total ${summary['cost']:.2f}")
            continue
        qs = parse_json_list(_text_of(msg))
        items = [{
            "code": sc, "document_id": str(doc_id), "exam": exam, "year": year,
            "section": section, "qno": q.get("qno"), "marks": q.get("marks"),
            "question_md": q.get("question_md"), "unit": q.get("unit"),
            "topic": q.get("topic"),
        } for q in qs]
        res = import_items(conn, items, extracted_by=model)
        summary["questions"] += res["inserted"]
        read.setdefault(sc, set()).add(digest)
        remember(doc_id, digest, len(qs))
        note = ""
        if msg.stop_reason == "max_tokens":
            note = "  TRUNCATED at max_tokens: kept what was complete"
        elif not qs:
            note = "  no questions found (remembered; delete .pyq-read.json to retry)"
        elif not res["inserted"]:
            note = "  all already present (a copy of another paper?)"
        log(f"{label}: {len(qs)} question(s), {res['inserted']} new, "
            f"{res['mapped']} placed  ${cost:.3f}, total ${summary['cost']:.2f}{note}")
        for err in res["errors"]:
            log(f"    {err}")
    if summary["missing"]:
        log(f"{summary['missing']} paper(s) have no file under {Path(library).resolve().parent}"
            " -- run this where library/archive/ is")
    return summary


# ---------------------------------------------------------------------------
# Answer.

ANSWER_PROMPT = """You are an experienced examiner for first-year B.Tech at MANIT Bhopal, writing the model answer that scores full marks, for {code} {name}.

For each question, write what a topper writes in the exam hall, sized to its marks:
- 1-2 marks: 3-4 lines. The definition, the one fact or the one formula, stated exactly.
- 3-5 marks: the key points in order, the formula with every symbol defined, a short example or a labelled diagram described in words where the marker expects one.
- 7-10 marks or more: headings or numbered steps, the full derivation with every step shown, the result in bold, units throughout.
- Numericals: given, to find, formula, substitution, result with units, step by step.
- Marks not given: answer as for 5 marks.
- An OR alternative is answered on its own; never answer both halves in one.
- A question with [illegible] or a [Figure: ...] you cannot see: answer what can be answered and say in one line what was assumed.

Markdown. All maths in LaTeX, $...$ inline and $$...$$ for display. When teaching notes are given for a topic, follow their notation and what they say the marker looks for, but answer the question that was asked.

Return ONLY a JSON array, no prose, no code fences, one object per question, using the ids given:
[{{"id": "...", "answer_md": "..."}}]
This is JSON: every backslash in LaTeX is doubled (\\\\frac, \\\\alpha) and newlines are \\n.
"""

_PACK_CACHE = {}


def pack_entries(code):
    """{(unit, topic) lower-cased: the pack's entry} for a subject, or {}.

    Packs are written by other hands at other times; a missing or half-written
    one means answers without notes, not a crash.
    """
    path = PACKS / f"{code}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _PACK_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    out = {}
    try:
        pack = json.loads(path.read_text())
        for u in pack.get("units") or []:
            for t in u.get("topics") or []:
                if isinstance(t, dict) and t.get("topic"):
                    out[(str(u.get("unit", "")).lower(), str(t["topic"]).lower())] = t
    except (OSError, ValueError, AttributeError, TypeError):
        out = {}
    _PACK_CACHE[path] = (mtime, out)
    return out


def _pack_note(entry):
    # The parts an examiner's answer leans on; the pack's own practice
    # questions would double the prompt and teach nothing an answer needs.
    keep = {k: entry[k] for k in ("why", "gist", "key_points", "formulas", "traps", "asked_as")
            if entry.get(k)}
    return json.dumps(keep, ensure_ascii=False, indent=1)


def answer(conn, code=None, model=DEFAULT_MODEL, limit=None, client=None, log=print,
           max_cost=None, batch=ANSWER_BATCH):
    """Write full-marks answers for questions that have none, ~8 per call.

    Resumable the same way extract is: a question is done when it has an
    answer, and one the model skipped is simply asked again next run.
    """
    _savepoints(conn)
    rows = conn.execute(
        "select id, subject_code, exam, year, qno, marks, unit, topic, question_md "
        "  from pyq_questions where answer_md is null "
        "   and (%s::text is null or subject_code = %s) "
        " order by subject_code, unit nulls last, topic nulls last, "
        "          year desc nulls last, qno "
        + ("limit %s" if limit is not None else ""),
        (code, code, limit) if limit is not None else (code, code)).fetchall()

    batches = []
    for r in rows:
        if batches and batches[-1][0][1] == r[1] and len(batches[-1]) < batch:
            batches[-1].append(r)
        else:
            batches.append([r])

    summary = {"batches": 0, "answered": 0, "cost": 0.0, "stopped": False, "failed": 0}
    client = None if not batches else _client(client)
    failures_in_a_row = 0
    for group in batches:
        if max_cost is not None and summary["cost"] >= max_cost:
            log(f"stopping: spent ${summary['cost']:.2f} of --max-cost ${max_cost:.2f}")
            summary["stopped"] = True
            break
        sc = group[0][1]
        pack = pack_entries(sc)
        notes_for, parts = {}, []
        for qid, _, exam, year, qno, marks, unit, topic, q in group:
            entry = pack.get(((unit or "").lower(), (topic or "").lower()))
            if entry and topic not in notes_for:
                notes_for[topic] = entry
            head = " · ".join(str(x) for x in (
                f"Q{qno}" if qno else None,
                f"{float(marks):g} marks" if marks is not None else "marks not given",
                exam and f"{exam} {year or ''}".strip(),
                topic and f"topic: {topic}") if x)
            parts.append(f"### id: {qid}\n{head}\n\n{q}")
        text = ""
        if notes_for:
            text += "Teaching notes for these topics:\n\n" + "\n\n".join(
                f"## {t}\n{_pack_note(e)}" for t, e in notes_for.items()) + "\n\n---\n\n"
        text += "Questions:\n\n" + "\n\n".join(parts)
        kwargs = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=ANSWER_PROMPT.format(code=sc, name=subject_name(sc)),
            messages=[{"role": "user", "content": [{"type": "text", "text": text}]}],
        )
        try:
            msg = _create(client, **kwargs)
        except Exception as e:
            summary["failed"] += 1
            failures_in_a_row += 1
            log(f"FAILED {sc} batch of {len(group)}: {type(e).__name__}: {str(e)[:200]}")
            if failures_in_a_row >= 3:
                log("stopping: three batches in a row failed")
                summary["stopped"] = True
                break
            continue
        failures_in_a_row = 0
        cost = _cost(model, msg.usage)
        summary["cost"] += cost
        summary["batches"] += 1
        if msg.stop_reason == "refusal":
            log(f"REFUSED {sc} batch of {len(group)}  ${cost:.3f}")
            continue
        wanted = {str(r[0]) for r in group}
        done = 0
        for a in parse_json_list(_text_of(msg)):
            qid, body = str(a.get("id", "")).strip(), a.get("answer_md")
            if qid not in wanted or not isinstance(body, str) or not body.strip():
                continue
            if len(body) > 40000:
                log(f"    {qid}: answer is {len(body)} characters, over the limit; left for next run")
                continue
            cur = conn.execute(
                "update pyq_questions set answer_md = %s, answer_model = %s "
                " where id = %s and answer_md is null", (body.strip(), model, qid))
            done += cur.rowcount
            wanted.discard(qid)
        summary["answered"] += done
        note = "  TRUNCATED at max_tokens" if msg.stop_reason == "max_tokens" else ""
        log(f"{sc}: {done}/{len(group)} answered  ${cost:.3f}, "
            f"total ${summary['cost']:.2f}{note}")
    return summary


# ---------------------------------------------------------------------------
# Status.

def status(conn):
    """Per subject: papers shelved, papers read, questions, answered, placed."""
    rows = conn.execute(
        "with p as (select subject_code code, count(*) n from archive_documents "
        "            where kind = 'paper' group by 1), "
        "     q as (select subject_code code, count(distinct document_id) docs, "
        "                  count(*) n, count(answer_md) answered, count(topic) mapped "
        "             from pyq_questions group by 1) "
        "select coalesce(p.code, q.code), coalesce(p.n, 0), coalesce(q.docs, 0), "
        "       coalesce(q.n, 0), coalesce(q.answered, 0), coalesce(q.mapped, 0) "
        "  from p full join q on q.code = p.code order by 1").fetchall()
    return [{"code": c, "papers": p, "extracted": d, "questions": n,
             "answered": a, "mapped": m} for c, p, d, n, a, m in rows]


def print_status(rows, out=print):
    cols = ("papers", "extracted", "questions", "answered", "mapped")
    out(f"{'subject':<8} " + " ".join(f"{c:>9}" for c in cols))
    for r in rows:
        out(f"{r['code']:<8} " + " ".join(f"{r[c]:>9}" for c in cols))
    tot = {c: sum(r[c] for r in rows) for c in cols}
    out(f"{'total':<8} " + " ".join(f"{tot[c]:>9}" for c in cols))


# ---------------------------------------------------------------------------
# CLI.

USAGE = f"""usage: notes.py tutor pyq <command> [options]

  extract [--code X] [--model M] [--limit N] [--max-cost USD]
      read every archived paper that has no questions yet, import its questions
  answer  [--code X] [--model M] [--limit N] [--max-cost USD]
      write full-marks model answers for questions that have none, {ANSWER_BATCH} per call
  import FILE.json [--by NAME]
      import ImportItems: a list, or {{"items": [...]}}; re-running is safe
  status
      per subject: papers shelved, papers read, questions, answered, placed on a topic

--code takes a code or an alias (CY1107, chem); without it, every subject.
--model defaults to {DEFAULT_MODEL}. --limit counts papers for extract and
questions for answer. --max-cost stops the run once it has spent that many
dollars (default {DEFAULT_MAX_COST:g}). extract and answer need ANTHROPIC_API_KEY.
Every command is resumable: run it again and it carries on."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SystemExit(f"{message}\n\n{USAGE}")


def _need_key():
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise SystemExit("set ANTHROPIC_API_KEY: extract and answer call the Claude API")


def cli(argv, library):
    argv = list(argv or [])
    if argv[:1] == ["pyq"]:
        argv = argv[1:]
    else:
        raise SystemExit(USAGE)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]

    p = _Parser(prog=f"notes.py tutor pyq {cmd}", add_help=False)
    if cmd in ("extract", "answer"):
        p.add_argument("--code")
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--limit", type=int)
        p.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST)
    elif cmd == "import":
        p.add_argument("file", type=Path)
        p.add_argument("--by")
    elif cmd != "status":
        raise SystemExit(f"unknown command {cmd!r}\n\n{USAGE}")
    a = p.parse_args(rest)

    if cmd == "import":
        try:
            data = json.loads(a.file.read_text())
        except (OSError, ValueError) as e:
            raise SystemExit(f"can't read {a.file}: {e}")
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise SystemExit(f"{a.file}: expected a list of items or {{\"items\": [...]}}")
        conn = notes.db()
        try:
            res = _import(conn, items, extracted_by=a.by)
        finally:
            conn.close()
        print(f"{len(items)} item(s): {res['inserted']} inserted, {res['skipped']} already "
              f"there ({res['answered']} of those got their answer), {res['mapped']} placed "
              f"on a topic, {len(res['errors'])} refused")
        for err in res["errors"]:
            print(f"  {err}")
        if res["errors"]:
            raise SystemExit(1)
        return 0

    code = notes.resolve_subject(a.code) if getattr(a, "code", None) else None
    if cmd in ("extract", "answer"):
        _need_key()
    conn = notes.db()
    try:
        if cmd == "status":
            print_status(status(conn))
        elif cmd == "extract":
            s = extract(conn, library, code=code, model=a.model, limit=a.limit,
                        max_cost=a.max_cost)
            print(f"{s['papers']} paper(s) read, {s['questions']} new question(s), "
                  f"{s['skipped']} skipped, {s['failed']} failed, ${s['cost']:.2f}")
        else:
            s = answer(conn, code=code, model=a.model, limit=a.limit, max_cost=a.max_cost)
            print(f"{s['answered']} answer(s) in {s['batches']} call(s), "
                  f"{s['failed']} failed, ${s['cost']:.2f}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    cli(sys.argv[1:], notes.DEFAULT_LIBRARY)
