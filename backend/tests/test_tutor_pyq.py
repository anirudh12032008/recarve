"""The PYQ pipeline: past papers in, questions with answers out.

What matters here is that every route is re-runnable. The archive is a few
hundred PDFs and a run over them costs real money, so a run that dies at
paper 140 has to resume at 141 -- not re-pay for 1 to 140, and not land the
same question twice because a person later typed it in by hand with a
different line break. The model itself is a fake: these tests are about
what the pipeline does with a reply, not about the reply.
"""

import json
import pathlib
import re
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import notes  # noqa: E402
import tutor_pyq  # noqa: E402
from conftest import as_admin_connection  # noqa: E402
from tutor_pyq import fingerprint, import_items, map_topic  # noqa: E402

MODEL = "claude-opus-5-5"


# ---------------------------------------------------------------------------
# A fake Anthropic client and a real (tiny) PDF.

def reply(text, stop="end_turn", inp=1000, out=500):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=inp, output_tokens=out),
        stop_reason=stop,
    )


class FakeClient:
    """`.beta.messages.create` answers with `respond(kwargs)`; every call is kept."""

    def __init__(self, respond, beta_raises=None):
        self.calls, self.plain_calls = [], []
        self.respond, self.beta_raises = respond, beta_raises
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta))
        self.messages = SimpleNamespace(create=self._plain)

    def _beta(self, **kw):
        if self.beta_raises:
            raise self.beta_raises
        self.calls.append(kw)
        return self.respond(kw)

    def _plain(self, **kw):
        self.plain_calls.append(kw)
        return self.respond(kw)


def write_pdf(path):
    from pypdf import PdfWriter

    path.parent.mkdir(parents=True, exist_ok=True)
    w = PdfWriter()
    w.add_blank_page(72, 72)
    with open(path, "wb") as f:
        w.write(f)
    return path


def shelve(conn, code="CY1107", key="library/archive/CY1107/End Term/End Term 2024-25.pdf",
           title="End Term 2024-25", exam="end", year=2024, section=None, kind="paper"):
    as_admin_connection(conn)
    return str(conn.execute(
        "insert into archive_documents (subject_code, kind, exam, year, set_for_section, "
        " title, file_key, size_bytes, source) values (%s,%s,%s,%s,%s,%s,%s,%s,'test') "
        "returning id", (code, kind, exam, year, section, title, key, 1234)).fetchone()[0])


def rows(conn, code="CY1107"):
    return conn.execute(
        "select qno, marks, unit, topic, question_md, answer_md, document_id::text, "
        "       exam, year, set_for_section, extracted_by, answer_model "
        "  from pyq_questions where subject_code = %s order by qno", (code,)).fetchall()


Q = dict(code="CY1107", question_md="Define hardness of water. Why is it expressed "
                                     "in terms of $CaCO_3$ equivalent?", marks=2)


# ---------------------------------------------------------------------------
# Identity.

def test_fingerprint_ignores_case_and_whitespace_only():
    a = fingerprint("CY1107", "Define  hardness\nof water.")
    assert a == fingerprint("cy1107", "  define hardness of WATER. ")
    assert re.fullmatch(r"[0-9a-f]{32}", a)
    # Another subject is another question; so is another number in a numerical.
    assert a != fingerprint("CY1110", "Define hardness of water.")
    assert fingerprint("CY1107", "Find the GCV for 2 g of coal") != \
        fingerprint("CY1107", "Find the GCV for 3 g of coal")


# ---------------------------------------------------------------------------
# Placement.

def test_map_topic_exact_is_case_insensitive_and_returns_the_syllabus_names():
    assert map_topic("cy1107", "FUELS", "proximate analysis of coal") == \
        ("Fuels", "Proximate analysis of coal")


def test_map_topic_fuzzy():
    assert map_topic("CY1107", "water", "lime soda process") == \
        ("Water", "Lime-soda process (hot and cold)")
    assert map_topic("CY1107", "Lubricant", "Mechanism of lubrication") == \
        ("Lubricants", "Mechanisms of lubrication: thick layer, thin layer, extreme pressure")
    assert map_topic("MC1101", "calculus", "Taylor series") == ("Calculus", "Taylor's series")


