"""The tutor: its policies, its tag grammar, its plan, and its routes.

Three halves. The policies run on the rolled-back `db` connection, acting as
one person after another, because 0056 is the boundary and tutor.py only
trusts it. The pure parts (parse_turn, the plan, importance) run on fake
views. The routes run through notes.py's real server with tutor.model_stream
swapped for a canned lesson, so no test ever spends a cent.
"""

import json
import os
import pathlib
import sys
import threading
import urllib.parse
import uuid

import psycopg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import notes  # noqa: E402
import tutor  # noqa: E402

from conftest import DB_URL, as_admin_connection, as_user, make_user  # noqa: E402
from test_auth import SECRET, call  # noqa: E402

BIO = ("BS1111", "Cell Biology", "Cell theory")


def member(db, role="trusted"):
    uid = make_user(db)
    db.execute("insert into profiles (id, name, status, role) values (%s, 'M', 'approved', %s)",
               (uid, role))
    return uid


def open_session(db, owner, code="BS1111", topic="Cell theory"):
    return db.execute(
        "insert into tutor_sessions (owner_id, subject_code, unit, topic, mode, title)"
        " values (%s, %s, 'Cell Biology', %s, 'teach', 'Teach · Cell theory') returning id",
        (owner, code, topic)).fetchone()[0]


# ------------------------------------------------------------ policies


def test_a_trusted_reader_sees_the_lesson_and_a_student_does_not(db):
    owner, reader, student = member(db, "admin"), member(db, "trusted"), member(db, "student")
    as_user(db, owner)
    sid = open_session(db, owner)
    db.execute("insert into tutor_messages (session_id, role, body) values (%s, 'tutor', 'hi')",
               (sid,))
    db.execute("insert into tutor_mastery (owner_id, subject_code, unit, topic, level)"
               " values (%s, 'BS1111', 'Cell Biology', 'Cell theory', 2)", (owner,))

    as_user(db, reader)
    assert db.execute("select count(*) from tutor_sessions where id = %s", (sid,)).fetchone()[0] == 1
    assert db.execute("select count(*) from tutor_messages where session_id = %s",
                      (sid,)).fetchone()[0] == 1
    assert db.execute("select count(*) from tutor_mastery where owner_id = %s",
                      (owner,)).fetchone()[0] == 1

    as_user(db, student)
    assert db.execute("select count(*) from tutor_sessions where id = %s", (sid,)).fetchone()[0] == 0
    assert db.execute("select count(*) from tutor_messages where session_id = %s",
                      (sid,)).fetchone()[0] == 0
    assert db.execute("select count(*) from tutor_mastery").fetchone()[0] == 0