def test_map_topic_the_topic_wins_over_a_wrong_unit():
    assert map_topic("CY1107", "Water", "Proximate analysis of coal") == \
        ("Fuels", "Proximate analysis of coal")


def test_map_topic_says_none_rather_than_guess():
    assert map_topic("CY1107", None, "quantum chromodynamics") == (None, None)
    # One shared word ("theorem") is not a match, however alike the letters.
    assert map_topic("MC1101", "Calculus", "Rolles theorem") == ("Calculus", None)
    # Two topics equally good is no answer either.
    assert map_topic("CY1107", "Fuels", "analysis of coal")[1] is None
    # A unit name in the topic box is the unit.
    assert map_topic("CY1107", None, "Fuels") == ("Fuels", None)
    assert map_topic("XX9999", "Fuels", "Coal") == (None, None)


# ---------------------------------------------------------------------------
# Import.

def test_import_inserts_maps_and_is_idempotent(db):
    items = [
        dict(Q, exam="end", year=2024, unit="water", topic="hardness classification"),
        dict(code="CY1107", question_md="Explain the zeolite process.", marks=5,
             qno="3(b)", topic="zeolite"),
        dict(code="CY1107", question_md="Write a note on something off-syllabus.",
             topic="astrology"),
    ]
    res = import_items(db, items, extracted_by="tester")
    assert res == {"inserted": 3, "skipped": 0, "mapped": 2, "errors": []}
    got = {r[4]: r for r in rows(db)}
    hard = got[Q["question_md"]]
    assert hard[2:4] == ("Water", "Hardness: classification and disadvantages")
    assert hard[7:9] == ("end", 2024) and hard[10] == "tester" and float(hard[1]) == 2
    assert got["Explain the zeolite process."][2:4] == ("Water", "Zeolite process")
    assert got["Write a note on something off-syllabus."][2:4] == (None, None)

    # The same three again, one retyped with other spacing and case: nothing new.
    again = [dict(items[0], question_md="  define HARDNESS of water.  Why is it "
                                        "expressed in terms of $caco_3$ equivalent?"),
             *items[1:]]
    assert import_items(db, again) == {"inserted": 0, "skipped": 3, "mapped": 0, "errors": []}
    assert len(rows(db)) == 3


def test_the_same_question_in_another_year_is_another_row(db):
    """"Asked in three of the last four end terms" is the fact the tutor is
    for. A question stored once under its first paper would hide it, so the
    sitting is part of the fingerprint -- and the same sitting read twice is
    still one row."""
    base = dict(code="CY1107", question_md="Define hardness of water.", marks=2)
    first = [dict(base, exam="end", year=2023), dict(base, exam="end", year=2024),
             dict(base, exam="mid", year=2024)]
    assert import_items(db, first)["inserted"] == 3
    # Two readings of the 2024 end term, one of them retyped: still three rows.
    again = [dict(base, exam="end", year=2024, question_md="  define HARDNESS of water. ")]
    assert import_items(db, again) == {"inserted": 0, "skipped": 1, "mapped": 0, "errors": []}
    years = sorted((r[7], r[8]) for r in rows(db))
    assert years == [("end", 2023), ("end", 2024), ("mid", 2024)]
    # With no sitting named at all, the old rule holds: the question lands once.
    assert import_items(db, [base, base])["inserted"] == 1
    assert fingerprint("CY1107", "x") == fingerprint("cy1107", " X ")
    assert fingerprint("CY1107", "x", "end|2024|") != fingerprint("CY1107", "x", "end|2023|")


def test_import_fills_a_missing_answer_and_never_overwrites_one(db):
    import_items(db, [Q])
    import_items(db, [dict(Q, answer_md="Hardness is the soap-destroying ...",
                           extracted_by="claude-code")])
    (r,) = rows(db)
    assert r[5] == "Hardness is the soap-destroying ..." and r[11] == "claude-code"

    import_items(db, [dict(Q, answer_md="A worse answer")])
    assert rows(db)[0][5] == "Hardness is the soap-destroying ..."