def test_only_the_owner_writes_and_messages_follow_the_session(db):
    owner, reader = member(db, "admin"), member(db, "trusted")
    as_user(db, owner)
    sid = open_session(db, owner)

    as_user(db, reader)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        open_session(db, owner)   # in somebody else's name
    assert db.execute("update tutor_sessions set title = 'mine now' where id = %s",
                      (sid,)).rowcount == 0
    assert db.execute("delete from tutor_sessions where id = %s", (sid,)).rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("insert into tutor_messages (session_id, role, body)"
                   " values (%s, 'student', 'hijack')", (sid,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("insert into tutor_mastery (owner_id, subject_code, unit, topic)"
                   " values (%s, 'BS1111', 'Cell Biology', 'Cell theory')", (owner,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("insert into tutor_profile (owner_id, subject_code) values (%s, 'BS1111')",
                   (owner,))

    as_user(db, owner)
    db.execute("insert into tutor_messages (session_id, role, body) values (%s, 'student', 'q')",
               (sid,))
    assert db.execute("delete from tutor_sessions where id = %s", (sid,)).rowcount == 1
    as_admin_connection(db)
    assert db.execute("select count(*) from tutor_messages where session_id = %s",
                      (sid,)).fetchone()[0] == 0, "messages go with their lesson"


def test_past_paper_questions_are_read_by_members_and_written_by_admins(db):
    admin, reader, student = member(db, "admin"), member(db, "trusted"), member(db, "student")
    as_user(db, reader)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("insert into pyq_questions (subject_code, question_md, fingerprint)"
                   " values ('BS1111', 'Q', 'policy-test-1')")
    as_user(db, admin)
    qid = db.execute("insert into pyq_questions (subject_code, question_md, fingerprint)"
                     " values ('BS1111', 'Q', 'policy-test-1') returning id").fetchone()[0]
    as_user(db, student)
    assert db.execute("select count(*) from pyq_questions where id = %s", (qid,)).fetchone()[0] == 1
    assert db.execute("update pyq_questions set question_md = 'X' where id = %s",
                      (qid,)).rowcount == 0
    assert db.execute("delete from pyq_questions where id = %s", (qid,)).rowcount == 0


def test_two_marks_the_same_way_move_the_level_one_step(db):
    owner = member(db, "admin")
    as_user(db, owner)
    sid = tutor.db_create_session(db, owner, *BIO, "teach", "english", "exam", "standard",
                                  "Teach · Cell theory", "m")
    session = tutor.db_session(db, sid, owner)
    tutor.db_apply_state(db, owner, *BIO, {"level": 2})
    checks = [{"id": f"c{n}", "kind": "mcq", "options": ["a", "b", "c", "d"], "answer": 1}
              for n in range(1, 7)]
    tutor.db_add_message(db, sid, "tutor", "lesson", "...", {"checks": checks})

    m, _ = tutor.db_mark(db, owner, session, "c1", 1, True)
    assert (m["level"], m["attempts"], m["correct"]) == (2, 1, 1)
    m, _ = tutor.db_mark(db, owner, session, "c2", 1, True)
    assert (m["level"], m["attempts"], m["correct"]) == (3, 2, 2), "two right: up one"
    m, msg = tutor.db_mark(db, owner, session, "c2", 1, True)
    assert msg is None and m["attempts"] == 2, "the same check is not counted twice"
    m, _ = tutor.db_mark(db, owner, session, "c3", 1, True)
    assert m["level"] == 3, "the run started again after the step"
    # The card says right; the stored answer says index 1 -- the server's word wins.
    m, msg = tutor.db_mark(db, owner, session, "c4", 0, True)
    assert msg["meta"]["correct"] is False and m["correct"] == 3
    m, _ = tutor.db_mark(db, owner, session, "c5", 2, False)
    assert m["level"] == 2, "two wrong: down one"
    assert "picked C" in msg["body"] or "picked A" in msg["body"]


# ------------------------------------------------------------ the tags


LESSON = """Cell theory has **three postulates**, and the end term asks for them by name.

<pyq id="{pyq}"/>
<pyq id="{ghost}"/>

<check id="c1" kind="mcq">
{{"q": "Who said every cell comes from a cell?", "options": ["Hooke", "Schleiden", "Virchow", "Brown"], "answer": 2, "why": "$\\\\textit{{Omnis cellula e cellula}}$ is Virchow, 1855."}}
</check>

<state>{{"level": 2, "importance": "high", "why": "asked in end terms", "note": "knows the names", "profile": "likes the history first", "next": "Cell types"}}</state>"""


def test_parse_turn_reads_a_whole_lesson():
    pyq, ghost = str(uuid.uuid4()), str(uuid.uuid4())
    got = tutor.parse_turn(LESSON.format(pyq=pyq, ghost=ghost))
    assert got["pyqs"] == [pyq, ghost]
    (check,) = got["checks"]
    assert check["id"] == "c1" and check["kind"] == "mcq" and check["answer"] == 2
    assert check["options"][2] == "Virchow" and "\\textit" in check["why"]
    assert got["state"] == {"level": 2, "importance": "high", "why": "asked in end terms",
                            "note": "knows the names", "profile": "likes the history first",
                            "next": "Cell types"}


def test_parse_turn_forgives_what_models_get_wrong():
    text = ("Try this.\n<check id='c3' kind=mcq>\n```json\n{\"q\": \"$\\frac{1}{2}$ of 4?\", "
            "\"options\": [\"1\", \"2\", \"3\", \"4\",], \"answer\": \"1\"}\n```\n</check>\n"
            "<check kind=\"short\">{'q': 'Define a cell.', 'marks': 2}</check>\n"
            "<pyq id=\"not-an-id\"/><pyq id=\"%s\"></pyq>\n"
            "<state>{\"level\": 2}</state>\n<state>{\"level\": 9, \"importance\": \"MUST\", "
            "\"done\": \"true\", \"note\": 4}</state>") % ("A" * 8 + "-1111-4222-8333-444455556666")
    got = tutor.parse_turn(text)
    first, second = got["checks"]
    assert first["q"] == "$\\frac{1}{2}$ of 4?", "LaTeX survives JSON that forgot to escape it"
    assert first["options"] == ["1", "2", "3", "4"] and first["answer"] == 1
    assert second == {"q": "Define a cell.", "marks": 2, "id": "c2", "kind": "short"}
    assert got["pyqs"] == ["aaaaaaaa-1111-4222-8333-444455556666"]
    assert got["state"] == {"level": 5, "importance": "must", "done": True}, "the last state wins"


def test_parse_turn_keeps_a_broken_check_and_survives_a_cut_off_turn():
    got = tutor.parse_turn("<check id=\"c1\" kind=\"short\">What is a {cell}, really</check>\n"
                           "<check id=\"c2\">{\"q\": \"cut off")
    assert got["checks"][0]["broken"] and "What is a" in got["checks"][0]["q"]
    assert got["checks"][1]["id"] == "c2"
    assert got["state"] is None and got["pyqs"] == []
    assert tutor.parse_turn("") == {"state": None, "checks": [], "pyqs": []}
    assert tutor.parse_turn("<state>not json at all</state>")["state"] is None


def test_last_line_is_what_a_person_would_read():
    assert tutor.last_line(LESSON.format(pyq=uuid.uuid4(), ghost=uuid.uuid4())) == \
        "Cell theory has three postulates, and the end term asks for them by name."


# ------------------------------------------------- the plan and importance


def topic(name, level=0, importance=None, source=None, last_seen=None, pyq=0, exams=None):
    return {"topic": name, "level": level, "importance": importance,
            "importance_source": source, "why": None, "last_seen": last_seen,
            "pyq_count": pyq, "pyq_exams": exams or {}}


def test_plan_ranks_importance_times_what_is_left_times_recency():
    import datetime
    now = datetime.datetime(2026, 10, 19, 12, tzinfo=datetime.timezone.utc)
    yesterday = (now - datetime.timedelta(days=1)).isoformat()
    last_week = (now - datetime.timedelta(days=7)).isoformat()
    views = [{"code": "BS1111", "units": [{"unit": "U", "topics": [
        topic("must, level 1", 1, "must", "pyq", last_week, 4, {"end": 4}),   # 4*.8*.8 = 2.56
        topic("high, level 4", 4, "high", "tutor", last_week),                # 3*.2*.8 = .48
        topic("unknown, unseen"),                                             # 1.5
        topic("low, seen yesterday", 0, "low", "estimate", yesterday),        # .6
        topic("finished", 5, "must", "pyq", last_week, 2),                    # 0: left out
    ]}]}]
    got = tutor.plan(views, now=now)
    assert [p["topic"] for p in got] == ["must, level 1", "unknown, unseen",
                                         "low, seen yesterday", "high, level 4"]
    assert got[0]["score"] == 2.56
    assert got[0]["reason"] == "4 end-term questions, you are at level 1"
    assert got[0]["suggested_mode"] == "revise"
    assert got[1]["reason"] == "on the syllabus, not started yet"
    assert got[1]["suggested_mode"] == "teach"
    assert got[3]["suggested_mode"] == "quiz"


def test_an_empty_plan_opens_every_subject_rather_than_one():
    views = [{"code": c, "units": [{"unit": "U", "topics": [topic("a"), topic("b")]}]}
             for c in ("X1", "X2", "X3")]
    assert [p["code"] for p in tutor.plan(views, limit=3)] == ["X1", "X2", "X3"]


def test_pyq_importance_is_by_quartile_across_the_subject():
    stats = {("U", "a"): {"count": 6, "marks": 40, "exams": {"end": 4, "mid": 2}},
             ("U", "b"): {"count": 3, "marks": 10, "exams": {}},
             ("U", "c"): {"count": 3, "marks": 10, "exams": {}},
             ("U", "d"): {"count": 1, "marks": 2, "exams": {"mini": 1}},
             (None, None): {"count": 9, "marks": 0, "exams": {}}}
    got = tutor.rank_importance(stats)
    assert {k: v[0] for k, v in got.items()} == {
        ("U", "a"): "must", ("U", "b"): "high", ("U", "c"): "high", ("U", "d"): "low"}
    assert got[("U", "a")][1] == "6 past-paper questions (4 end, 2 mid), 40 marks in all"


@pytest.fixture
def packs(tmp_path, monkeypatch):
    (tmp_path / "BS1111.json").write_text(json.dumps({
        "code": "BS1111", "name": "Biology for Engineers", "exam_notes": "Short answers.",
        "units": [{"unit": "Cell Biology", "topics": [
            {"topic": "Cell theory", "importance": "medium", "why": "pack says so",
             "gist": "Cells come from cells. GIST-MARKER", "key_points": ["three postulates"],
             "formulas": [], "traps": ["Hooke saw dead cells"], "asked_as": "State ... 2 marks",
             "questions": [{"q": "Who?", "a": "Virchow", "marks": 1, "kind": "mcq",
                            "options": ["A", "B", "C", "D"], "answer_index": 2}]},
            {"topic": "Cell types", "importance": "low", "why": "rarely asked"}]}]}))
    (tmp_path / "HS1112.json").write_text(json.dumps({
        "code": "HS1112", "name": "Indian Knowledge Systems", "syllabus_estimated": True,
        "units": [{"unit": "Foundations", "topics": [
            {"topic": "What IKS is", "importance": "medium", "why": "estimate"}]}]}))
    monkeypatch.setattr(tutor, "PACK_DIR", tmp_path)
    return tmp_path


def test_importance_comes_from_pyqs_then_the_tutor_then_the_pack(packs):
    mastery = {("BS1111", "Cell Biology", "Cell types"):
               {"level": 1, "importance": "must", "importance_why": "the tutor's flag"}}
    stats = {("Cell Biology", "Cell theory"): {"count": 2, "marks": 10, "exams": {"end": 2}}}
    view = tutor.subject_view("BS1111", mastery, stats)
    tops = {t["topic"]: t for t in view["units"][0]["topics"]}
    assert (tops["Cell theory"]["importance"], tops["Cell theory"]["importance_source"]) == ("must", "pyq")
    assert (tops["Cell types"]["importance"], tops["Cell types"]["importance_source"]) == ("must", "tutor")
    assert tops["Cell organelles: structure and function"]["importance"] is None
    assert view["importance_source"] == "pyq" and view["units"][0]["progress"] == 0.04
    assert tops["Cell theory"]["has_pack"] and not tops["Cell organelles: structure and function"]["has_pack"]

    view = tutor.subject_view("BS1111")
    tops = {t["topic"]: t for t in view["units"][0]["topics"]}
    assert (tops["Cell types"]["importance"], tops["Cell types"]["importance_source"]) == ("low", "estimate")
    assert view["importance_source"] == "estimate"


def test_a_pack_supplies_the_syllabus_a_subject_does_not_have(packs):
    assert "HS1112" not in notes.SYLLABUS
    units, estimated = tutor.syllabus("HS1112")
    assert estimated and units == [{"unit": "Foundations", "topics": ["What IKS is"]}]
    assert "HS1112" in tutor.tutor_codes()
    assert tutor.subject_view("HS1112")["syllabus_estimated"] is True
    assert tutor.syllabus("BS1111")[1] is False, "syllabus.json still wins where it has the code"


def test_a_pack_is_reread_when_it_changes(packs):
    assert tutor.pack_topic(*BIO)["importance"] == "medium"
    path = packs / "BS1111.json"
    data = json.loads(path.read_text())
    data["units"][0]["topics"][0]["importance"] = "must"
    path.write_text(json.dumps(data))
    os.utime(path, ns=(path.stat().st_mtime_ns + 10**9,) * 2)
    assert tutor.pack_topic(*BIO)["importance"] == "must"
    path.write_text("{ half written")
    os.utime(path, ns=(path.stat().st_mtime_ns + 2 * 10**9,) * 2)
    assert tutor.load_pack("BS1111") is None, "a broken pack is no pack, not a 500"


def test_cost_counts_the_cache_at_its_own_rates():
    usage = {"input_tokens": 1000, "output_tokens": 500, "cache_read_tokens": 2000,
             "cache_write_tokens": 3000}
    rate_in, rate_out = notes.price_of("claude-opus-5-5")
    want = (1000 * rate_in + 500 * rate_out + 3000 * 1.25 * rate_in + 2000 * 0.1 * rate_in) / 1e6
    assert tutor.cost_of("claude-opus-5-5", usage) == pytest.approx(want)


# ---------------------------------------------------------- the server


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """notes.py's server on a real socket, an owner (two, so one can be
    refused the other's lesson), a trusted reader, a student, the visitor."""
    os.environ["RECARVE_SECRET"] = SECRET.decode()
    os.environ.pop("RECARVE_TUTOR_MODEL", None)
    lib = tmp_path_factory.mktemp("library")
    pack_dir = tmp_path_factory.mktemp("packs")
    (pack_dir / "BS1111.json").write_text(json.dumps({
        "code": "BS1111", "name": "Biology for Engineers",
        "units": [{"unit": "Cell Biology", "topics": [
            {"topic": "Cell theory", "importance": "high", "why": "pack",
             "gist": "Cells come from cells. GIST-MARKER"}]}]}))
    old_packs, tutor.PACK_DIR = tutor.PACK_DIR, pack_dir

    tag = uuid.uuid4().hex[:8]
    people = {}
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        # The visitor is a roll number, and roll numbers are unique.
        conn.execute("delete from profiles where upper(roll_no) = 'VISITOR'")
        for name, roll, role in (("Asha", f"TA-{tag}", "admin"), ("Esha", f"TE-{tag}", "admin"),
                                 ("Bilal", f"TB-{tag}", "trusted"),
                                 ("Chan", f"TC-{tag}", "student"),
                                 ("Visitor", "visitor", "student")):
            uid = make_user(conn)
            conn.execute(
                "insert into profiles (id, name, roll_no, status, role, password)"
                " values (%s, %s, %s, 'approved', %s, 'a-real-password')",
                (uid, name, roll, role))
            people[name] = uid
        pyq = str(conn.execute(
            "insert into pyq_questions (subject_code, exam, year, marks, unit, topic,"
            " question_md, answer_md, fingerprint) values ('BS1111', 'end', 2024, 5,"
            " 'Cell Biology', 'Cell theory', 'State the postulates of cell theory.',"
            " 'Three postulates ...', %s) returning id", (f"test-tutor-{tag}",)).fetchone()[0])

    args = notes.argparse.Namespace(
        library=lib, out=lib / "site" / "index.html", host="127.0.0.1", port=0,
        notes_model="claude-haiku-4-5", max_cost=1.0, max_explains=0,
        no_auth=False, verbose=False,
    )
    srv = notes.build_server(args)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield type("Server", (), {
        "port": srv.server_address[1], "ids": people, "pyq": pyq, "tag": tag,
        "cookie": {n: notes.sign_session(i, SECRET) for n, i in people.items()}})

    srv.shutdown()
    srv.server_close()
    tutor.PACK_DIR = old_packs
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("delete from pyq_questions where fingerprint = %s or question_md like %s",
                     (f"test-tutor-{tag}", f"%{tag}%"))
        conn.execute("delete from profiles where id = any(%s::uuid[])", (list(people.values()),))
        conn.execute("delete from auth.users where id = any(%s::uuid[])", (list(people.values()),))


@pytest.fixture
def model(server, monkeypatch):
    """The canned lesson, streamed forty characters at a time. Records what
    it was asked, so a test can read the prompt the server built."""
    calls = []
    reply = {"stop_reason": "end_turn"}

    def fake(system_blocks, messages, model_name, effort):
        calls.append({"system": system_blocks, "messages": messages, "model": model_name,
                      "effort": effort})
        text = LESSON.format(pyq=server.pyq, ghost=uuid.uuid4())
        for i in range(0, len(text), 40):
            yield text[i:i + 40]
        return {"input_tokens": 1000, "output_tokens": 500, "cache_read_tokens": 2000,
                "cache_write_tokens": 3000, "stop_reason": reply["stop_reason"],
                "model": model_name}

    monkeypatch.setattr(tutor, "model_stream", fake)
    monkeypatch.delenv("RECARVE_TUTOR_MODEL", raising=False)
    monkeypatch.delenv("RECARVE_TUTOR_EFFORT", raising=False)
    return type("Model", (), {"calls": calls, "reply": reply})


def get(server, who, path):
    status, body, _ = call(server.port, "GET", path, cookie=server.cookie[who])
    return status, json.loads(body)


def post(server, who, path, payload):
    status, body, _ = call(server.port, "POST", path, payload, cookie=server.cookie[who])
    return status, json.loads(body)


def stream(server, who, path, payload):
    status, body, headers = call(server.port, "POST", path, payload, cookie=server.cookie[who])
    assert status == 200, body
    assert headers["Content-Type"] == "application/x-ndjson"
    assert headers["Transfer-Encoding"] == "chunked"
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def start(server, who="Asha", **payload):
    payload = {"code": "BS1111", "unit": "Cell Biology", "topic": "Cell theory",
               "mode": "teach", **payload}
    return stream(server, who, "/tutor/start", payload)


def test_start_streams_a_lesson_and_stores_what_it_said(server, model):
    lines = start(server)
    assert lines[0]["t"] == "session"
    session = lines[0]["session"]
    assert session["title"] == "Teach · Cell theory" and session["mine"]
    deltas = [x["text"] for x in lines if x["t"] == "delta"]
    assert len(deltas) > 5, "streamed, not delivered at the end"
    done = lines[-1]
    assert done["t"] == "done", lines[-1]
    msg = done["message"]
    assert msg["body"] == "".join(deltas) and msg["role"] == "tutor" and msg["kind"] == "lesson"
    assert msg["meta"]["pyqs"] == [server.pyq], "an id that is not in the table is not cited"
    assert msg["meta"]["checks"][0]["id"] == "c1"
    assert msg["meta"]["state"]["level"] == 2
    assert msg["cost_usd"] == pytest.approx(0.0149), "priced at the standard tier's model"
    assert done["session"]["turns"] == 1
    assert done["session"]["cost_usd"] == pytest.approx(0.0149)
    (mastery,) = done["mastery"]
    assert (mastery["level"], mastery["importance"], mastery["note"]) == (2, "high", "knows the names")
    assert mastery["last_seen"]
    assert done["spend"]["today"] >= 0.0149

    (asked,) = model.calls
    assert session["tier"] == "standard"
    assert asked["model"] == "claude-sonnet-5-5" and asked["effort"] == "medium"
    assert "Tier: standard" in asked["system"][3]["text"]
    system = asked["system"]
    assert [("cache_control" in b) for b in system] == [False, False, True, False], \
        "one breakpoint, on the last stable block"
    assert "<check" in system[0]["text"] and "Cell theory" in system[1]["text"]
    assert server.pyq in system[2]["text"] and "GIST-MARKER" in system[2]["text"]
    assert "level 0/5" in system[3]["text"]
    assert [m["role"] for m in asked["messages"]] == ["user"]
    assert "Teach me Cell theory" in asked["messages"][0]["content"]

    status, prof = get(server, "Asha", "/tutor/subject?code=BS1111")
    assert prof["profile"]["notes"] == "likes the history first", "<state> profile is kept"


def test_say_continues_the_lesson_and_a_quick_action_is_a_sentence(server, model):
    sid = start(server)[0]["session"]["id"]
    lines = stream(server, "Asha", "/tutor/say", {"session_id": sid, "action": "deeper"})
    assert lines[0]["t"] == "delta", "only /tutor/start sends the session first"
    assert lines[-1]["t"] == "done" and lines[-1]["session"]["turns"] == 2
    asked = model.calls[-1]["messages"]
    assert [m["role"] for m in asked] == ["user", "assistant", "user"]
    assert asked[1]["content"].startswith("Cell theory has"), "tutor turns go back verbatim"
    assert asked[2]["content"] == tutor.ACTIONS["deeper"]

    status, got = get(server, "Asha", f"/tutor/session?id={sid}")
    assert status == 200 and got["owner"] is True
    assert [(m["role"], m["kind"]) for m in got["messages"]] == [
        ("tutor", "lesson"), ("student", "action"), ("tutor", "lesson")]
    assert got["messages"][1]["meta"] == {"action": "deeper"}

    lines = stream(server, "Asha", "/tutor/say", {"session_id": sid, "body": "Why Virchow?",
                                                  "kind": "answer"})
    assert lines[-1]["t"] == "done"
    assert model.calls[-1]["messages"][-1]["content"] == "Why Virchow?"
    status, err = post(server, "Asha", "/tutor/say", {"session_id": sid, "body": "  "})
    assert status == 400


def test_mark_counts_the_card_once_and_shows_the_tutor_the_pick(server, model):
    sid = start(server)[0]["session"]["id"]
    status, got = post(server, "Asha", "/tutor/mark",
                       {"session_id": sid, "check_id": "c1", "picked": 2, "correct": True})
    assert status == 200, got
    assert (got["mastery"]["attempts"], got["mastery"]["correct"]) == (1, 1)
    assert "Virchow" in got["message"]["body"]
    status, again = post(server, "Asha", "/tutor/mark",
                         {"session_id": sid, "check_id": "c1", "picked": 2, "correct": True})
    assert status == 200 and again["mastery"]["attempts"] == 1
    status, bad = post(server, "Asha", "/tutor/mark",
                       {"session_id": sid, "check_id": "c1", "picked": 2, "correct": "yes"})
    assert status == 400

    stream(server, "Asha", "/tutor/say", {"session_id": sid, "body": "and why?"})
    asked = model.calls[-1]["messages"]
    assert asked[-1]["role"] == "user" and "picked C" in asked[-1]["content"] \
        and asked[-1]["content"].endswith("and why?"), "the mark and the question are one turn"


def test_a_trusted_reader_reads_the_lesson_and_cannot_write_in_it(server, model):
    sid = start(server)[0]["session"]["id"]
    status, got = get(server, "Bilal", f"/tutor/session?id={sid}")
    assert status == 200 and got["owner"] is False and not got["session"]["mine"]
    assert got["session"]["owner_name"] == "Asha" or got["session"]["owner_name"] is None
    assert got["pyqs"][server.pyq]["question_md"] == "State the postulates of cell theory."
    assert got["pyqs"][server.pyq]["path"] is None and got["mastery"]["level"] == 2

    status, home = get(server, "Bilal", "/tutor")
    assert status == 200 and home["owner"] is False and home["learner"]["id"] == server.ids["Asha"]
    assert any(s["id"] == sid for s in home["recent"])

    for path, payload in (("/tutor/say", {"session_id": sid, "body": "hi"}),
                          ("/tutor/start", {"code": "BS1111", "mode": "ask"}),
                          ("/tutor/mark", {"session_id": sid, "check_id": "c1", "correct": True}),
                          ("/tutor/session/delete", {"id": sid})):
        status, _ = post(server, "Bilal", path, payload)
        assert status == 403, path
    # Gated on the path, so a POST to a read path reaches tutor.py as a
    # trusted reader: it is refused there.
    status, _ = post(server, "Bilal", "/tutor", {})
    assert status == 404

    # Another owner can read it too, and still cannot write in it.
    status, err = post(server, "Esha", "/tutor/say", {"session_id": sid, "body": "hi"})
    assert status == 403 and "somebody else's" in err["error"]


def test_a_student_is_refused_reading_and_writing(server, model):
    for path in ("/tutor", "/tutor/subject?code=BS1111", "/tutor/pyq?code=BS1111"):
        status, got = get(server, "Chan", path)
        assert status == 403 and got["required"] == "trusted", path
    status, got = post(server, "Chan", "/tutor/start", {"code": "BS1111", "mode": "ask"})
    assert status == 403
    assert not model.calls


def test_the_visitor_cannot_make_the_tutor_teach(server, model):
    status, got = post(server, "Visitor", "/tutor/start", {"code": "BS1111", "mode": "ask"})
    assert status == 403 and "visitors" in got["error"]
    assert not model.calls


def test_the_overview(server, model):
    start(server)
    status, home = get(server, "Asha", "/tutor")
    assert status == 200
    assert home["owner"] is True and home["model"] == "claude-sonnet-5-5"
    assert home["tiers"]["advanced"]["model"] == "claude-opus-5-5"
    assert home["tiers"]["advanced"]["effort"] == "medium"
    assert home["tiers"]["standard"]["price_in"] < home["tiers"]["advanced"]["price_in"]
    assert home["role_needed"] == notes.TUTOR_ROLE
    bio = next(s for s in home["subjects"] if s["code"] == "BS1111")
    theory = bio["units"][0]["topics"][0]
    assert theory["topic"] == "Cell theory" and theory["level"] == 2
    assert (theory["importance"], theory["importance_source"]) == ("must", "pyq")
    assert theory["pyq_count"] >= 1 and theory["has_pack"] and theory["sessions"] >= 1
    assert bio["importance_source"] == "pyq" and bio["progress"] > 0
    assert 0 < len(home["plan"]) <= 8
    assert all(set(p) == {"code", "unit", "topic", "reason", "score", "suggested_mode"}
               for p in home["plan"])
    assert home["recent"][0]["last_line"]
    assert home["spend"]["total"] > 0

    status, subj = get(server, "Asha", "/tutor/subject?code=BS1111")
    assert status == 200 and subj["sessions"] and subj["pyq_summary"]["count"] >= 1
    assert 2024 in subj["pyq_summary"]["years"] and subj["plan"]
    status, _ = get(server, "Asha", "/tutor/subject?code=NC1151")
    assert status == 404, "no syllabus, no subject"


def test_a_refusal_is_an_error_line_and_nothing_is_stored(server, model):
    model.reply["stop_reason"] = "refusal"
    lines = start(server)
    assert lines[-1]["t"] == "error" and "declined" in lines[-1]["error"]
    sid = lines[0]["session"]["id"]
    status, got = get(server, "Asha", f"/tutor/session?id={sid}")
    assert got["messages"] == [] and got["session"]["turns"] == 0
    assert got["session"]["cost_usd"] > 0, "the refused call is still on the bill"


def test_a_model_failure_mid_lesson_is_an_error_line(server, model, monkeypatch):
    def broken(*a):
        yield "Half a sentence"
        raise RuntimeError("the line went dead")
    monkeypatch.setattr(tutor, "model_stream", broken)
    lines = start(server)
    assert [x["t"] for x in lines] == ["session", "delta", "error"]
    assert "the line went dead" in lines[-1]["error"]


def test_settings_override_close_and_delete(server, model):
    status, got = post(server, "Asha", "/tutor/settings",
                       {"code": "BS1111", "lang": "hinglish", "depth": "deep"})
    assert status == 200 and (got["profile"]["lang"], got["profile"]["depth"]) == ("hinglish", "deep")
    status, _ = post(server, "Asha", "/tutor/settings", {"code": "BS1111", "lang": "french"})
    assert status == 400

    lines = start(server, mode="quiz")
    assert lines[0]["session"]["lang"] == "hinglish", "a new lesson takes the saved language"
    assert "Hinglish" in model.calls[-1]["system"][3]["text"]
    sid = lines[0]["session"]["id"]

    status, got = post(server, "Asha", "/tutor/mastery",
                       {"code": "BS1111", "unit": "Cell Biology", "topic": "Cell types",
                        "level": 4})
    assert status == 200 and got["mastery"]["level"] == 4 and got["mastery"]["last_seen"] is None
    status, _ = post(server, "Asha", "/tutor/mastery",
                     {"code": "BS1111", "topic": "Not a topic", "level": 4})
    assert status == 400

    assert post(server, "Asha", "/tutor/session/close", {"id": sid}) == (200, {"ok": True})
    assert get(server, "Asha", f"/tutor/session?id={sid}")[1]["session"]["status"] == "done"
    assert post(server, "Asha", "/tutor/session/delete", {"id": sid}) == (200, {"ok": True})
    assert get(server, "Asha", f"/tutor/session?id={sid}")[0] == 404
    post(server, "Asha", "/tutor/settings", {"code": "BS1111", "lang": "english", "depth": "exam"})


def test_the_advanced_tier_is_the_best_model_with_the_whole_unit_in_view(server, model):
    status, got = post(server, "Asha", "/tutor/settings", {"code": "BS1111", "tier": "advanced"})
    assert status == 200 and got["profile"]["tier"] == "advanced"
    assert post(server, "Asha", "/tutor/settings", {"code": "BS1111", "tier": "premium"})[0] == 400
    # A picture of the learner from another subject, for the tutor to carry over.
    post(server, "Asha", "/tutor/settings", {"code": "CY1107", "lang": "english"})
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("update tutor_profile set notes = 'LEARNS-BY-DRAWING'"
                     " where subject_code = 'CY1107' and owner_id = %s", (server.ids["Asha"],))

    lines = start(server)
    session = lines[0]["session"]
    assert session["tier"] == "advanced" and session["model"] == "claude-opus-5-5"
    asked = model.calls[-1]
    assert asked["model"] == "claude-opus-5-5" and asked["effort"] == "medium"
    system = asked["system"]
    assert "Tier: advanced" in system[3]["text"] and "diagnostic" in system[3]["text"]
    assert "LEARNS-BY-DRAWING" in system[3]["text"], "how they learn elsewhere comes along"
    assert server.pyq in system[2]["text"]
    assert lines[-1]["message"]["cost_usd"] == pytest.approx(0.0298), "priced at Opus"

    lines = start(server, tier="standard")
    assert lines[0]["session"]["tier"] == "standard", "a lesson may pick its own tier"
    assert model.calls[-1]["model"] == "claude-sonnet-5-5"
    assert "LEARNS-BY-DRAWING" not in model.calls[-1]["system"][3]["text"]
    post(server, "Asha", "/tutor/settings", {"code": "BS1111", "tier": "standard"})


def test_bad_requests(server, model):
    assert get(server, "Asha", "/tutor/session?id=nope")[0] == 400
    assert get(server, "Asha", f"/tutor/session?id={uuid.uuid4()}")[0] == 404
    assert get(server, "Asha", "/tutor/nowhere")[0] == 404
    assert post(server, "Asha", "/tutor/start", {"code": "BS1111", "mode": "lecture"})[0] == 400
    assert post(server, "Asha", "/tutor/start",
                {"code": "BS1111", "topic": "Quantum gravity", "mode": "teach"})[0] == 400
    assert not model.calls, "nothing reached the model"


def test_a_past_paper_question_carries_its_figure_as_a_library_path(server):
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("update pyq_questions set figure = 'library/pyq-figures/x.png' where id = %s",
                     (server.pyq,))
    status, got = get(server, "Asha", "/tutor/pyq?code=BS1111")
    assert status == 200
    (row,) = [q for q in got["questions"] if q["id"] == server.pyq]
    assert row["figure"] == "/library/pyq-figures/x.png", "served like the paper itself"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("update pyq_questions set figure = null where id = %s", (server.pyq,))


def test_pyq_import_is_idempotent_and_the_bank_shows_it(server):
    items = [{"code": "BS1111", "exam": "mid", "year": 2023, "marks": 2,
              "unit": "Cell Biology", "topic": "Cell theory",
              "question_md": f"Who first saw cells? ({server.tag})", "answer_md": "Hooke, 1665."},
             {"code": "BS1111", "exam": "end", "year": 2022, "marks": 5,
              "question_md": f"Explain cell division. ({server.tag})"}]
    status, first = post(server, "Asha", "/tutor/pyq/import", {"items": items})
    assert status == 200, first
    assert first["inserted"] == 2 and first["skipped"] == 0 and not first["errors"]
    status, again = post(server, "Asha", "/tutor/pyq/import", {"items": items})
    assert (again["inserted"], again["skipped"]) == (0, 2), "re-running an import adds nothing"
    assert post(server, "Bilal", "/tutor/pyq/import", {"items": items})[0] == 403

    q = urllib.parse.urlencode({"code": "BS1111", "topic": "Cell theory", "year": 2023})
    status, bank = get(server, "Bilal", f"/tutor/pyq?{q}")
    assert status == 200
    assert [x["question_md"] for x in bank["questions"]] == [f"Who first saw cells? ({server.tag})"]
    assert bank["counts"]["Cell Biology"]["Cell theory"] >= 2
    status, bank = get(server, "Bilal", "/tutor/pyq?code=BS1111")
    years = [x["year"] for x in bank["questions"]]
    assert years == sorted(years, reverse=True), "newest first"
    assert get(server, "Bilal", "/tutor/pyq?code=BS1111&exam=final")[0] == 400


def test_model_stream_asks_for_fallbacks_and_falls_back_without_them(monkeypatch):
    """The one function that talks to the API, against a stand-in client:
    the request it makes, the usage it hands back, and the plain stream it
    drops to when the SDK has never heard of `fallbacks`."""
    import types

    import anthropic

    asked = []
    final = types.SimpleNamespace(
        stop_reason="end_turn", model="claude-opus-5-5",
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5,
                                    cache_read_input_tokens=3, cache_creation_input_tokens=7))

    class Manager:
        def __enter__(self):
            return types.SimpleNamespace(text_stream=iter(["Hel", "lo"]),
                                         get_final_message=lambda: final)

        def __exit__(self, *exc):
            asked.append("closed")

    def client(beta_works):
        def beta(**kw):
            asked.append(("beta", kw))
            if not beta_works:
                raise TypeError("unexpected keyword argument 'fallbacks'")
            return Manager()

        def plain(**kw):
            asked.append(("plain", kw))
            return Manager()
        return types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(stream=beta)),
                                     messages=types.SimpleNamespace(stream=plain))

    def drain(gen):
        out = []
        while True:
            try:
                out.append(next(gen))
            except StopIteration as stop:
                return out, stop.value

    system = [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": "hi"}]
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client(True))
    text, usage = drain(tutor.model_stream(system, messages, "claude-opus-5-5", "medium"))
    assert text == ["Hel", "lo"] and asked[-1] == "closed"
    assert usage == {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 3,
                     "cache_write_tokens": 7, "stop_reason": "end_turn", "model": "claude-opus-5-5"}
    kind, kw = asked[0]
    assert kind == "beta" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default" and kw["output_config"] == {"effort": "medium"}
    assert kw["max_tokens"] == 16000 and kw["system"] is system
    assert "thinking" not in kw and "temperature" not in kw

    asked.clear()
    monkeypatch.setattr(anthropic, "Anthropic", lambda: client(False))
    text, usage = drain(tutor.model_stream(system, messages, "claude-opus-5-5", "high"))
    assert [a[0] for a in asked[:2]] == ["beta", "plain"] and text == ["Hel", "lo"]
    assert "fallbacks" not in asked[1][1] and asked[1][1]["output_config"] == {"effort": "high"}