def test_import_refuses_bad_items_one_at_a_time(db):
    res = import_items(db, [
        dict(Q, code="XX9999"),
        dict(Q, question_md="   "),
        dict(Q, exam="final"),
        dict(Q, year="2024-25"),
        dict(Q, year=1999),
        dict(Q, marks="five"),
        dict(Q, section="Q"),
        "not an object",
        dict(Q, exam="END", year="2023", section="b"),  # this one is fine
    ])
    assert res["inserted"] == 1 and res["skipped"] == 0
    assert len(res["errors"]) == 8
    assert "XX9999" in res["errors"][0] and res["errors"][0].startswith("item 1")
    assert rows(db)[0][7:10] == ("end", 2023, "B")
    assert import_items(db, {"items": []})["errors"] == ["items must be a list"]


def test_import_links_the_archive_document_by_path_or_title(db):
    doc = shelve(db, section="C")
    res = import_items(db, [
        dict(Q, path="library/archive/CY1107/End Term/End Term 2024-25.pdf"),
        # The same paper named without the library/ prefix, and by its title.
        dict(code="CY1107", question_md="Q two", path="archive/CY1107/End Term/End Term 2024-25.pdf"),
        dict(code="CY1107", question_md="Q three", document_title="end term 2024-25"),
        dict(code="CY1107", question_md="Q four", path="library/archive/CY1107/nope.pdf"),
        dict(code="CY1107", question_md="Q five", document_title="No Such Paper"),
    ])
    assert res["inserted"] == 3
    assert len(res["errors"]) == 2 and "nope.pdf" in res["errors"][0]
    for r in rows(db):
        # Linked, and the sitting inherited from the paper's own row.
        assert r[6] == doc and r[7:10] == ("end", 2024, "C")
    # An item that says its own year keeps it.
    import_items(db, [dict(code="CY1107", question_md="Q six", year=2023,
                           path="library/archive/CY1107/End Term/End Term 2024-25.pdf")])
    assert [r[8] for r in rows(db) if r[4] == "Q six"] == [2023]


# ---------------------------------------------------------------------------
# Extract.

EXTRACTED = [
    {"qno": "1(a)", "marks": 2, "question_md": "Define octane number.",
     "unit": "Fuels", "topic": "Knocking, octane and cetane number"},
    {"qno": "1(b)", "marks": 5, "question_md": "Calculate the GCV: $C = 80\\%$, $H = 5\\%$.",
     "unit": "fuels", "topic": "numericals on calorific value"},
    {"qno": "1(b) OR", "marks": 5, "question_md": "Explain the bomb calorimeter.",
     "unit": None, "topic": None},
]


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "library"
    write_pdf(lib / "archive" / "CY1107" / "End Term" / "End Term 2024-25.pdf")
    return lib


def test_extract_reads_a_paper_once_and_files_its_questions(db, library):
    doc = shelve(db)
    fake = FakeClient(lambda kw: reply("```json\n" + json.dumps(EXTRACTED) + "\n```"))
    logs = []
    s = tutor_pyq.extract(db, library, code="CY1107", client=fake, log=logs.append)
    assert s["papers"] == 1 and s["questions"] == 3

    (call,) = fake.calls
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    assert call["model"] == MODEL and call["max_tokens"] == 16000
    assert "temperature" not in call and "thinking" not in call
    block = call["messages"][0]["content"][0]
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf" and block["source"]["data"]
    assert "Unit: Fuels" in call["system"] and "Proximate analysis of coal" in call["system"]

    got = {r[0]: r for r in rows(db)}
    assert set(got) == {"1(a)", "1(b)", "1(b) OR"}
    assert all(r[6] == doc and r[7:9] == ("end", 2024) and r[10] == MODEL
               for r in got.values())
    assert got["1(b)"][2:4] == ("Fuels", "Numericals on calorific value and ultimate analysis")
    assert got["1(b) OR"][2:4] == (None, None)
    # $ in, $ out: 1000 in at $4/M and 500 out at $20/M.
    assert any("$0.014" in line for line in logs)

    # Resumable by construction: the paper has questions, so it is not read again.
    tutor_pyq.extract(db, library, code="CY1107", client=fake, log=logs.append)
    assert len(fake.calls) == 1


def test_extract_skips_a_second_copy_of_the_same_file(db, library):
    shelve(db)
    src = library / "archive" / "CY1107" / "End Term" / "End Term 2024-25.pdf"
    copy = library / "archive" / "CY1107" / "PYQs" / "End Term 2024-25.pdf"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(src.read_bytes())
    shelve(db, key="library/archive/CY1107/PYQs/End Term 2024-25.pdf")
    fake = FakeClient(lambda kw: reply(json.dumps(EXTRACTED)))
    logs = []
    tutor_pyq.extract(db, library, code="CY1107", client=fake, log=logs.append)
    tutor_pyq.extract(db, library, code="CY1107", client=fake, log=logs.append)
    assert len(fake.calls) == 1
    assert any("same file" in line for line in logs)


def test_extract_stops_at_max_cost(db, library):
    shelve(db)
    write_pdf(library / "archive" / "CY1107" / "Mid Term" / "Mid Term 2023.pdf")
    shelve(db, key="library/archive/CY1107/Mid Term/Mid Term 2023.pdf",
           title="Mid Term 2023", exam="mid", year=2023)
    # Different questions per paper, so the second is not a duplicate of the first.
    n = iter(range(100))
    fake = FakeClient(lambda kw: reply(json.dumps(
        [{"qno": "1", "marks": 2, "question_md": f"Question {next(n)}"}])))
    logs = []
    s = tutor_pyq.extract(db, library, code="CY1107", client=fake, log=logs.append,
                          max_cost=0.001)
    assert len(fake.calls) == 1 and s["stopped"]
    assert any(line.startswith("stopping") for line in logs)


def test_extract_falls_back_without_the_beta_and_skips_big_files(db, library, monkeypatch):
    shelve(db)
    fake = FakeClient(lambda kw: reply(json.dumps(EXTRACTED[:1])), beta_raises=TypeError("fallbacks"))
    monkeypatch.setattr(tutor_pyq, "MAX_PAPER_BYTES", 10)
    logs = []
    tutor_pyq.extract(db, library, code="CY1107", client=fake, log=logs.append)
    assert fake.plain_calls == [] and any("MB is over" in line for line in logs)

    monkeypatch.setattr(tutor_pyq, "MAX_PAPER_BYTES", 30 * 1024 * 1024)
    tutor_pyq.extract(db, library, code="CY1107", client=fake, log=logs.append)
    assert len(fake.plain_calls) == 1
    assert "betas" not in fake.plain_calls[0] and "fallbacks" not in fake.plain_calls[0]
    assert len(rows(db)) == 1


def test_extract_salvages_a_truncated_reply_and_repairs_latex(db, library):
    shelve(db)
    # A model that forgot to double its backslashes, and ran out of tokens.
    raw = ('[{"qno": "1", "marks": 2, "question_md": "Show that $\\frac{1}{2} \\theta$ '
           '$\\alpha$"}, {"qno": "2", "marks": 5, "question_md": "cut off mid')
    fake = FakeClient(lambda kw: reply(raw, stop="max_tokens"))
    logs = []
    tutor_pyq.extract(db, library, code="CY1107", client=fake, log=logs.append)
    (r,) = rows(db)
    assert r[4] == "Show that $\\frac{1}{2} \\theta$ $\\alpha$"
    assert any("TRUNCATED" in line for line in logs)


# ---------------------------------------------------------------------------
# Answer.

def answering(kw):
    text = kw["messages"][0]["content"][0]["text"]
    ids = re.findall(r"### id: (\S+)", text)
    return reply(json.dumps([{"id": i, "answer_md": f"Answer to {i}: $\\frac{{a}}{{b}}$"}
                             for i in ids]))


def test_answer_fills_unanswered_questions_in_batches_with_the_pack(db, tmp_path, monkeypatch):
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / "CY1107.json").write_text(json.dumps({"code": "CY1107", "units": [
        {"unit": "Fuels", "topics": [{"topic": "Knocking, octane and cetane number",
                                      "gist": "GIST-MARKER", "questions": [{"q": "QUIZ"}]}]}]}))
    monkeypatch.setattr(tutor_pyq, "PACKS", packs)
    items = [dict(code="CY1107", question_md=f"Question {i}", marks=2,
                  unit="Fuels", topic="Knocking, octane and cetane number") for i in range(9)]
    items.append(dict(code="CY1107", question_md="Already answered", answer_md="kept"))
    import_items(db, items)

    fake = FakeClient(answering)
    s = tutor_pyq.answer(db, code="CY1107", client=fake, log=lambda *_: None)
    assert s["answered"] == 9 and len(fake.calls) == 2  # 8 + 1
    first = fake.calls[0]["messages"][0]["content"][0]["text"]
    assert "GIST-MARKER" in first and "QUIZ" not in first and "Already answered" not in first
    assert "full marks" in fake.calls[0]["system"]
    assert fake.calls[0]["max_tokens"] == 16000 and "temperature" not in fake.calls[0]

    got = {r[4]: r for r in rows(db)}
    assert got["Already answered"][5] == "kept"
    q0 = got["Question 0"]
    assert q0[5].endswith("$\\frac{a}{b}$") and q0[11] == MODEL

    # Nothing left to answer, so nothing is sent.
    tutor_pyq.answer(db, code="CY1107", client=fake, log=lambda *_: None)
    assert len(fake.calls) == 2


def test_answer_without_a_pack_and_with_a_reply_that_skips_one(db, tmp_path, monkeypatch):
    monkeypatch.setattr(tutor_pyq, "PACKS", tmp_path / "no-packs-here")
    import_items(db, [dict(code="BS1111", question_md="What is a cell?"),
                      dict(code="BS1111", question_md="What is DNA?")])

    def half(kw):
        ids = re.findall(r"### id: (\S+)", kw["messages"][0]["content"][0]["text"])
        return reply(json.dumps([{"id": ids[0], "answer_md": "The unit of life."},
                                 {"id": "not-one-of-ours", "answer_md": "ignored"}]))

    s = tutor_pyq.answer(db, code="BS1111", client=FakeClient(half), log=lambda *_: None)
    assert s["answered"] == 1
    assert sorted(r[5] is None for r in rows(db, "BS1111")) == [False, True]


# ---------------------------------------------------------------------------
# Status and the command line.

def test_status_counts_per_subject(db, library):
    before = {r["code"]: r for r in tutor_pyq.status(db)}
    doc = shelve(db)
    shelve(db, key="library/archive/CY1107/x.pdf", title="x")
    import_items(db, [dict(Q, document_id=doc, answer_md="yes", topic="hardness"),
                      dict(code="CY1107", question_md="Unplaced", document_id=doc),
                      dict(code="BS1111", question_md="Loose question")])
    after = {r["code"]: r for r in tutor_pyq.status(db)}

    def delta(code, k):
        return after[code][k] - before.get(code, {}).get(k, 0)

    assert [delta("CY1107", k) for k in
            ("papers", "extracted", "questions", "answered", "mapped")] == [2, 1, 2, 1, 1]
    assert delta("BS1111", "questions") == 1 and delta("BS1111", "papers") == 0


class Borrowed:
    """The test's own connection, handed to cli() in place of notes.db(), so
    the fixture's rollback still undoes what the command wrote."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, k):
        return getattr(self._conn, k)

    def close(self):
        pass


def test_cli_import_and_status(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(notes, "db", lambda *a: Borrowed(db))
    f = tmp_path / "CY1107.json"
    f.write_text(json.dumps({"items": [Q, dict(Q, code="nope")]}))
    with pytest.raises(SystemExit) as e:  # one refused item is a non-zero exit
        tutor_pyq.cli(["pyq", "import", str(f), "--by", "claude-code"], tmp_path)
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "1 inserted" in out and "unknown subject" in out
    assert rows(db)[0][10] == "claude-code"

    f.write_text(json.dumps([Q]))
    tutor_pyq.cli(["pyq", "import", str(f)], tmp_path)
    assert "0 inserted, 1 already there" in capsys.readouterr().out

    tutor_pyq.cli(["pyq", "status"], tmp_path)
    assert re.search(r"CY1107\s+\d+\s+\d+\s+[1-9]", capsys.readouterr().out)


def test_cli_says_how_when_argv_is_wrong(tmp_path, monkeypatch):
    for argv in ([], ["pyq", "bogus"], ["pyq", "import"], ["pyq", "extract", "--limit", "x"]):
        with pytest.raises(SystemExit) as e:
            tutor_pyq.cli(argv, tmp_path)
        assert "usage: notes.py tutor pyq" in str(e.value.code)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit) as e:
        tutor_pyq.cli(["pyq", "extract"], tmp_path)
    assert "ANTHROPIC_API_KEY" in str(e.value.code)
