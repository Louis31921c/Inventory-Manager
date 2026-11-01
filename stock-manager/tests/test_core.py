import asyncio
import json
import pathlib
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from inventory import audit, db, llm, quotes, vocabulary
from inventory.config import Config, MailConfig, SmsConfig
from inventory.models import Line, Note, parse_date


def make_note(**kw):
    base = dict(
        supplier="Acme Supplies",
        delivery_date="2026-09-21",
        order_date="2026-09-15",
        site="Villa Project",
        work_item="Ground slab",
        lines=[Line(article="Cement 35kg", quantity=40, designation="CIM35 - Cement CEM II 35kg"),
               Line(article="Mesh ST25", backorder=True)],
    )
    base.update(kw)
    return Note(**base)


def make_config(tmp_path, **kw):
    sms = kw.pop("sms", None) or SmsConfig(provider="none", to="", sender="", account_sid="",
                                           auth_token="", webhook_url="", webhook_token="",
                                           max_per_hour=6)
    mail = kw.pop("mail", None) or MailConfig(host="", port=993, user="", password="", folder="INBOX",
                      security="ssl", insecure=False, mark_read=True, every_minutes=15)
    base = dict(backend="claude", gemini_api_key="", openai_api_key="", photo_retention_days=42,
                data_dir=tmp_path, model="", user="ALEX", sms=sms, mail=mail)
    base.update(kw)
    return Config(**base)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-09-21", date(2026, 9, 21)),
        ("21/09/2026", date(2026, 9, 21)),
        ("03/10/2026", date(2026, 10, 3)),
        ("3.10.26", date(2026, 10, 3)),
        ("31/02/2026", None),
        ("yesterday", None),
        (None, None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_problems():
    assert make_note().problems() == []
    bad = make_note(supplier=" ", delivery_date="32/13/2026", lines=[Line(article=None)])
    assert bad.problems() == ["supplier missing",
                              "delivery date unreadable: '32/13/2026'",
                              "article 1 empty"]
    assert "no article" in make_note(lines=[]).problems()
    assert "delivery date missing" in make_note(delivery_date=None).problems()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.duckdb"
    db.init(path)
    db.init(path)
    return path


def test_save_query_delete(db_path, tmp_path):
    note_id = db.save_note(db_path, make_note(), "abc", "img.jpg", "ALEX")
    assert db.find_by_image(db_path, "abc") == note_id
    assert db.describe_note(db_path, note_id) == ("ACME SUPPLIES", 2)

    r = db.run_readonly(
        db_path, "SELECT article, weekday, backorder, order_date FROM inventory_lines ORDER BY article")
    assert r.columns == ["article", "weekday", "backorder", "order_date"]
    assert r.rows == [("CEMENT 35KG", "Monday", False, date(2026, 9, 15)),
                      ("MESH ST25", "Monday", True, date(2026, 9, 15))]
    assert db.run_readonly(db_path, "SELECT confirmed_by FROM notes").rows == [("ALEX",)]

    csv_path, parquet_path = db.export(db_path, tmp_path / "out")
    csv_lines = csv_path.read_text().splitlines()
    assert csv_lines[0] == ("note_id;line_no;article;quantity;supplier;delivery_date;weekday;"
                            "order_date;site;work_item;backorder;designation")
    assert ";CEMENT 35KG;40.0;ACME SUPPLIES;2026-09-21;Monday;" in csv_lines[1]
    assert "CIM35 - Cement" in csv_lines[1]
    assert parquet_path.stat().st_size > 0

    assert db.delete_note(db_path, note_id) == "img.jpg"
    assert db.run_readonly(db_path, "SELECT count(*) FROM inventory_lines").rows == [(0,)]
    assert db.delete_note(db_path, note_id) is None


def test_duplicate_image_rejected(db_path):
    db.save_note(db_path, make_note(), "abc", "img.jpg")
    with pytest.raises(Exception):
        db.save_note(db_path, make_note(), "abc", "img.jpg")
    assert db.run_readonly(db_path, "SELECT count(*) FROM inventory_lines").rows == [(2,)]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM inventory_lines",
        "DROP TABLE inventory_lines",
        "COPY inventory_lines TO '/tmp/x.csv'",
        "SELECT 1; SELECT 2",
        "SELECT * FROM read_csv('/etc/passwd')",
        "ATTACH '/tmp/other.duckdb'",
        "SELECT * FROM nope",
    ],
)
def test_readonly_guard(db_path, sql):
    with pytest.raises(db.QueryError):
        db.run_readonly(db_path, sql)


def test_readonly_truncation(db_path):
    r = db.run_readonly(db_path, "SELECT * FROM range(20)", max_rows=5)
    assert len(r.rows) == 5 and r.truncated


class FakeGenai:
    """Stands in for genai.Client, returning canned structured-output text."""

    def __init__(self, *payloads, status="completed", error=None):
        self.payloads, self.calls = list(payloads), []
        self.status, self.error = status, error
        self.aio = SimpleNamespace(interactions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(status=self.status, output_text=json.dumps(self.payloads.pop(0)))


def gemini(*payloads, **kw):
    backend = llm.GeminiBackend.__new__(llm.GeminiBackend)
    backend.model = "gemini-3.8-flash"
    backend.client = FakeGenai(*payloads, **kw)
    return backend


EMPTY_NOTE = {"supplier": None, "delivery_date": None, "order_date": None,
              "site": None, "work_item": None, "lines": []}


async def test_gemini_extract():
    b = gemini(dict(EMPTY_NOTE, supplier="Acme", lines=[{"article": "Cement", "backorder": False}]))
    note = await b.extract(b"\xff\xd8", "image/jpeg")
    assert note.supplier == "Acme" and note.lines[0].article == "Cement"
    call = b.client.calls[0]
    assert call["input"][0]["type"] == "image" and call["input"][0]["mime_type"] == "image/jpeg"
    assert call["response_format"]["schema"] == llm.EXTRACT_SCHEMA and call["store"] is False

    b = gemini(EMPTY_NOTE)
    await b.extract(b"%PDF", "application/pdf")
    assert b.client.calls[0]["input"][0]["type"] == "document"


async def test_gemini_failures():
    with pytest.raises(llm.ExtractionError, match="Incomplete"):
        await gemini({}, status="failed").extract(b"", "image/jpeg")
    with pytest.raises(llm.ExtractionError, match="quota"):
        await gemini(error=RuntimeError("429 RESOURCE_EXHAUSTED")).extract(b"", "image/jpeg")
    with pytest.raises(llm.ExtractionError, match="Unreadable"):
        await gemini({"lines": "nope"}).extract(b"", "image/jpeg")


async def test_gemini_sql_retry_prompt():
    b = gemini({"sql": "SELECT 1", "note": None})
    assert await b.to_sql("how many?", previous=("SELEC 1", "syntax error")) == ("SELECT 1", None)
    call = b.client.calls[0]
    assert "syntax error" in call["input"] and "SELEC 1" in call["input"]
    assert "DuckDB" in call["system_instruction"]


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
log = {"args": args, "prompt": prompt, "cwd": os.getcwd(), "files": sorted(os.listdir("."))}
open(os.environ["FAKE_CLAUDE_LOG"], "w").write(json.dumps(log))
print(os.environ["FAKE_CLAUDE_OUT"])
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(0o755)
    log = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    def respond(payload):
        monkeypatch.setenv("FAKE_CLAUDE_OUT", json.dumps(payload))
        return llm.ClaudeCodeBackend(binary=str(binary)), log

    return respond


def ok_result(output):
    return {"type": "result", "subtype": "success", "is_error": False, "structured_output": output}


async def test_claude_extract(fake_claude):
    out = dict(EMPTY_NOTE, supplier="Acme", lines=[{"article": "Cement", "backorder": True}])
    backend, log = fake_claude(ok_result(out))
    note = await backend.extract(b"\xff\xd8", "image/jpeg")
    assert note.lines[0].backorder
    seen = json.loads(log.read_text())
    assert seen["files"] == ["note.jpg"] and "./note.jpg" in seen["prompt"]
    args = seen["args"]
    assert args[args.index("--tools") + 1] == "Read" and args[args.index("--allowedTools") + 1] == "Read"
    assert json.loads(args[args.index("--json-schema") + 1]) == llm.EXTRACT_SCHEMA
    assert not pathlib.Path(seen["cwd"]).exists()


async def test_claude_sql(fake_claude):
    backend, log = fake_claude(ok_result({"sql": "SELECT 1", "note": None}))
    assert await backend.to_sql("how many?") == ("SELECT 1", None)
    args = json.loads(log.read_text())["args"]
    assert args[args.index("--tools") + 1] == ""
    assert "DuckDB" in args[args.index("--system-prompt") + 1]


async def test_claude_failures(fake_claude, tmp_path):
    backend, _ = fake_claude({"type": "result", "subtype": "success", "is_error": True,
                              "result": "usage limit reached"})
    with pytest.raises(llm.ExtractionError, match="limit"):
        await backend.to_sql("x")
    backend, _ = fake_claude({"type": "result", "subtype": "error_max_turns", "is_error": False})
    with pytest.raises(llm.ExtractionError, match="error_max_turns"):
        await backend.to_sql("x")
    with pytest.raises(llm.ExtractionError, match="not found"):
        await llm.ClaudeCodeBackend(binary=str(tmp_path / "nope")).to_sql("x")


async def test_shared_vocabulary_reaches_both_prompts(fake_claude):
    backend, log = fake_claude(ok_result(dict(EMPTY_NOTE, warnings=[])))
    await backend.extract(b"\xff\xd8", "image/jpeg", ["HEX BOLT", "WASHER M6"])
    prompt = json.loads(log.read_text())["prompt"]
    assert "<known_articles>" in prompt and "- HEX BOLT\n- WASHER M6" in prompt
    assert "<house_rules>" in prompt

    backend, log = fake_claude(ok_result({"list_date": None, "site": None, "work_item": None,
                                          "drafter": None, "lines": [], "warnings": []}))
    await backend.extract_list(b"\xff\xd8", "image/jpeg", ["HEX BOLT", "WASHER M6"])
    list_prompt = json.loads(log.read_text())["prompt"]
    assert "- HEX BOLT\n- WASHER M6" in list_prompt


def test_catalog_learns_and_applies(db_path):
    from inventory import catalog

    first = make_note(supplier="Supplier A", lines=[
        Line(article="HEX BOLT", quantity=1200, designation="550110 - HEX BOLT M6X25 DIN933 A2 BOX 200"),
        Line(article="PLATE", quantity=1, designation="Plate without reference"),
    ])
    db.save_note(db_path, first, "a", "a.jpg")
    assert catalog.reference_of("550110 - HEX BOLT M6X25") == "550110"
    assert catalog.reference_of("Plate 450x303 - steel") is None

    later = make_note(supplier="SUPPLIER A ", lines=[
        Line(article="BOLT HEXAGON HEAD", designation="550110 - HEX BOLT M6X25 DIN933 A2 BOX 200"),
        Line(article="BOLT", designation="550110 - Hex bolt M6x25 A2 (new wording)"),
        Line(article="plate", designation="plate  WITHOUT reference"),
        Line(article="NEW THING", designation="999 - Something else"),
    ])
    applied = catalog.apply(db_path, later)
    assert [(l.article, l.known) for l in applied.lines] == [
        ("HEX BOLT", True), ("HEX BOLT", True), ("PLATE", True), ("NEW THING", False)]
    other = catalog.apply(db_path, make_note(supplier="SUPPLIER B", lines=later.lines))
    assert other.lines[0].known is False

    fixed = applied.model_copy(deep=True)
    fixed.lines[0].article = "HEX BOLT A2"
    db.save_note(db_path, fixed, "b", "b.jpg")
    assert catalog.apply(db_path, later).lines[0].article == "HEX BOLT A2"


def test_catalog_backfill(tmp_path):
    import duckdb

    from inventory import catalog

    path = tmp_path / "t.duckdb"
    db.init(path)
    db.save_note(path, make_note(), "a", "a.jpg")
    with duckdb.connect(str(path)) as con:
        con.execute("DELETE FROM catalog")
    db.init(path)
    assert ("CEMENT 35KG", "ACME SUPPLIES") in [
        (r[0], r[1]) for r in db.run_readonly(path, "SELECT article, supplier FROM catalog").rows]


def test_vocabulary_is_shared_between_notes_and_lists(db_path):
    db.save_note(db_path, make_note(lines=[Line(article="hex bolt", quantity=10)]), "s1", "1.jpg")
    db.save_purchase_order(db_path, {"site": "C", "lines": [
        {"article": "HEX BOLT", "quantity": 20}, {"article": "WASHER M6", "quantity": 5}]},
        "l1", "l.jpg")

    entries = {e["article"]: e for e in vocabulary.vocabulary(db_path)}
    assert entries["HEX BOLT"]["inventory"] == 1 and entries["HEX BOLT"]["orders"] == 1
    assert entries["HEX BOLT"]["shared"] is True
    assert entries["HEX BOLT"]["seen"] == 2
    assert entries["WASHER M6"]["shared"] is False
    assert vocabulary.known_articles(db_path)[0] == "HEX BOLT"


def test_vocabulary_merge_rewrites_both_tables_and_sticks(db_path):
    db.save_note(db_path, make_note(lines=[Line(article="BOLT M6", quantity=100)]), "s1", "1.jpg")
    db.save_purchase_order(db_path, {"lines": [{"article": "BOLT M6", "quantity": 7}]}, "l1", "l.jpg")

    result = vocabulary.merge(db_path, "BOLT M6", "hex bolt", "ALEX")
    assert result == {"alias": "BOLT M6", "article": "HEX BOLT", "inventory": 1, "order_lines": 1}
    assert db.run_readonly(db_path, "SELECT DISTINCT article FROM inventory_lines").rows == [("HEX BOLT",)]
    assert db.run_readonly(db_path, "SELECT DISTINCT article FROM purchase_order_lines").rows \
        == [("HEX BOLT",)]
    assert "BOLT M6" not in [e["article"] for e in vocabulary.vocabulary(db_path)]
    assert vocabulary.aliases(db_path)[0] == {
        **vocabulary.aliases(db_path)[0], "alias": "BOLT M6", "article": "HEX BOLT", "created_by": "ALEX"}

    db.save_note(db_path, make_note(lines=[Line(article="Bolt M6", quantity=1)]), "s2", "2.jpg")
    db.save_purchase_order(db_path, {"lines": [{"article": "bolt m6", "quantity": 1}]}, "l2",
                          "l2.jpg")
    assert db.run_readonly(db_path, "SELECT DISTINCT article FROM inventory_lines").rows == [("HEX BOLT",)]
    assert db.run_readonly(db_path, "SELECT DISTINCT article FROM purchase_order_lines").rows \
        == [("HEX BOLT",)]


def test_vocabulary_merge_refuses_nonsense(db_path):
    with pytest.raises(vocabulary.VocabularyError):
        vocabulary.merge(db_path, "BOLT", "bolt")
    with pytest.raises(vocabulary.VocabularyError):
        vocabulary.merge(db_path, "", "BOLT")
    vocabulary.merge(db_path, "VIS", "BOLT")
    with pytest.raises(vocabulary.VocabularyError):
        vocabulary.merge(db_path, "BOLT", "VIS")


@pytest.mark.parametrize("raw,expected", [
    ("alex", "ALEX"), ("  marie claire ", "MARIE CLAIRE"), ("N.T", "N.T"), ("a_b-1", "A_B-1"),
    ("", None), (None, None), ("x" * 40, None), ("bad:name", None), ("<script>", None),
])
def test_clean_name(raw, expected):
    assert audit.clean_name(raw) == expected


def test_audit_records_who_did_what(db_path):
    audit.record(db_path, "alex", "login", None, "password", "10.0.0.1")
    audit.record(db_path, "ALEX", "note.save", "note #1", "ACME · 2 line(s)", "10.0.0.1")
    audit.record(db_path, "marie", "order.tick", "list #3 line 2", "to order", "10.0.0.2")
    audit.record(db_path, None, "export", "inventory.csv", None, "10.0.0.3")

    events = audit.events(db_path, limit=10)
    assert [e["action"] for e in events] == ["export", "order.tick", "note.save", "login"]
    assert events[-1]["user"] == "ALEX" and events[0]["user"] == "?"
    assert audit.events(db_path, user="marie")[0]["target"] == "list #3 line 2"

    people = {u["user"]: u for u in audit.users(db_path)}
    assert people["ALEX"]["notes"] == 1 and people["ALEX"]["logins"] == 1 and people["ALEX"]["events"] == 2
    assert people["MARIE"]["corrections"] == 1
    assert people["ALEX"]["last_seen"] is not None


def test_audit_never_raises(tmp_path):
    audit.record(tmp_path / "missing" / "nope.duckdb", "ALEX", "note.save")


def test_saving_records_the_user(db_path):
    db.save_note(db_path, make_note(), "s", "p.jpg", "ALEX")
    db.save_purchase_order(db_path, {"lines": [{"article": "BOLT"}]}, "l", "l.jpg", "MARIE")
    assert db.run_readonly(db_path, "SELECT confirmed_by FROM notes").rows == [("ALEX",)]
    assert db.purchase_orders(db_path)[0]["created_by"] == "MARIE"


def test_report_numbers_and_stores_without_a_provider(tmp_path):
    from inventory import alerts

    config = make_config(tmp_path)
    db.init(config.db_path)

    first = alerts.report(config, "request", "ValueError: boom", "/api/notes", "ALEX")
    second = alerts.report(config, "backup", "backup failed: no space left")
    assert (first["error_no"], second["error_no"]) == (1, 2)
    assert first["sms_status"] == "disabled"

    stored = alerts.recent(config.db_path)
    assert [e["error_no"] for e in stored] == [2, 1]
    assert stored[1]["description"] == "ValueError: boom"
    assert stored[1]["context"] == "/api/notes" and stored[1]["user"] == "ALEX"
    assert alerts.destination(config.sms) == {"provider": "none", "enabled": False, "to": "",
                                              "max_per_hour": 6}


def webhook_config(tmp_path, **kw):
    sms = SmsConfig(provider="webhook", to="+33600000000", sender="", account_sid="",
                    auth_token="", webhook_url="https://sms.example/send", webhook_token="t",
                    max_per_hour=kw.pop("max_per_hour", 6))
    return make_config(tmp_path, sms=sms)


def test_report_texts_the_number_and_mutes_repeats(tmp_path, monkeypatch):
    from inventory import alerts

    config = webhook_config(tmp_path)
    db.init(config.db_path)
    sent = []

    class FakeResponse:
        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        sent.append({"url": request.full_url, "body": json.loads(request.data),
                     "headers": dict(request.headers)})
        return FakeResponse()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    first = alerts.report(config, "request", "KeyError: 'site'", "/api/notes")
    assert first["sms_status"] == "sent"
    assert sent[0]["url"] == "https://sms.example/send"
    assert sent[0]["body"]["to"] == "+33600000000"
    assert sent[0]["body"]["text"] == "Stock manager error #1: KeyError: 'site'"
    assert sent[0]["headers"]["Authorization"] == "Bearer t"

    again = alerts.report(config, "request", "KeyError: 'site'", "/api/notes")
    assert again["error_no"] == 2 and again["sms_status"].startswith("muted")
    assert len(sent) == 1

    assert alerts.destination(config.sms)["to"] == "+33...000"


def test_report_rate_limits_and_survives_a_dead_gateway(tmp_path, monkeypatch):
    from inventory import alerts

    config = webhook_config(tmp_path, max_per_hour=1)
    db.init(config.db_path)
    monkeypatch.setattr(alerts.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("connection refused")))

    first = alerts.report(config, "request", "first failure")
    assert first["sms_status"].startswith("failed: OSError")

    monkeypatch.setattr(alerts.urllib.request, "urlopen", lambda *a, **k: _ok())
    assert alerts.report(config, "request", "second failure")["sms_status"] == "sent"
    third = alerts.report(config, "request", "third failure")
    assert third["sms_status"] == "muted: 1 messages already sent this hour"


def _ok():
    class R:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    return R()


def test_purchase_orders_save_tick_and_delete(db_path):
    sheet = {
        "list_date": date(2026, 8, 24),
        "site": "Harbour site", "work_item": "Railing block B", "drafter": "N.T",
        "lines": [
            {"article": "BOLT M6X30", "designation": "Bolt M6x30", "reference": None,
             "quantity": 110, "stock": 110, "in_stock": None},
            {"article": "WASHER M6", "designation": "Washers M6", "reference": None,
             "quantity": 220, "stock": None, "in_stock": None},
        ],
    }
    list_id = db.save_purchase_order(db_path, sheet, "sha-list", "photo.jpg")
    assert db.find_order_by_image(db_path, "sha-list") == list_id

    stored = db.purchase_orders(db_path)
    assert len(stored) == 1
    assert stored[0]["site"] == "HARBOUR SITE" and stored[0]["photo"] is True
    assert [l["article"] for l in stored[0]["lines"]] == ["BOLT M6X30", "WASHER M6"]
    assert [l["line_no"] for l in stored[0]["lines"]] == [1, 2]
    assert all(l["in_stock"] is None for l in stored[0]["lines"])

    assert db.set_in_stock(db_path, list_id, 1, True)
    assert db.set_in_stock(db_path, list_id, 2, False)
    assert [l["in_stock"] for l in db.purchase_orders(db_path)[0]["lines"]] == [True, False]

    assert db.set_in_stock(db_path, list_id, 1, None)
    assert db.purchase_orders(db_path)[0]["lines"][0]["in_stock"] is None
    assert not db.set_in_stock(db_path, list_id, 99, True)

    assert db.delete_purchase_order(db_path, list_id) == "photo.jpg"
    assert db.purchase_orders(db_path) == []
    assert db.delete_purchase_order(db_path, list_id) is None


def test_order_image_is_unique(db_path):
    sheet = {"lines": [{"article": "BOLT M6X30"}]}
    db.save_purchase_order(db_path, sheet, "same-photo", "a.jpg")
    with pytest.raises(Exception):
        db.save_purchase_order(db_path, sheet, "same-photo", "b.jpg")
    assert len(db.purchase_orders(db_path)) == 1


def test_work_item_is_editable_per_line(db_path):
    note_id = db.save_note(db_path, make_note(), "sha-wi", "img.jpg")
    assert db.update_work_item(db_path, note_id, 2, "Slab level 2")
    rows = db.run_readonly(
        db_path, "SELECT line_no, article, work_item FROM inventory_lines ORDER BY line_no").rows
    assert rows == [(1, "CEMENT 35KG", "GROUND SLAB"), (2, "MESH ST25", "SLAB LEVEL 2")]
    assert db.update_work_item(db_path, note_id, 1, None)
    assert db.run_readonly(db_path,
                           "SELECT work_item FROM inventory_lines WHERE line_no = 1").rows == [(None,)]
    assert not db.update_work_item(db_path, note_id, 99, "x")


def test_inventory_aggregates_by_article(db_path):
    db.save_note(db_path, make_note(supplier="A", delivery_date="2026-09-01", lines=[
        Line(article="HEX BOLT", quantity=100, designation="d1")]), "s1", "1.jpg")
    db.save_note(db_path, make_note(supplier="B", delivery_date="2026-09-20", site="Site 2",
                                    work_item="Balcony", lines=[
        Line(article="HEX BOLT", quantity=50, designation="d2", backorder=True)]), "s2", "2.jpg")
    stock = {row["article"]: row for row in db.stock_by_article(db_path)}
    bolt = stock["HEX BOLT"]
    assert bolt["quantity"] == 150 and bolt["lines"] == 2 and bolt["backorders"] == 1
    assert bolt["supplier"] == "B" and bolt["site"] == "SITE 2" and bolt["work_item"] == "BALCONY"
    assert bolt["delivery_date"] == "2026-09-20"


def test_exports_cover_every_dataset(db_path, tmp_path):
    db.save_note(db_path, make_note(), "sha-exp", "img.jpg", "ALEX")
    db.save_purchase_order(db_path, {"site": "C", "lines": [{"article": "BOLT M6X30", "quantity": 10}]},
                          "l1", "l.jpg", "ALEX")
    audit.record(db_path, "ALEX", "note.save", "note #1")
    assert set(db.EXPORTS) == {"inventory", "purchase_orders", "bills", "quotes"}
    for dataset in db.EXPORTS:
        columns, rows = db.export_query(db_path, dataset)
        assert columns, dataset
        for suffix in (".csv", ".parquet"):
            out = tmp_path / (dataset + suffix)
            db.export_to_file(db_path, dataset, out)
            assert out.stat().st_size > 0, (dataset, suffix)
    inventory_columns = db.export_query(db_path, "inventory")[0]
    assert inventory_columns[:3] == ["note_id", "line_no", "article"]
    assert db.export_query(db_path, "purchase_orders")[0][:3] == ["list_id", "line_no", "article"]


def test_storage_reports_sizes(db_path, tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "a.jpg").write_bytes(b"x" * 1000)
    info = db.storage(db_path, db_path.parent, images)
    assert info["photo_bytes"] == 1000 and info["photo_count"] == 1
    assert info["database_bytes"] > 0 and info["disk_total_bytes"] > info["disk_free_bytes"]
    assert set(info["rows"]) == set(db.COUNTED_TABLES)


def test_purge_photos_keeps_the_records(db_path, tmp_path):
    import duckdb as ddb

    images = tmp_path / "images"
    images.mkdir()
    old_photo, recent_photo, orphan = images / "old.jpg", images / "recent.jpg", images / "orphan.jpg"
    for photo in (old_photo, recent_photo, orphan):
        photo.write_bytes(b"x" * 500)

    old_id = db.save_note(db_path, make_note(), "sha-old", str(old_photo))
    new_id = db.save_note(db_path, make_note(supplier="Recent"), "sha-new", str(recent_photo))
    list_id = db.save_purchase_order(db_path, {"site": "C", "lines": [{"article": "BOLT"}]},
                                    "sha-l", str(old_photo))
    with ddb.connect(str(db_path)) as con:
        con.execute("UPDATE notes SET confirmed_at = now() - INTERVAL 50 DAY WHERE note_id = ?",
                    [old_id])
        con.execute("UPDATE purchase_orders SET created_at = now() - INTERVAL 50 DAY "
                    "WHERE list_id = ?", [list_id])

    result = db.purge_photos(db_path, images, days=42)
    assert result["notes"] == 1 and result["lists"] == 1
    assert result["files"] == 2 and result["bytes"] == 1000

    assert not old_photo.exists() and not orphan.exists()
    assert recent_photo.exists()

    rows = db.run_readonly(
        db_path, "SELECT note_id, image_sha256, image_path FROM notes ORDER BY note_id").rows
    assert rows == [(old_id, "sha-old", None), (new_id, "sha-new", str(recent_photo))]
    assert db.find_by_image(db_path, "sha-old") == old_id
    assert db.run_readonly(db_path, "SELECT count(*) FROM inventory_lines").rows == [(4,)]
    assert db.purchase_orders(db_path)[0]["photo"] is False

    again = db.purge_photos(db_path, images, days=42)
    assert again["files"] == 0


def test_migrations_run_once_and_are_recorded(tmp_path):
    import duckdb as ddb

    from inventory import migrations

    path = tmp_path / "fresh.duckdb"
    applied = db.init(path)
    assert [v for v, _ in applied] == [v for v, _, _ in migrations.MIGRATIONS]
    assert db.init(path) == []

    with ddb.connect(str(path), read_only=True) as con:
        assert migrations.current_version(con) == max(v for v, _, _ in migrations.MIGRATIONS)
        recorded = con.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert recorded == len(migrations.MIGRATIONS)


def test_migrations_are_idempotent_over_existing_data(tmp_path):
    import duckdb as ddb

    from inventory import migrations

    path = tmp_path / "existing.duckdb"
    db.init(path)
    db.save_note(path, make_note(), "sha", "p.jpg", "ALEX")
    with ddb.connect(str(path)) as con:
        con.execute("DELETE FROM schema_migrations")
        migrations.apply(con)
    assert db.run_readonly(path, "SELECT count(*) FROM inventory_lines").rows == [(2,)]
    assert db.run_readonly(path, "SELECT article FROM inventory_lines ORDER BY line_no").rows[0][0] \
        == "CEMENT 35KG"


def test_backup_round_trip_keeps_every_row(db_path, tmp_path):
    from inventory import backup

    db.save_note(db_path, make_note(), "sha-b", "p.jpg", "ALEX")
    db.save_purchase_order(db_path, {"site": "C", "lines": [{"article": "BOLT M6X30", "quantity": 10}]},
                          "l", "l.jpg")
    audit.record(db_path, "ALEX", "note.save", "note #1")
    before = backup.counts(db_path)
    assert before["inventory_lines"] == 2 and before["purchase_order_lines"] == 1 and before["audit"] == 1

    export_dir = tmp_path / "export"
    backup.export(db_path, export_dir)
    assert (export_dir / "schema.sql").exists()

    restored = tmp_path / "restored.duckdb"
    assert backup.restore(export_dir, restored) == before
    assert db.run_readonly(restored,
                           "SELECT article FROM inventory_lines ORDER BY line_no").rows[0][0] == "CEMENT 35KG"


def test_eval_compares_only_the_fields_given():
    from inventory.eval import _compare

    expected = {"supplier": "ACME", "lines": [{"quantity": 40}]}
    got = {"supplier": "acme ", "site": "ignored", "lines": [{"quantity": 40, "article": "X"}]}
    assert list(_compare(expected, got)) == []

    wrong = {"supplier": "ACME", "lines": [{"quantity": 41}]}
    assert [f for f, _, _ in _compare(wrong, got)] == ["lines[1].quantity"]

    assert list(_compare({"supplier": {"$or": ["ACME LTD", "ACME"]}}, {"supplier": "ACME"})) == []
    assert len(list(_compare({"supplier": {"$or": ["A", "B"]}}, {"supplier": "C"}))) == 1


def test_accuracy_is_measured_from_corrections(db_path):
    import duckdb as ddb

    db.record_reading(db_path, "note", fields=20, corrected=0, lines=5)
    db.record_reading(db_path, "note", fields=20, corrected=4, lines=5)
    db.record_reading(db_path, "list", fields=0, corrected=0, lines=0)

    stats = db.accuracy(db_path)
    assert stats["documents"] == 2 and stats["fields"] == 40 and stats["corrected"] == 4
    assert stats["accuracy"] == 90.0
    assert len(stats["series"]) == 1 and stats["series"][0]["accuracy"] == 90.0

    with ddb.connect(str(db_path)) as con:
        con.execute("UPDATE readings SET measured_at = now() - INTERVAL 200 DAY WHERE corrected = 0")
    recent = db.accuracy(db_path, days=90)
    assert recent["series"][0]["fields"] == 20 and recent["series"][0]["accuracy"] == 80.0
    assert recent["accuracy"] == 90.0


def test_accuracy_is_empty_before_any_reading(db_path):
    stats = db.accuracy(db_path)
    assert stats["series"] == [] and stats["accuracy"] is None and stats["documents"] == 0


def temp_config(tmp_path):
    from inventory.config import Config, MailConfig, SmsConfig

    sms = SmsConfig(provider="none", to="", sender="", account_sid="", auth_token="",
                    webhook_url="", webhook_token="", max_per_hour=6)
    return Config(backend="claude", gemini_api_key="", openai_api_key="",
                  photo_retention_days=42, data_dir=tmp_path, model="", user="ALEX",
                  sms=sms, mail=MailConfig(host="", port=993, user="", password="", folder="INBOX",
                      security="ssl", insecure=False, mark_read=True, every_minutes=15))


def filled(tmp_path):
    config = temp_config(tmp_path)
    db.init(config.db_path)
    db.save_note(config.db_path, make_note(), "sha-tui", "", "ALEX")
    db.save_purchase_order(config.db_path, {"site": "HARBOUR POINT", "lines": [
        {"article": "HEX BOLT", "quantity": 10}, {"article": "WASHER M6", "quantity": 20}]},
        "sha-list", "", "ALEX")
    return config


def test_screen_grid_draws_where_it_is_told():
    from inventory.screen import Screen, columns

    screen = Screen(20, 3)
    screen.put(0, 2, "HELLO", "bright")
    screen.rule(1, 0, 20)
    screen.right(2, 20, "42")
    assert screen.rows_text() == ["  HELLO", "-" * 20, " " * 18 + "42"]
    assert columns([6, -4], ["BOLT", "12"]) == "BOLT      12"
    assert screen.cells[0][2] == ("H", "bright")


def test_render_draws_every_tab(tmp_path):
    from inventory import tui

    app = tui.App(filled(tmp_path))
    app.start()
    for index in range(len(tui.TABS)):
        app.tab = index
        text = str(tui.render(app, 110, 30))
        assert "STOCK MANAGER" in text
        assert "F7=QUIT" in text
        assert f"[{tui.TABS[index]}]" in text
    app.tab = 1
    assert "CEMENT 35KG" in str(tui.render(app, 110, 30))
    app.tab = 3
    assert "HARBOUR POINT" in str(tui.render(app, 110, 30))


def test_search_filter_and_work_item_edit(tmp_path):
    import curses

    from inventory import tui

    app = tui.App(filled(tmp_path))
    app.start()
    app.tab = 1
    app.search = "mesh"
    assert [row["article"] for row in app.filtered()] == ["MESH ST25"]
    app.only_backorders = True
    assert len(app.filtered()) == 1
    app.search = ""
    assert len(app.filtered()) == 1  # only the backorder line survives

    app.only_backorders = False
    app.search_sel = 0
    tui.handle(app, 0, "e")
    assert app.prompt is not None
    assert app.prompt.value == "GROUND SLAB"
    tui.handle(app, 21, None)
    for char in "GARAGE DOOR":
        tui.handle(app, ord(char), char)
    tui.handle(app, curses.KEY_ENTER, None)
    assert app.prompt is None
    assert "GARAGE DOOR" in {row["work_item"] for row in app.rows}


def test_review_keys_edit_and_save_a_note(tmp_path):
    import curses

    from inventory import tui
    from inventory.models import Line, Note

    app = tui.App(temp_config(tmp_path))
    app.start()
    app.draft = Note(supplier="ACME", delivery_date="2026-09-30",
                     lines=[Line(article="HEX BOLT", quantity=100)])
    app.draft_proposed = app.draft.model_copy(deep=True)
    app.draft_sel = 0

    tui.handle(app, 0, "a")
    assert len(app.draft.lines) == 2
    tui.handle(app, curses.KEY_ENTER, None)
    for char in "WASHER M6":
        tui.handle(app, ord(char), char)
    tui.handle(app, curses.KEY_ENTER, None)
    assert app.draft.lines[1].article == "WASHER M6"

    tui.handle(app, 0, "q")
    for char in "25":
        tui.handle(app, ord(char), char)
    tui.handle(app, curses.KEY_ENTER, None)
    assert app.draft.lines[1].quantity == 25

    tui.handle(app, 0, "b")
    assert app.draft.lines[1].backorder is True

    tui.handle(app, 0, "s")
    assert app.draft is None
    rows = db.run_readonly(app.config.db_path,
                           "SELECT article, quantity, backorder FROM inventory_lines ORDER BY line_no")
    assert rows.rows == [("HEX BOLT", 100.0, False), ("WASHER M6", 25.0, True)]
    assert db.accuracy(app.config.db_path)["fields"] > 0


def test_order_ticks_from_the_keyboard(tmp_path):
    import curses

    from inventory import tui

    app = tui.App(filled(tmp_path))
    app.start()
    app.tab = 3
    app.sheet_cursor = app.line_cursor = 0
    tui.handle(app, 0, "s")
    tui.handle(app, curses.KEY_DOWN, None)
    tui.handle(app, 0, "o")
    assert [line["in_stock"] for line in app.sheets[0]["lines"]] == [True, False]
    tui.handle(app, curses.KEY_UP, None)
    tui.handle(app, 0, "c")
    assert app.sheets[0]["lines"][0]["in_stock"] is None


def test_function_keys_switch_tabs_and_export(tmp_path):
    import curses

    from inventory import tui

    app = tui.App(filled(tmp_path))
    app.start()
    tui.handle(app, curses.KEY_F3, None)
    assert app.tab == 1
    tui.handle(app, curses.KEY_F1, None)
    assert app.tab == 2
    tui.handle(app, curses.KEY_F5, None)
    assert (app.config.data_dir / "exports" / "inventory.csv").exists()
    tui.handle(app, curses.KEY_F7, None)
    assert app.running is False


def test_cli_commands(tmp_path, capsys):
    from inventory import cli

    data = str(tmp_path)
    assert cli.main(["--data", data, "--user", "ALEX", "where"]) == 0
    assert "database" in capsys.readouterr().out

    config = cli.ready(cli.load_config(data, "ALEX"))
    db.save_note(config.db_path, make_note(), "sha-cli", "", "ALEX")

    assert cli.main(["--data", data, "search", "cement"]) == 0
    assert "CEMENT 35KG" in capsys.readouterr().out

    assert cli.main(["--data", data, "sql", "SELECT count(*) FROM inventory_lines"]) == 0
    assert "2" in capsys.readouterr().out

    assert cli.main(["--data", data, "--user", "ALEX", "export", "inventory",
                     "--format", "csv"]) == 0
    assert (tmp_path / "exports" / "inventory.csv").exists()

    assert cli.main(["--data", data, "vocab"]) == 0
    assert "CEMENT 35KG" in capsys.readouterr().out
    assert cli.main(["--data", data, "vocab", "--merge", "CEMENT 35KG", "CEMENT"]) == 0

    assert cli.main(["--data", data, "audit", "--users"]) == 0
    assert "ALEX" in capsys.readouterr().out

    assert cli.main(["--data", data, "errors"]) == 0
    assert cli.main(["--data", data, "sql", "DELETE FROM inventory_lines"]) == 1


def test_backup_snapshot_round_trip(tmp_path):
    from inventory import backup, cli

    config = cli.ready(temp_config(tmp_path))
    db.save_note(config.db_path, make_note(), "sha-snap", "", "ALEX")
    archive = backup.snapshot(config, passphrase="")
    assert archive.exists() and archive.name.startswith("stock-")

    import tarfile

    with tarfile.open(archive) as tar:
        tar.extractall(tmp_path / "unpacked", filter="data")
    counts = backup.restore(tmp_path / "unpacked" / "db", tmp_path / "again.duckdb")
    assert counts["inventory_lines"] == 2


def test_account_file_and_password(tmp_path):
    from inventory import account

    path = tmp_path / "account.json"
    assert account.read_account(path) is None
    with pytest.raises(ValueError):
        account.write_account("alex", "short", path)
    with pytest.raises(ValueError):
        account.write_account("bad:name", "longenough", path)

    assert account.write_account("alex", "correct horse", path) == "ALEX"
    stored = account.read_account(path)
    assert stored["user"] == "ALEX" and "$" in stored["hash"]
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert account.check_password("correct horse", stored["hash"])
    assert not account.check_password("wrong", stored["hash"])

    account.change_password("correct horse", "another one", path)
    assert account.check_password("another one", account.read_account(path)["hash"])
    with pytest.raises(ValueError):
        account.change_password("not it", "whatever else", path)


def test_lock_sessions_and_rate_limit(tmp_path):
    from inventory import account

    path = tmp_path / "account.json"
    account.write_account("alex", "correct horse", path)
    lock = account.Lock(tmp_path, path)

    assert lock.ready and lock.user == "ALEX"
    assert lock.unlock("correct horse")
    cookie = lock.new_session()
    assert lock.read_session(cookie) == "ALEX"
    assert lock.read_session(None) is None
    assert lock.read_session(cookie.rpartition(".")[0] + ".deadbeef") is None
    assert lock.read_session(lock.new_session(hours=-1)) is None
    assert account.Lock(tmp_path / "elsewhere", path).read_session(cookie) is None

    for _ in range(account.MAX_ATTEMPTS):
        assert not lock.unlock("wrong")
    with pytest.raises(account.TooManyAttempts):
        lock.unlock("correct horse")


def test_server_locks_everything_until_the_password(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("STOCK_USER", raising=False)

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/api/data").status_code == 401
        assert ">create</button>" in client.get("/unlock").text

        assert client.post("/unlock", data={"user": "alex", "password": "x"},
                           headers={"origin": "http://evil.example"}).status_code == 403
        assert client.post("/unlock", data={"user": "alex", "password": "x"}).status_code == 403

        answer = client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                              "again": "nope"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 200
        assert client.get("/api/data").status_code == 401
        answer = client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                              "again": "correct horse"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 303
        assert client.get("/api/data").json()["user"] == "ALEX"

        assert client.post("/lock", headers=mine, follow_redirects=False).status_code == 303
        assert client.get("/api/data").status_code == 401
        page = client.get("/unlock").text
        assert ">ENTER</button>" in page
        assert "readonly" not in page

        answer = client.post("/unlock", data={"user": "someone else",
                                              "password": "correct horse"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 200
        assert client.get("/api/data").status_code == 401

        answer = client.post("/unlock", data={"user": "alex", "password": "correct horse"},
                             headers=mine, follow_redirects=False)
        assert answer.status_code == 303
        assert client.get("/api/data").status_code == 200
        assert client.post("/lock", headers=mine, follow_redirects=False).status_code == 303

        answer = client.post("/unlock", data={"password": "guess"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 200
        assert client.get("/api/data").status_code == 401
        answer = client.post("/unlock", data={"password": "correct horse"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 303
        assert client.get("/api/data").status_code == 200
        assert client.get("/").status_code == 200


def legacy_database(path):
    import duckdb as ddb

    with ddb.connect(str(path)) as con:
        con.execute("CREATE TABLE bons (bon_id INTEGER, image_sha256 VARCHAR, image_path VARCHAR, "
                    "confirmed_at TIMESTAMP)")
        con.execute("CREATE TABLE livraisons (article VARCHAR, quantite DOUBLE, "
                    "fournisseur VARCHAR, date_livraison DATE, date_commande DATE, "
                    "chantier VARCHAR, ouvrage VARCHAR, reliquat BOOLEAN, designation VARCHAR, "
                    "bon_id INTEGER, ligne_no INTEGER)")
        con.execute("CREATE TABLE listes (liste_id INTEGER, date_liste DATE, chantier VARCHAR, "
                    "ouvrage VARCHAR, dessinateur VARCHAR, image_sha256 VARCHAR, "
                    "image_path VARCHAR, cree_le TIMESTAMP)")
        con.execute("CREATE TABLE liste_lignes (liste_id INTEGER, ligne_no INTEGER, "
                    "article VARCHAR, designation VARCHAR, reference VARCHAR, quantite DOUBLE, "
                    "stock DOUBLE, en_stock BOOLEAN)")
        con.execute("CREATE TABLE catalogue (fournisseur VARCHAR, designation VARCHAR, "
                    "reference VARCHAR, article VARCHAR, vu INTEGER, maj_le TIMESTAMP)")
        con.execute("CREATE TABLE lectures (mesure_id INTEGER, mesure_le TIMESTAMP, "
                    "document VARCHAR, champs INTEGER, corriges INTEGER, lignes INTEGER)")

        con.execute("INSERT INTO bons VALUES (7, 'sha-old', '/server/images/sha-old.jpg', now())")
        con.execute("INSERT INTO livraisons VALUES "
                    "('BOLT M6', 1200, 'NORTHGATE', DATE '2026-09-21', DATE '2026-09-14', "
                    "'HARBOUR POINT', 'PARAPET', false, '550110 - BOLT M6X25', 7, 1), "
                    "('WASHER M6', 400, 'NORTHGATE', DATE '2026-09-21', NULL, 'HARBOUR POINT', NULL, "
                    "true, '550220 - WASHER SERIES M 6', 7, 2)")
        con.execute("INSERT INTO listes VALUES (3, DATE '2026-09-17', 'HARBOUR POINT', 'RAILING', "
                    "'N.T', 'sha-list-old', '/server/images/sha-list-old.jpg', now())")
        con.execute("INSERT INTO liste_lignes VALUES (3, 1, 'BOLT M6X30', 'Bolt M6x30', NULL, 110, "
                    "110, true)")
        con.execute("INSERT INTO catalogue VALUES ('NORTHGATE', '550110 - BOLT M6X25', '550110', "
                    "'BOLT M6', 4, now())")
        con.execute("INSERT INTO lectures VALUES (1, now(), 'bon', 20, 2, 5)")
    return path


def test_sync_brings_the_web_version_over(tmp_path):
    from inventory import sync

    legacy = legacy_database(tmp_path / "inventaire.duckdb")
    config = temp_config(tmp_path / "local")
    db.init(config.db_path)

    assert sync.counts(legacy)["livraisons"] == 2
    before, added = sync.from_file(config, legacy)
    assert added["notes"] == 1 and added["inventory"] == 2
    assert added["orders"] == 1 and added["order_lines"] == 1
    assert added["catalog"] == 1 and added["readings"] == 1

    rows = db.run_readonly(config.db_path, "SELECT article, quantity, supplier, delivery_date, "
                                           "site, work_item, backorder FROM inventory_lines "
                                           "ORDER BY line_no").rows
    assert rows[0] == ("BOLT M6", 1200.0, "NORTHGATE", date(2026, 9, 21), "HARBOUR POINT", "PARAPET",
                       False)
    assert rows[1][6] is True
    sheet = db.purchase_orders(config.db_path)[0]
    assert sheet["site"] == "HARBOUR POINT" and sheet["lines"][0]["in_stock"] is True
    assert sheet["photo"] is False  # the photo stayed on the server
    assert db.accuracy(config.db_path)["fields"] == 20
    assert "BOLT M6" in {entry["article"] for entry in __import__(
        "inventory.vocabulary", fromlist=["vocabulary"]).vocabulary(config.db_path)}

    again = sync.from_file(config, legacy)[1]
    assert again["notes"] == 0 and again["skipped_notes"] == 1
    assert again["orders"] == 0 and again["skipped_orders"] == 1
    assert db.run_readonly(config.db_path, "SELECT count(*) FROM inventory_lines").rows == [(2,)]


def test_sync_refuses_a_file_that_is_not_the_web_database(tmp_path):
    from inventory import sync

    config = temp_config(tmp_path)
    db.init(config.db_path)
    with pytest.raises(sync.SyncError):
        sync.from_file(config, config.db_path)


def test_model_panel_lists_providers(monkeypatch, tmp_path):
    from inventory import models_info

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rows = {row["id"]: row for row in models_info.status("claude")}
    assert set(rows) == {"claude", "gemini", "openai"}
    assert rows["claude"]["current"] is True
    assert rows["openai"]["state"] == "missing"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert {row["id"]: row for row in models_info.status("claude")}["openai"]["state"] == "ready"
    monkeypatch.delenv("OPENAI_API_KEY")

    monkeypatch.setenv("GEMINI_API_KEY", "key")
    gemini = {row["id"]: row for row in models_info.status("gemini")}["gemini"]
    assert gemini["state"] in ("ready", "configured") and gemini["current"] is True

    ok, detail = models_info.probe("openai")
    assert ok is False and "no OpenAI key" in detail

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from inventory import config as config_module

    settings = tmp_path / "stock-manager" / "settings.env"
    config_module.set_setting("LLM_BACKEND", "gemini", settings)
    assert "LLM_BACKEND=gemini" in settings.read_text()
    config_module.set_setting("LLM_BACKEND", "claude", settings)
    assert settings.read_text().count("LLM_BACKEND=") == 1

    with pytest.raises(ValueError):
        models_info.use("openai")
    with pytest.raises(ValueError):
        models_info.use("nonsense")


def planning_database(path):
    from inventory.models import Line, Note

    db.init(path)
    for week, (bolts, sheets) in enumerate([(700, 2), (500, 3), (600, 1)]):
        day = date(2026, 9, 7) + timedelta(days=7 * week)
        note = Note(supplier="NORTHGATE", delivery_date=day.isoformat(), site="PARAPET",
                    lines=[Line(article="BOLT M6", quantity=bolts, designation="BOLT M6 ZINC"),
                           Line(article="SHEET ALU", quantity=sheets)])
        db.save_note(path, note, f"sha-{week}", "", "ALEX")
    return path


def test_forecast_reads_the_shelf_and_the_rate(tmp_path):
    from inventory import forecast

    path = planning_database(tmp_path / "plan.duckdb")
    rows, (first, last, days) = forecast.articles(path, lead=7, service=95, horizon=14)
    bolts = {row["article"]: row for row in rows}["BOLT M6"]

    assert (first, last, days) == (date(2026, 9, 7), date(2026, 9, 21), 15)
    assert bolts["used"] == 1800
    assert bolts["per_day"] == round(1800 / 15, 3)
    assert bolts["lead_demand"] == round(1800 / 15 * 7, 2)
    assert bolts["safety_stock"] > 0
    assert bolts["reorder_point"] > bolts["lead_demand"]
    assert bolts["state"] == "uncounted"
    assert bolts["on_hand"] is None and bolts["cover_days"] is None

    db.save_stock_count(path, "BOLT M6", 300, "ALEX")
    rows, _ = forecast.articles(path, lead=7, service=95, horizon=14)
    bolts = {row["article"]: row for row in rows}["BOLT M6"]
    assert bolts["on_hand"] == 300
    assert bolts["cover_days"] == round(300 / (1800 / 15), 1)
    assert bolts["state"] == "short"
    assert bolts["suggested"] > 0
    assert 0 < bolts["risk"] <= 100

    db.save_stock_count(path, "BOLT M6", 99999, "ALEX")
    rows, _ = forecast.articles(path, lead=7, service=95, horizon=14)
    bolts = {row["article"]: row for row in rows}["BOLT M6"]
    assert bolts["state"] == "ok" and bolts["suggested"] == 0 and bolts["risk"] == 0

    db.save_stock_count(path, "BOLT M6", None, "ALEX")
    assert db.stock_counts(path) == []


def test_forecast_summary_counts_and_series(tmp_path):
    from inventory import forecast

    path = planning_database(tmp_path / "plan.duckdb")
    db.save_stock_count(path, "SHEET ALU", 0, "ALEX")
    answer = forecast.summary(path, lead=5, service=90, horizon=10)

    assert answer["settings"] == {"lead_days": 5, "service_level": 90, "horizon_days": 10}
    assert answer["counts"]["articles"] == 2
    assert answer["counts"]["counted"] == 1
    assert answer["counts"]["short"] == 1
    assert answer["thin"] is True
    assert [week["week"] for week in answer["weeks"]] == ["2026-09-07", "2026-09-14",
                                                          "2026-09-21"]
    assert [week["lines"] for week in answer["weeks"]] == [2, 2, 2]
    assert answer["sites"][0]["site"] == "PARAPET"
    assert len(answer["articles"][0]["weekly"]) == 3
    assert answer["shortlist"] and answer["shortlist"][0]["suggested"] > 0


def test_dots_reads_numbers_not_photos(monkeypatch):
    from inventory import llm

    dots = llm.OpenAIBackend("sk-test", model="test-model")
    with pytest.raises(llm.ExtractionError):
        asyncio.run(dots.extract(b"x", "image/jpeg"))

    sent = {}

    def fake_post(body):
        sent.update(body)
        return {"status": "completed",
                "output": [{"content": [{"type": "output_text",
                                         "text": '{"lines": [{"article": "BOLT M6", '
                                                 '"quantity": 500, "unit": "box", '
                                                 '"supplier": "NORTHGATE", "reference": null, '
                                                 '"reason": "under the reorder point"}], '
                                                 '"note": "one line only"}'}]}]}

    monkeypatch.setattr(dots, "_post", fake_post)
    answer = asyncio.run(dots.draft_order({"articles": [{"article": "BOLT M6"}]}))
    assert answer["lines"][0]["quantity"] == 500
    assert answer["note"] == "one line only"
    assert sent["model"] == "test-model"
    assert sent["text"]["format"]["name"] == "purchase_order"
    assert sent["text"]["format"]["strict"] is True
    assert "purchase order" in sent["input"][0]["content"].lower()

    monkeypatch.setattr(dots, "_post", lambda body: {
        "status": "completed",
        "output": [{"content": [{"type": "refusal", "refusal": "no"}]}]})
    with pytest.raises(llm.ExtractionError):
        asyncio.run(dots.draft_order({}))

    with pytest.raises(llm.ExtractionError):
        asyncio.run(llm.OpenAIBackend("").draft_order({}))


def test_planner_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)
    planning_database(server.config.db_path)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        plan = client.get("/api/forecast?lead=7&service=95&horizon=14").json()
        assert plan["settings"]["lead_days"] == 7
        assert plan["counts"]["articles"] == 2

        clamped = client.get("/api/forecast?lead=0&service=400&horizon=9999").json()
        assert clamped["settings"] == {"lead_days": 1, "service_level": 99, "horizon_days": 180}

        assert client.put("/api/stock-count", json={"article": "BOLT M6", "quantity": 120},
                          headers=mine).status_code == 200
        counted = client.get("/api/forecast").json()["articles"]
        assert {row["article"]: row for row in counted}["BOLT M6"]["on_hand"] == 120
        assert client.put("/api/stock-count", json={"article": "  ", "quantity": 1},
                          headers=mine).status_code == 422

        assert "Predictions" in client.get("/").text
        page = client.get("/").text
        assert 'id="ask-agent"' in page and 'id="table-movements"' in page
        assert "/export/safety-stock.sql" in page

        moves = client.get("/api/movements?period=month").json()
        assert moves["period"] == "month"
        assert [row["label"] for row in moves["rows"]] == ["2026-09"]
        assert moves["rows"][0]["in_quantity"] == 1800 + 6
        assert moves["totals"]["net"] == moves["rows"][0]["net"]
        assert client.get("/api/movements?period=decade").status_code == 422

        week = client.get("/api/movements?period=week").json()
        assert len(week["rows"]) == 3
        half = client.get("/api/movements?period=half").json()
        assert [row["label"] for row in half["rows"]] == ["2026 H2"]

        # Plutus is not called for real here: a draft is a Claude run.
        from inventory import agents

        async def no_agent(planner, model=None):
            raise llm.ExtractionError("Plutus needs the Claude command line.")

        monkeypatch.setattr(agents, "draft_order", no_agent)
        answer = client.post("/api/forecast/draft", json={}, headers=mine)
        assert answer.status_code == 502
        assert "Claude" in answer.json()["detail"]

        async def drafted(planner, model=None):
            return {"lines": [{"article": "BOLT M6", "quantity": 200, "unit": "piece",
                               "supplier": "ACME", "reference": None, "reason": "thin"}],
                    "note": "one line."}

        monkeypatch.setattr(agents, "draft_order", drafted)
        answer = client.post("/api/forecast/draft", json={}, headers=mine).json()
        assert answer["agent"] == agents.PLUTUS
        assert answer["lines"][0]["article"] == "BOLT M6"

        for dataset in ("inventory", "purchase_orders", "safety-stock"):
            for fmt in ("csv", "xlsx", "sql"):
                answer = client.get(f"/export/{dataset}.{fmt}")
                assert answer.status_code == 200, f"{dataset}.{fmt}"
                assert answer.content, f"{dataset}.{fmt} came back empty"
        for gone in ("/export.csv", "/export.parquet", "/export/movements.csv",
                     "/export/ddmrp.csv", "/export/audit.csv", "/export/inventory.parquet"):
            assert client.get(gone).status_code == 404, gone

        assert client.put("/api/models/key", json={"key": "sk-test"},
                          headers=mine).status_code == 404

        actions = [row["action"] for row in client.get("/api/audit").json()["events"]]
        assert "stock.count" in actions


def test_downloads_land_somewhere(tmp_path, monkeypatch):
    from inventory import desktop

    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path / "pick-ups"))
    folder = desktop.downloads_dir()
    assert folder.is_dir()

    first = desktop.free_name(folder, "movements-week.csv")
    first.write_text("one")
    second = desktop.free_name(folder, "movements-week.csv")
    second.write_text("two")
    assert first.name == "movements-week.csv"
    assert second.name == "movements-week-2.csv"
    assert desktop.free_name(folder, "").name == "download"
    assert desktop.free_name(folder, "../../escape.csv").parent == folder


def buffer_database(path):
    from inventory.models import Line, Note

    db.init(path)
    weeks = [(300, 2), (0, 0), (900, 3), (300, 1)]
    for index, (bolts, sheets) in enumerate(weeks):
        day = date(2026, 9, 7) + timedelta(days=7 * index)
        lines = [Line(article="BOLT M6", quantity=bolts)] if bolts else []
        if sheets:
            lines.append(Line(article="SHEET ALU", quantity=sheets))
        if not lines:
            continue
        note = Note(supplier="NORTHGATE", delivery_date=day.isoformat(),
                    order_date=(day - timedelta(days=10)).isoformat(),
                    site="PARAPET", lines=lines)
        db.save_note(path, note, f"buf-{index}", "", "ALEX")
    return path


def test_buffers_size_the_zones_from_the_data(tmp_path):
    from inventory import ddmrp

    path = buffer_database(tmp_path / "buffers.duckdb")
    rows, chain, (first, last, days) = ddmrp.buffers(path, cycle=7, horizon=4)
    bolts = {row["article"]: row for row in rows}["BOLT M6"]

    assert (first, last) == (date(2026, 9, 7), date(2026, 9, 28))
    assert bolts["lead_days"] == 10.0 and bolts["lead_class"] == "medium"
    assert bolts["adu"] == round(1500 / days, 3)
    assert bolts["red"] == round(bolts["red_base"] + bolts["red_safety"], 1)
    assert bolts["yellow"] == round(bolts["adu"] * 10, 1)
    assert bolts["top_of_green"] == round(bolts["red"] + bolts["yellow"] + bolts["green"], 1)
    # Nothing has shipped, so the shelf is what the notes brought in.
    assert bolts["on_hand"] == 1500 and bolts["zone"] == "green"

    db.save_stock_count(path, "BOLT M6", 10, "ALEX")
    rows, _, _ = ddmrp.buffers(path, cycle=7, horizon=4)
    bolts = {row["article"]: row for row in rows}["BOLT M6"]
    assert bolts["zone"] == "red" and bolts["net_flow"] == 10
    assert bolts["to_order"] >= bolts["top_of_green"] - 10

    db.save_stock_count(path, "BOLT M6", 100000, "ALEX")
    rows, _, _ = ddmrp.buffers(path, cycle=7, horizon=4)
    bolts = {row["article"]: row for row in rows}["BOLT M6"]
    assert bolts["zone"] == "over" and bolts["to_order"] == 0


def test_the_chain_walks_between_quiet_and_busy_weeks():
    from inventory import ddmrp

    assert ddmrp.state_of(0, 100) == "quiet"
    assert ddmrp.state_of(20, 100) == "light"
    assert ddmrp.state_of(100, 100) == "usual"
    assert ddmrp.state_of(300, 100) == "spike"
    assert ddmrp.typical_week([0, 100, 0, 200]) == 150

    walk = [["quiet", "usual", "quiet", "usual"]] * 20
    matrix, moves = ddmrp.transition_matrix(walk)
    assert moves == 60
    assert matrix["quiet"]["usual"] > 0.9
    assert all(abs(sum(row.values()) - 1) < 1e-9 for row in matrix.values())

    steady = ddmrp.steady_state(matrix)
    assert abs(sum(steady.values()) - 1) < 1e-6
    assert abs(steady["quiet"] - steady["usual"]) < 0.05

    spiky = ddmrp.transition_matrix([["usual", "spike"] * 10])[0]
    assert ddmrp.risk_of_spike(spiky, "usual", 0) == 0
    assert ddmrp.risk_of_spike(spiky, "usual", 1) > 0.8
    assert ddmrp.risk_of_spike(spiky, "usual", 4) >= ddmrp.risk_of_spike(spiky, "usual", 1)


def test_buffer_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)
    buffer_database(server.config.db_path)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        answer = client.get("/api/ddmrp?cycle=14&horizon=6").json()
        assert answer["settings"] == {"cycle_days": 14, "horizon_weeks": 6}
        assert answer["counts"]["articles"] == 2
        assert set(answer["chain"]["states"]) == {"quiet", "light", "usual", "spike"}
        assert answer["thin"] is True

        clamped = client.get("/api/ddmrp?cycle=0&horizon=99").json()
        assert clamped["settings"] == {"cycle_days": 1, "horizon_weeks": 26}

        sheet = client.get("/export/safety-stock.csv?cycle=14&horizon=6")
        assert sheet.status_code == 200
        assert sheet.headers["content-disposition"].endswith('"safety_stock.csv"')
        assert "top of green" in sheet.text and "BOLT M6" in sheet.text
        assert client.get("/export/safety-stock.xlsx").status_code == 200

        dump = client.get("/export/safety-stock.sql").text
        assert "CREATE SCHEMA IF NOT EXISTS stock;" in dump
        assert "CREATE TABLE IF NOT EXISTS stock.safety_stock" in dump
        assert "REFERENCES stock.articles(article)" in dump
        assert "BEGIN;" in dump and dump.rstrip().endswith("COMMIT;")

        page = client.get("/").text
        assert 'id="table-buffers"' in page and 'id="table-chain"' in page
        assert 'id="panel-ddmrp"' in page


def test_the_sql_dump_loads_into_postgres(tmp_path):
    from inventory import pg

    rows = [(1, 2, "BOLT M6", 10.5, date(2026, 9, 7), True, None),
            (2, 3, "O'HARA PLATE", None, None, False, "back\\slash")]
    columns = (("id", "integer"), ("note_id", "integer"), ("article", "text"),
               ("quantity", "double precision"), ("delivery_date", "date"),
               ("backorder", "boolean"), ("designation", "text"))
    dump = pg.script("inventory_lines", columns, rows, ("id",), "Inventory lines")

    assert dump.startswith("-- Inventory lines")
    assert "CREATE SCHEMA IF NOT EXISTS stock;" in dump
    assert "PRIMARY KEY (id)" in dump
    assert "article text REFERENCES stock.articles(article)" in dump
    assert "DELETE FROM stock.inventory_lines;" in dump
    assert "'O''HARA PLATE'" in dump
    assert "E'back\\\\slash'" in dump
    assert "'2026-09-07'" in dump and "TRUE" in dump and "NULL" in dump
    assert dump.count("COMMIT;") == 1

    assert pg.literal(None) == "NULL"
    assert pg.literal(True) == "TRUE"
    assert pg.literal(12) == "12"
    assert pg.literal(date(2026, 1, 2)) == "'2026-01-02'"
    assert pg.literal("plain") == "'plain'"


def test_the_queue_keeps_one_copy_of_each_file(tmp_path):
    from inventory import intake

    config = make_config(tmp_path)
    db.init(config.db_path)

    first = intake.add(config, b"%PDF-1.4 note", "Bon de livraison.pdf", "application/pdf")
    again = intake.add(config, b"%PDF-1.4 note", "copy of the same.pdf", "application/pdf")
    sheet = intake.add(config, b"article;qty\nBOLT M6;10\n", "lines.csv", "text/csv")

    assert first["duplicate"] is False and again["duplicate"] is True
    assert again["intake_id"] == first["intake_id"]
    assert len(intake.waiting(config)) == 2
    assert intake.counts(config) == {"waiting": 2}

    kinds = {row["name"]: row["kind"] for row in intake.waiting(config)}
    assert kinds["Bon de livraison.pdf"] == "scan"
    assert kinds["lines.csv"] == "sheet"

    raw, media_type, item = intake.bytes_of(config, sheet["intake_id"])
    assert raw.startswith(b"article;") and media_type == "text/csv"
    assert item["path"].stat().st_mode & 0o777 == 0o600

    with pytest.raises(ValueError):
        intake.add(config, b"zip", "catalogue.zip", "application/zip")

    intake.mark(config, first["intake_id"], intake.DONE, "note #4")
    assert len(intake.waiting(config)) == 1

    path = intake.one(config, sheet["intake_id"])["path"]
    assert intake.drop(config, sheet["intake_id"]) is True
    assert not path.exists()
    assert intake.drop(config, 9999) is False


def test_a_spreadsheet_becomes_lines(tmp_path):
    from inventory import sheets

    raw = ("Article;Quantite;Fournisseur;Date livraison;Chantier\n"
           "bolt m6;500;foussier;2026-09-21;ariane\n"
           "washer m6;1 200;foussier;21/09/2026;ariane\n"
           ";80;foussier;2026-09-21;ariane\n"
           "sheet alu;;metaleco;2026-09-21;ariane\n"
           "insert m6;12;metaleco;;ariane\n").encode()

    header, body = sheets.read_table(raw, "text/csv")
    assert header[0] == "Article" and len(body) == 5

    mapping = sheets.guess_mapping(header, "inventory")
    assert mapping["article"] == 0 and mapping["quantity"] == 1
    assert mapping["supplier"] == 2 and mapping["site"] == 4

    kept, dropped = sheets.shape(header, body, mapping, "inventory")
    assert [row["article"] for row in kept] == ["BOLT M6", "WASHER M6"]
    assert kept[0]["quantity"] == 500 and kept[1]["quantity"] == 1200
    assert kept[1]["delivery_date"] == date(2026, 9, 21)
    assert [row["why"] for row in dropped] == ["no article", "no quantity", "no delivery date"]

    config = make_config(tmp_path)
    db.init(config.db_path)
    answer = sheets.store_inventory(config.db_path, kept, "ALEX")
    assert answer["lines"] == 2
    stored = db.run_readonly(config.db_path,
                             "SELECT article, quantity, weekday, site FROM inventory_lines "
                             "ORDER BY article")
    assert stored.rows == [("BOLT M6", 500.0, "Monday", "ARIANE"),
                           ("WASHER M6", 1200.0, "Monday", "ARIANE")]

    with pytest.raises(ValueError):
        sheets.shape(header, body, {"quantity": 1}, "inventory")


def test_a_catalogue_file_fills_the_catalog(tmp_path):
    from inventory import sheets

    raw = ("article,supplier,reference,description\n"
           "bolt m6,foussier,306260,VIS MX TH 8.8\n"
           "orphan,,,no supplier here\n").encode()
    header, body = sheets.read_table(raw, "text/csv")
    mapping = sheets.guess_mapping(header, "catalog")
    kept, dropped = sheets.shape(header, body, mapping, "catalog")

    assert kept == [{"article": "BOLT M6", "supplier": "FOUSSIER",
                     "designation": "VIS MX TH 8.8", "reference": "306260"}]
    assert dropped[0]["why"] == "no supplier"

    config = make_config(tmp_path)
    db.init(config.db_path)
    assert sheets.store_catalog(config.db_path, kept)["lines"] == 1
    assert db.run_readonly(config.db_path, "SELECT article, reference FROM catalog").rows == [
        ("BOLT M6", "306260")]


def test_mail_queues_the_attachments(tmp_path, monkeypatch):
    import email.message

    from inventory import mailbox

    config = make_config(tmp_path)
    db.init(config.db_path)

    message = email.message.EmailMessage()
    message["From"] = "ventes@foussier.example"
    message["Subject"] = "Bon de livraison 306260"
    message.set_content("Bonjour, ci-joint le bon.")
    message.add_attachment(b"%PDF-1.4 bon", maintype="application", subtype="pdf",
                           filename="BL-306260.pdf")
    message.add_attachment(b"MZ not a note", maintype="application", subtype="octet-stream",
                           filename="installer.exe")
    raw = message.as_bytes()

    flags = []

    class FakeBox:
        def select(self, folder, readonly=False):
            return "OK", [b"1"]

        def search(self, charset, what):
            assert what == "UNSEEN"
            return "OK", [b"1"]

        def fetch(self, number, parts):
            return "OK", [(b"1 (RFC822)", raw)]

        def store(self, number, what, flag):
            flags.append((number, flag))
            return "OK", [b""]

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr(mailbox, "connect", lambda settings: FakeBox())
    answer = mailbox.fetch(config, config.mail)

    assert answer == {"messages": 1, "queued": 1, "duplicates": 0, "names": ["BL-306260.pdf"]}
    assert flags == [(b"1", "\\Seen")]

    from inventory import intake

    row = intake.waiting(config)[0]
    assert row["source"] == "mail" and row["sender"] == "ventes@foussier.example"
    assert row["subject"] == "Bon de livraison 306260" and row["kind"] == "scan"

    assert mailbox.fetch(config, config.mail)["duplicates"] == 1


def test_mail_without_settings_says_so(tmp_path):
    from inventory import mailbox

    config = make_config(tmp_path)
    with pytest.raises(mailbox.MailError):
        mailbox.connect(config.mail)
    ok, detail = mailbox.check(config.mail)
    assert ok is False and "No mailbox" in detail


def test_intake_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("IMAP_HOST", raising=False)

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        answer = client.post("/api/intake", headers=mine, files=[
            ("files", ("lines.csv", b"article;qty;fournisseur;date livraison\n"
                                    b"bolt m6;10;foussier;2026-09-21\n", "text/csv")),
            ("files", ("note.pdf", b"%PDF-1.4 note", "application/pdf")),
            ("files", ("virus.exe", b"MZ", "application/octet-stream")),
        ]).json()
        assert len(answer["added"]) == 2
        assert answer["refused"][0]["name"] == "virus.exe"

        queue = client.get("/api/intake").json()
        assert len(queue["waiting"]) == 2
        assert queue["mail"]["configured"] is False
        sheet = next(row for row in queue["waiting"] if row["kind"] == "sheet")
        scan = next(row for row in queue["waiting"] if row["kind"] == "scan")

        preview = client.get(f"/api/import/{sheet['intake_id']}").json()
        assert preview["header"][0] == "article" and preview["rows"] == 1
        assert preview["mapping"]["article"] == 0

        dry = client.post(f"/api/import/{sheet['intake_id']}", headers=mine,
                          json={"mapping": preview["mapping"], "dry_run": True}).json()
        assert dry["kept"] == 1

        done = client.post(f"/api/import/{sheet['intake_id']}", headers=mine,
                           json={"mapping": preview["mapping"]}).json()
        assert done["lines"] == 1 and done["note_id"]
        notes = client.get("/api/data").json()["notes"]
        assert sum(len(note["lines"]) for note in notes) == 1

        class NoModel:
            model = "none"

            async def extract(self, *args, **kwargs):
                raise llm.ExtractionError("no model on this machine")

        server.backend = NoModel()
        refused = client.post(f"/api/intake/{scan['intake_id']}/read", headers=mine)
        assert refused.status_code == 502 and "no model" in refused.json()["detail"]
        assert client.get("/api/import/9999").status_code == 404

        assert client.post("/api/mail/fetch", headers=mine).status_code == 422
        assert client.post("/api/mail/check", headers=mine).json()["ok"] is False

        left = client.get("/api/intake").json()["waiting"]
        assert len(left) == 1
        assert client.delete(f"/api/intake/{left[0]['intake_id']}",
                             headers=mine).status_code == 200
        assert client.get("/api/intake").json()["waiting"] == []

        page = client.get("/").text
        # The queue table was taken off the screen; importing and mapping stayed.
        assert 'id="intake-files"' in page and 'id="mapping-target"' in page
        assert 'id="table-intake"' not in page


def test_a_spreadsheet_cannot_smuggle_a_formula(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    db.init(server.config.db_path)
    note = make_note(lines=[Line(article="=cmd|' /c calc'!A1", quantity=1),
                            Line(article="BOLT M6", quantity=2)])
    db.save_note(server.config.db_path, note, "sha-formula", "", "ALEX")

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        sheet = client.get("/export/inventory.csv").text
        assert "'=CMD" in sheet.upper()
        assert "\n=" not in sheet and ";=" not in sheet

        many = [("files", (f"f{number}.pdf", b"%PDF-1.4", "application/pdf"))
                for number in range(server.MAX_FILES + 1)]
        assert client.post("/api/intake", headers=mine, files=many).status_code == 413


def test_the_bridge_is_the_only_unchecked_certificate(tmp_path):
    import ssl

    from inventory import mailbox
    from inventory.config import MailConfig

    def settings(host):
        return MailConfig(host=host, port=1143, user="me", password="x", folder="INBOX",
                          security="starttls", insecure=True, mark_read=True, every_minutes=15)

    here = mailbox._context(settings("127.0.0.1"))
    assert here.verify_mode == ssl.CERT_NONE

    away = mailbox._context(settings("imap.example.com"))
    assert away.verify_mode == ssl.CERT_REQUIRED and away.check_hostname is True


def test_bill_shaping_fills_in_what_it_can(db_path):
    from inventory import bills

    row = bills.shape({"supplier": "foussier", "number": " FA-2210 ", "bill_date": "05/10/2026",
                       "due_date": "2026-11-30", "net": "1 234,56", "tax": "246,91",
                       "total": None, "site": "ariane", "order_ref": "BL 88"},
                      source="mail", user="ALEX", sender="compta@foussier.fr")
    assert row["supplier"] == "FOUSSIER" and row["number"] == "FA-2210"
    assert row["bill_date"] == date(2026, 10, 5) and row["currency"] == "EUR"
    assert row["net"] == 1234.56 and row["total"] == 1481.47
    assert row["site"] == "ARIANE" and row["state"] == "to pay"
    assert bills.problems(row) == []

    thin = bills.shape({"supplier": None, "bill_date": "not a date", "total": None})
    assert bills.problems(thin) == ["supplier missing", "bill date missing or unreadable",
                                    "total missing"]

    from_total = bills.shape({"supplier": "X", "bill_date": "2026-10-01", "total": "1 200.00",
                              "tax": "200"})
    assert from_total["net"] == 1000.0

    bill_id = bills.save(db_path, row)
    assert bills.find_same(db_path, "FOUSSIER", "FA-2210") == bill_id
    stored = bills.one(db_path, bill_id)
    assert stored["month"] == "2026-10" and stored["sender"] == "compta@foussier.fr"

    bills.save(db_path, bills.shape({"supplier": "METALECO", "number": "M-9",
                                     "bill_date": "2026-09-02", "total": "480"}))
    months = bills.by_month(db_path)
    assert [month["month"] for month in months] == ["2026-10", "2026-09"]
    summary = bills.summary(db_path)
    assert summary["bills"] == 2 and summary["owed"] == 1961.47 and summary["suppliers"] == 2

    assert bills.update(db_path, bill_id, {"state": "paid"}) is True
    paid = bills.one(db_path, bill_id)
    assert paid["state"] == "paid" and paid["paid_on"] == date.today().isoformat()
    assert bills.summary(db_path)["owed"] == 480.0
    assert bills.update(db_path, bill_id, {"bill_id": 7, "state": "nonsense"}) is False
    assert bills.drop(db_path, bill_id) is True and bills.drop(db_path, bill_id) is False


def test_bill_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("IMAP_HOST", raising=False)

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        empty = client.get("/api/bills").json()
        assert empty["months"] == [] and empty["summary"]["bills"] == 0

        bill = {"supplier": "foussier", "number": "FA-1", "bill_date": "2026-10-05",
                "due_date": "2026-10-01", "net": "100", "tax": "20"}
        saved = client.post("/api/bills", headers=mine, json={"bill": bill}).json()
        assert saved["bill"]["total"] == 120.0 and saved["bill"]["state"] == "to pay"

        again = client.post("/api/bills", headers=mine, json={"bill": bill})
        assert again.status_code == 409 and "already stored" in again.json()["detail"]

        thin = client.post("/api/bills", headers=mine, json={"bill": {"supplier": "x"}})
        assert thin.status_code == 422 and "bill date" in thin.json()["detail"]

        listed = client.get("/api/bills").json()
        assert listed["summary"]["owed"] == 120.0 and listed["summary"]["late"] == 120.0
        assert listed["months"][0]["month"] == "2026-10"
        assert "suppliers" not in listed and "bills" not in listed

        bill_id = saved["bill_id"]
        changed = client.patch(f"/api/bills/{bill_id}", headers=mine,
                               json={"state": "paid"}).json()
        assert changed["bill"]["state"] == "paid"
        assert client.get("/api/bills").json()["summary"]["owed"] == 0
        assert client.patch("/api/bills/404", headers=mine,
                            json={"state": "paid"}).status_code == 404

        sql = client.get("/export/bills.sql").text
        assert "CREATE TABLE IF NOT EXISTS stock.bills" in sql and "FOUSSIER" in sql
        assert client.get("/export/bills.csv").status_code == 200

        class NoEyes:
            model = "none"

            async def extract_bill(self, *args, **kwargs):
                raise llm.ExtractionError("no model on this machine")

        server.backend = NoEyes()
        refused = client.post("/api/bills/read", headers=mine,
                              files={"photo": ("bill.pdf", b"%PDF-1.4 bill", "application/pdf")})
        assert refused.status_code == 502 and "no model" in refused.json()["detail"]

        assert client.delete(f"/api/bills/{bill_id}", headers=mine).status_code == 200
        assert client.delete(f"/api/bills/{bill_id}", headers=mine).status_code == 404
        assert client.get("/api/bills").json()["summary"]["bills"] == 0

        # The months are what the screen shows; the bills themselves are not listed.
        page = client.get("/").text
        assert 'id="table-bill-months"' in page
        assert 'id="table-bills"' not in page and 'id="bill-suppliers"' not in page


def test_mail_provider_presets_fill_the_server_in(monkeypatch, tmp_path):
    from inventory import mailbox
    from inventory.config import load_config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in ("IMAP_HOST", "IMAP_PORT", "IMAP_SECURITY", "IMAP_INSECURE"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("MAIL_PROVIDER", "gmail")
    gmail = load_config(data_dir=tmp_path).mail
    assert (gmail.host, gmail.port, gmail.security) == ("imap.gmail.com", 993, "ssl")
    assert gmail.insecure is False and gmail.provider == "gmail"

    monkeypatch.setenv("MAIL_PROVIDER", "proton")
    proton = load_config(data_dir=tmp_path).mail
    assert (proton.host, proton.port, proton.security) == ("127.0.0.1", 1143, "starttls")
    assert proton.insecure is True

    monkeypatch.setenv("IMAP_HOST", "mail.example.com")
    monkeypatch.setenv("IMAP_PORT", "143")
    by_hand = load_config(data_dir=tmp_path).mail
    assert (by_hand.host, by_hand.port) == ("mail.example.com", 143)

    monkeypatch.delenv("MAIL_PROVIDER")
    monkeypatch.delenv("IMAP_HOST")
    monkeypatch.delenv("IMAP_PORT")
    plain = load_config(data_dir=tmp_path).mail
    assert plain.host == "" and plain.provider == "" and plain.enabled is False

    assert {row["id"] for row in mailbox.providers()} == set(mailbox.PROVIDERS)
    assert all(row["help"] for row in mailbox.providers())
    assert "insecure" not in mailbox.providers()[0]
    assert mailbox.preset("nonsense") == mailbox.PROVIDERS["other"]


def test_chat_basket_becomes_a_purchase_order(db_path):
    from inventory import baskets, discord_bot

    items = baskets.parse_items("12 vis th 6x25 inox\n3x rondelle m8; ecrou borgne d6 x 100")
    # A line carries a price now, empty when the shop page gave none.
    assert items == [{"article": "VIS TH 6X25 INOX", "quantity": 12.0, "unit_price": None},
                     {"article": "RONDELLE M8", "quantity": 3.0, "unit_price": None},
                     {"article": "ECROU BORGNE D6", "quantity": 100.0, "unit_price": None}]
    assert baskets.parse_items("   ") == []
    assert baskets.parse_items("silicone noir")[0]["quantity"] == 1.0

    baskets.add(db_path, 777, items, shop="foussier.fr", site="ariane",
                work_item="ecran acoustique", who="LOUIS")
    held = baskets.lines(db_path, 777)
    assert len(held) == 3 and held[0]["shop"] == "foussier.fr" and held[0]["site"] == "ARIANE"
    assert baskets.shops(db_path, 777) == ["foussier.fr"]

    open_now = baskets.open_baskets(db_path)
    assert open_now[0]["channel"] == "777" and open_now[0]["lines"] == 3

    assert baskets.drop_line(db_path, 777, held[0]["line_id"]) is True
    assert baskets.drop_line(db_path, 777, held[0]["line_id"]) is False

    answer = baskets.to_purchase_order(db_path, 777, "LOUIS")
    assert answer["list_id"] and answer["site"] == "ARIANE" and len(answer["lines"]) == 2
    assert baskets.lines(db_path, 777) == []
    assert baskets.to_purchase_order(db_path, 777, "LOUIS") is None

    stored = db.purchase_orders(db_path)[0]
    assert {line["article"] for line in stored["lines"]} == {"RONDELLE M8", "ECROU BORGNE D6"}
    assert stored["drafter"] == "LOUIS"

    assert "does not pay" in discord_bot.purchase_text(answer)
    assert "#" + str(answer["list_id"]) in discord_bot.purchase_text(answer)
    assert discord_bot.purchase_text(None) == "Nothing in the basket to order."
    assert "empty" in discord_bot.basket_text([])
    assert "foussier.fr" in discord_bot.basket_text(held)
    assert discord_bot.allowed(777, []) is True
    assert discord_bot.allowed(777, ["42"]) is False
    assert discord_bot.allowed(777, ["777"]) is True


def test_mail_and_chat_settings_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "MAIL_PROVIDER",
                "DISCORD_BOT_TOKEN", "DISCORD_CHANNELS"):
        monkeypatch.delenv(key, raising=False)

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        start = client.get("/api/mail/settings").json()
        assert start["current"]["configured"] is False
        assert start["current"]["password_set"] is False
        assert "gmail" in {row["id"] for row in start["providers"]}

        saved = client.post("/api/mail/settings", headers=mine, json={
            "provider": "gmail", "user": "stock@company.com", "password": "hunter2-xyzzy",
            "folder": "Suppliers", "every_minutes": 5,
        }).json()
        assert saved["current"]["host"] == "imap.gmail.com"
        assert saved["current"]["folder"] == "Suppliers"
        assert saved["current"]["configured"] is True
        assert saved["current"]["password_set"] is True
        assert "hunter2-xyzzy" not in client.get("/api/mail/settings").text

        kept = client.post("/api/mail/settings", headers=mine, json={
            "provider": "gmail", "user": "stock@company.com", "every_minutes": 5,
        }).json()
        assert kept["current"]["password_set"] is True

        assert client.post("/api/mail/settings", headers=mine,
                           json={"provider": "nope"}).status_code == 422

        # Two doors now, so the answer names them: Discord and Slack side by side.
        chat = client.post("/api/chat/settings", headers=mine,
                           json={"token": "a-bot-token", "channels": "777, 888"}).json()
        assert chat["discord"]["token_set"] is True
        assert chat["discord"]["channels"] == ["777", "888"]
        assert "a-bot-token" not in client.get("/api/chat/settings").text

        gone = client.post("/api/chat/settings", headers=mine,
                           json={"channels": "777", "forget": True}).json()
        assert gone["discord"]["token_set"] is False

        slack = client.post("/api/slack/settings", headers=mine,
                            json={"bot_token": "xoxb-secret", "app_token": "xapp-secret",
                                  "channels": "C01"}).json()
        assert slack["slack"]["bot_token_set"] is True
        assert slack["slack"]["app_token_set"] is True
        assert slack["slack"]["channels"] == ["C01"]
        assert "xoxb-secret" not in client.get("/api/chat/settings").text

        dropped = client.post("/api/slack/settings", headers=mine,
                              json={"channels": "C01", "forget": True}).json()
        assert dropped["slack"]["bot_token_set"] is False
        assert dropped["slack"]["app_token_set"] is False

        assert client.get("/api/baskets").json()["baskets"] == []
        assert client.post("/api/baskets/777/order", headers=mine).status_code == 404
        assert client.delete("/api/baskets/777", headers=mine).status_code == 404

        page = client.get("/").text
        assert 'id="mail-provider"' in page and 'id="chat-token"' in page
        assert 'id="slack-bot-token"' in page and 'id="slack-app-token"' in page


def test_the_bot_registers_its_commands(db_path, tmp_path):
    """Only runs where the extra is installed: pip install 'stock-manager[discord]'."""
    discord = pytest.importorskip("discord")
    from discord import app_commands

    from inventory import discord_bot

    held = []

    class Spy(app_commands.CommandTree):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            held.append(self)

    config = make_config(tmp_path)
    original = app_commands.CommandTree
    app_commands.CommandTree = Spy
    try:
        client = discord_bot.build_client(config, ["777"])
    finally:
        app_commands.CommandTree = original

    assert isinstance(client, discord.Client)
    names = {command.name: [p.name for p in command.parameters]
             for command in held[0].get_commands()}
    assert set(names) == {"basket", "basket_show", "basket_clear", "purchase", "stock", "quote"}
    assert names["quote"] == ["supplier", "items", "site", "work_item"]
    assert names["basket"] == ["shop", "items", "site", "work_item", "supplier"]


def test_the_logo_is_served_before_the_password(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("STOCK_USER", raising=False)

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    with TestClient(server.app) as client:
        for path in ("/logo.png", "/icon.png"):
            answer = client.get(path, follow_redirects=False)
            assert answer.status_code == 200
            assert answer.headers["content-type"] == "image/png"
            assert answer.content[:8] == b"\x89PNG\r\n\x1a\n"
        page = client.get("/unlock").text
        assert 'href="/icon.png"' in page
        assert 'url("/logo.png")' in page

        # The landing picture belongs to the app page, so it waits for the password.
        assert client.get("/landing.png", follow_redirects=False).status_code == 303
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"},
                    headers={"origin": "http://testserver"})
        answer = client.get("/landing.png")
        assert answer.status_code == 200 and answer.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_shelf_counts_itself_from_notes_and_orders(db_path):
    from inventory import stock

    db.save_note(db_path, make_note(lines=[Line(article="BOLT M6", quantity=100)]),
                 "in-1", "", "ALEX")
    db.save_purchase_order(db_path, {"list_date": date.today() - timedelta(days=3), "site": "C",
                                     "lines": [{"article": "BOLT M6", "quantity": 30}]},
                           "out-1", "", "ALEX")
    db.save_purchase_order(db_path, {"list_date": date.today(), "site": "C",
                                     "lines": [{"article": "BOLT M6", "quantity": 5}]},
                           "out-2", "", "ALEX")

    answer = stock.summary(db_path)
    bolts = {row["article"]: row for row in answer["rows"]}["BOLT M6"]
    assert bolts["received"] == 100 and bolts["shipped"] == 30
    # Today's order has not had its 24 hours yet, so it is still being picked.
    assert bolts["picking"] == 5 and bolts["on_hand"] == 70
    assert bolts["counted"] is None and answer["negative"] == 0

    db.save_stock_count(db_path, "BOLT M6", 64, "ALEX")
    again = {row["article"]: row for row in stock.rows(db_path)}["BOLT M6"]
    assert again["counted"] == 64 and again["gap"] == -6
    assert stock.on_hand_map(db_path)["BOLT M6"] == 64


def test_a_quote_asks_in_the_right_unit(db_path):
    from inventory import quotes

    asked = quotes.parse_request("120 ml skirting board; 3 m3 gravel; 12 anchors")
    assert [(line["quantity"], line["unit"], line["article"]) for line in asked] == [
        (120.0, "ml", "SKIRTING BOARD"), (3.0, "m3", "GRAVEL"), (12.0, "piece", "ANCHORS")]

    quote_id = quotes.save(db_path, {"supplier": "foussier", "site": "villa"}, asked, "ALEX")
    stored = quotes.one(db_path, quote_id)
    assert stored["supplier"] == "FOUSSIER" and stored["state"] == "open"
    assert [row["unit_label"] for row in stored["rows"]] == ["linear metre", "cubic metre", "piece"]

    text = quotes.as_text(stored)
    assert "120 ml SKIRTING BOARD" in text and "price" in text
    assert "€" not in text and "EUR" not in text

    assert quotes.update(db_path, quote_id, {"state": "sent"})
    assert quotes.one(db_path, quote_id)["state"] == "sent"
    assert not quotes.update(db_path, quote_id, {"state": "nonsense"})
    assert quotes.drop(db_path, quote_id) and quotes.one(db_path, quote_id) is None


def test_quote_and_stock_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        assert client.get("/api/quotes").json()["quotes"] == []
        saved = client.post("/api/quotes", headers=mine,
                            json={"supplier": "foussier", "text": "250 kg sand; 40 ml pipe"}).json()
        quote_id = saved["quote_id"]
        assert len(saved["quote"]["rows"]) == 2

        one = client.get(f"/api/quotes/{quote_id}").json()
        assert "250 kg SAND" in one["text"]
        assert client.patch(f"/api/quotes/{quote_id}", headers=mine,
                            json={"state": "sent"}).json()["quote"]["state"] == "sent"

        empty = client.post("/api/quotes", headers=mine, json={"text": "   "})
        assert empty.status_code == 422

        tables = {row["table"] for row in client.get("/api/tables").json()["tables"]}
        assert {"inventory_lines", "purchase_orders", "bills", "quotes", "places"} <= tables
        assert "schema_migrations" not in tables

        shelf = client.get("/api/stock").json()
        assert shelf["articles"] == 0 and shelf["shipping_days"] == 1

        assert client.delete(f"/api/quotes/{quote_id}", headers=mine).status_code == 200
        assert client.get(f"/api/quotes/{quote_id}").status_code == 404


def test_the_route_is_ordered_without_the_internet(db_path):
    from inventory import routing

    answers = {
        "YARD": {"lat": 0.0, "lon": 0.0, "display_name": "Yard, Town"},
        "FAR": {"lat": 0.0, "lon": 0.9, "display_name": "Far, Town"},
        "NEAR": {"lat": 0.0, "lon": 0.1, "display_name": "Near, Town"},
        "MIDDLE": {"lat": 0.0, "lon": 0.5, "display_name": "Middle, Town"},
    }

    def fake(url):
        if "nominatim" in url:
            for name, point in answers.items():
                if name.lower() in url.lower():
                    return [{"lat": str(point["lat"]), "lon": str(point["lon"]),
                             "display_name": point["display_name"]}]
            return []
        raise OSError("no road service here")

    routing.add(db_path, "yard", "YARD", "depot", None, "ALEX", fetch=fake)
    for name in ("far", "near", "middle"):
        routing.add(db_path, name, name.upper(), "stop", None, "ALEX", fetch=fake)

    plan = routing.route(db_path, round_trip=False, fetch=fake)
    assert plan["source"] == "straight"
    assert [stop["label"] for stop in plan["stops"]] == ["YARD", "NEAR", "MIDDLE", "FAR"]
    assert plan["km"] > 0 and len(plan["legs"]) == 3

    # A round trip ends where it started, so it is longer.
    loop = routing.route(db_path, round_trip=True, fetch=fake)
    assert loop["metres"] > plan["metres"] and len(loop["legs"]) == 4

    only_depot = routing.clear_stops(db_path)
    assert only_depot == 3
    with pytest.raises(routing.MapError):
        routing.route(db_path, fetch=fake)


def test_the_route_asks_the_road_service_first(db_path):
    from inventory import routing

    def fake(url):
        if "nominatim" in url:
            return [{"lat": "1.0", "lon": "1.0", "display_name": "Somewhere"}]
        return {"code": "Ok", "distances": [[0, 400, 900], [400, 0, 600], [900, 600, 0]]}

    routing.add(db_path, "yard", "YARD", "depot", None, "ALEX", fetch=fake)
    routing.add(db_path, "a", "A", "stop", None, "ALEX", fetch=fake)
    routing.add(db_path, "b", "B", "stop", None, "ALEX", fetch=fake)

    plan = routing.route(db_path, round_trip=True, fetch=fake)
    assert plan["source"] == "road"
    assert plan["metres"] == 400 + 600 + 900
    assert [stop["label"] for stop in plan["stops"]] == ["YARD", "A", "B"]


def test_a_contact_rides_on_the_order(db_path):
    from inventory import contacts

    contact_id = contacts.save(db_path, {"company": "bati sud", "person": "Mme Roussel",
                                         "phone": "0556", "email": "a@b.fr",
                                         "address": "12 rue Fondaudege", "incoterm": "dap",
                                         "site": "la genette"}, "ALEX")
    stored = contacts.one(db_path, contact_id)
    assert stored["company"] == "BATI SUD" and stored["incoterm"] == "DAP"
    assert contacts.for_site(db_path, "LA GENETTE")["contact_id"] == contact_id

    list_id = db.save_purchase_order(db_path, {"site": "LA GENETTE",
                                               "lines": [{"article": "BOLT", "quantity": 2}]},
                                     "sha-c", "", "ALEX")
    assert contacts.put_on_order(db_path, list_id, contact_id, "ddp", "2026-11-12")
    order = {sheet["id"]: sheet for sheet in db.purchase_orders(db_path)}[list_id]
    assert order["incoterm"] == "DDP" and order["ship_by"] == "2026-11-12"
    assert order["contact"]["person"] == "Mme Roussel"

    # Nonsense terms are dropped rather than stored.
    contacts.put_on_order(db_path, list_id, None, "banana", None)
    assert {sheet["id"]: sheet for sheet in db.purchase_orders(db_path)}[list_id]["incoterm"] is None

    assert contacts.drop(db_path, contact_id)
    assert {sheet["id"]: sheet for sheet in db.purchase_orders(db_path)}[list_id]["contact"] is None


def test_every_box_the_page_wires_up_is_on_the_page():
    """The page is edited by hand; a box taken out must not strand its screen."""
    import re

    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    here = set(re.findall(r'\bid="([^"]+)"', page))
    # Listeners hung at the top level run as the page loads: a missing one used to
    # throw there and leave every screen below it dead.
    wired = re.findall(r'^\$\("([^"]+)"\)(\??)\.addEventListener', page, re.M)
    assert [name for name, optional in wired if not optional and name not in here] == []

    # Nothing is left half-written either.
    assert page.count("<section") == page.count("</section>")
    assert page.count("<label") == page.count("</label>")


def test_the_contact_file_on_the_disk_fills_the_book(db_path, tmp_path):
    from inventory import contacts

    paper = tmp_path / "supplier-contacts.csv"
    paper.write_text("Societe;Contact;Tel;Mail;Adresse;Incoterm;Chantier;Remarque\n"
                     "foussier;M. Lagarde;05 99 00 00 01;a@foussier.example.com;"
                     "2 rue des Ateliers;dap;la genette;fixings\n"
                     "metaleco;;05 99 00 00 02;;;banana;;\n"
                     ";nobody;;;;;;a line with no company is skipped\n",
                    encoding="utf-8")

    where, rows = contacts.read_file(paper)
    assert where == paper and len(rows) == 2

    done = contacts.load_file(db_path, paper, "ALEX")
    assert (done["read"], done["added"], done["updated"]) == (2, 2, 0)
    book = {row["company"]: row for row in contacts.all_of(db_path)}
    assert book["FOUSSIER"]["incoterm"] == "DAP" and book["FOUSSIER"]["site"] == "LA GENETTE"
    assert book["METALECO"]["incoterm"] is None          # nonsense terms are dropped
    assert book["METALECO"]["phone"] == "05 99 00 00 02"

    # Reading it again recognises what is already there instead of doubling it.
    again = contacts.load_file(db_path, paper, "ALEX")
    assert (again["added"], again["updated"], again["same"]) == (0, 0, 2)
    assert len(contacts.all_of(db_path)) == 2

    with pytest.raises(FileNotFoundError):
        contacts.read_file(tmp_path / "nothing.csv")


def test_the_notes_say_what_is_standing_at_each_place(db_path):
    from inventory import stock

    db.save_note(db_path, make_note(site="LA GENETTE", work_item="PHASE 3",
                                    lines=[Line(article="SPIT FIX", quantity=100)]),
                 "sha-place-1", "", "ALEX")
    db.save_note(db_path, make_note(site="OSTREA", work_item="PORTAIL",
                                    lines=[Line(article="SPIT FIX", quantity=40)]),
                 "sha-place-2", "", "ALEX")
    mine = db.save_purchase_order(db_path, {"site": "LA GENETTE", "work_item": "PHASE 3",
                                            "lines": [{"article": "SPIT FIX", "quantity": 60}]},
                                  "sha-place-3", "", "ALEX")
    other = db.save_purchase_order(db_path, {"site": "LA GENETTE", "work_item": "PHASE 3",
                                             "lines": [{"article": "SPIT FIX", "quantity": 25}]},
                                   "sha-place-4", "", "ALEX")

    held = stock.by_place(db_path)
    # The order being judged is left out, the other one has already taken its share.
    assert stock.cover(held, "SPIT FIX", "LA GENETTE", "PHASE 3", besides=mine) == 75
    assert stock.cover(held, "SPIT FIX", "OSTREA", "PORTAIL", besides=mine) == 40
    # Nothing was delivered for TASSIGNY and no note left the site blank, so it holds none.
    assert stock.cover(held, "SPIT FIX", "TASSIGNY", None, besides=mine) == 0
    assert stock.cover(held, "NOTHING LIKE IT", "LA GENETTE", None) == 0.0
    assert stock.cover(held, "SPIT FIX", "LA GENETTE", "PHASE 3", besides=other) == 40


def test_the_sample_year_hangs_together():
    """The made-up year must read like the real paperwork, or it teaches nothing."""
    import importlib.util
    import random

    here = pathlib.Path(__file__).resolve().parent.parent / "sample_data"
    spec = importlib.util.spec_from_file_location("make_sample_data",
                                                  here / "make_sample_data.py")
    maker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(maker)

    year = maker.build(random.Random(1), days=120)
    assert year["notes"] and year["orders"] and year["quotes"] and year["bills"]

    # Every line belongs to a head, and every head has lines.
    heads = {note["note_id"] for note in year["notes"]}
    assert {line["note_id"] for line in year["note_lines"]} == heads
    sheets = {order["list_id"] for order in year["orders"]}
    assert {line["list_id"] for line in year["order_lines"]} == sheets

    days = sorted(note["delivery_date"] for note in year["notes"])
    assert days[0] < days[-1] <= maker.LAST_DAY.isoformat()
    assert all(line["order_date"] <= line["delivery_date"] for line in year["note_lines"])
    assert all(float(line["quantity"]) > 0 for line in year["note_lines"])

    missing = [line for line in year["note_lines"] if line["backorder"]]
    assert missing and any(line["expected_on"] for line in missing)

    # Most of what goes out was delivered for that site; some of it never was.
    knows = {(line["site"], line["article"]) for line in year["note_lines"]}
    places = {order["list_id"]: order["site"] for order in year["orders"]}
    answered = [line for line in year["order_lines"]
                if (places[line["list_id"]], line["article"]) in knows]
    assert len(year["order_lines"]) > len(answered) > len(year["order_lines"]) // 2

    assert {bill["state"] for bill in year["bills"]} <= set(maker.BILL_STATES)
    assert all(bill["total"] > 0 for bill in year["bills"])
    assert {line["unit"] for line in year["quote_lines"]} <= set(quotes.UNITS)


def test_the_contact_file_answers_a_name_and_a_company(db_path, tmp_path):
    from inventory import contacts

    paper = tmp_path / "supplier-contacts.csv"
    paper.write_text("company,person,phone,site\n"
                     "FOUSSIER,Marc Bernard,05 99 00 00 01,LA GENETTE\n"
                     "FOUSSIER,Claire Roux,05 99 00 00 02,OSTREA\n"
                     "WURTH,Marc Dubois,05 99 00 00 03,\n", encoding="utf-8")

    whole = contacts.look_up(path=paper)
    assert whole["found"] == 3 and whole["columns"][0] == "company"

    assert [row["person"] for row in contacts.look_up(company="foussier", path=paper)["rows"]] \
        == ["Marc Bernard", "Claire Roux"]
    assert [row["company"] for row in contacts.look_up(name="marc", path=paper)["rows"]] \
        == ["FOUSSIER", "WURTH"]
    assert contacts.look_up(name="marc", company="wurth", path=paper)["found"] == 1
    assert contacts.look_up(name="nobody", path=paper)["rows"] == []
    assert contacts.look_up(path=paper, limit=2)["rows"].__len__() == 2


def test_the_missing_lines_are_listed_by_note(db_path):
    from inventory import backorders

    note = make_note(lines=[Line(article="CEMENT 35KG", quantity=40),
                            Line(article="MESH ST25", quantity=5, backorder=True)])
    db.save_note(db_path, note, "sha-back", "", "ALEX")

    waiting = backorders.notes(db_path)
    assert len(waiting) == 1 and len(waiting[0]["missing"]) == 1
    missing = waiting[0]["missing"][0]
    assert missing["article"] == "MESH ST25" and missing["supplier"] == "ACME SUPPLIES"
    assert missing["site"] == "VILLA PROJECT" and missing["expected_on"] is None
    assert backorders.summary(db_path) == {"lines": 1, "notes": 1, "suppliers": 1,
                                           "late": 0, "undated": 1}

    backorders.expect(db_path, missing["note_id"], missing["line_no"], "2020-01-02")
    assert backorders.summary(db_path)["late"] == 1

    backorders.settle(db_path, missing["note_id"], missing["line_no"])
    assert backorders.notes(db_path) == []
    assert backorders.rows(db_path, settled=True)[0]["article"] == "MESH ST25"


def test_a_van_says_when_the_load_does_not_fit(db_path):
    from inventory import routing

    vans = {van["name"]: van for van in routing.vehicles(db_path)}
    assert set(vans) == {"Renault Partner 1", "Renault Partner 2", "Iveco Daily"}
    partner = vans["Renault Partner 1"]
    assert partner["cubic"] == 3.3 and partner["shape"] == "van"

    stops = [{"label": "A", "cubic": 2.4, "kilos": 400, "open_from": "08:00", "open_to": "12:00"},
             {"label": "B", "cubic": 1.6, "kilos": 300, "open_from": "13:00", "open_to": "17:00"}]
    assert routing.load_of(stops) == {"cubic": 4.0, "kilos": 700.0}
    said = routing.trouble(partner, stops)
    assert len(said) == 2 and "3.3 m3 fits" in said[0] and "650.0 kg allowed" in said[1]

    assert routing.trouble(vans["Iveco Daily"], stops) == []
    backwards = [stops[1], stops[0]]
    assert any("shuts before" in word for word in routing.trouble(vans["Iveco Daily"], backwards))


def test_route_screen_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    import importlib

    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        screen = client.get("/api/route").json()
        assert [van["name"] for van in screen["vehicles"]] == [
            "Renault Partner 1", "Renault Partner 2", "Iveco Daily"]
        assert screen["vehicles"][0]["stops"] == 0

        saved = client.post("/api/contacts", headers=mine,
                            json={"company": "bati sud", "incoterm": "DAP"}).json()
        assert saved["contacts"][0]["company"] == "BATI SUD"
        assert client.get("/api/contacts").json()["incoterms"][0]["id"] == "EXW"

        paper = tmp_path / "book.csv"
        paper.write_text("company,person,phone\nprolians,M. Sauret,05 99 00 00 03\n")
        read = client.post(f"/api/contacts/load?path={paper}", headers=mine).json()
        assert (read["added"], read["updated"]) == (1, 0)
        assert "PROLIANS" in [row["company"] for row in read["contacts"]]
        assert client.post("/api/contacts/load?path=/no/such/book.csv",
                           headers=mine).status_code == 404

        waiting = client.get("/api/backorders").json()
        assert waiting["notes"] == [] and waiting["summary"]["lines"] == 0

        assert client.delete(f"/api/contacts/{saved['contact_id']}",
                             headers=mine).status_code == 200


def test_what_has_shipped_is_only_taken_off_the_shelf_once(db_path):
    """Net flow once read as on hand minus every order ever, shipped ones included."""
    from inventory import ddmrp, stock

    db.save_note(db_path, make_note(delivery_date="2026-01-05", order_date="2026-01-02",
                                    lines=[Line(article="SPIT FIX", quantity=1000)]),
                 "sha-flow-1", "", "ALEX")
    db.save_purchase_order(db_path, {"list_date": "2026-01-09", "site": "Villa Project",
                                     "lines": [{"article": "SPIT FIX", "quantity": 400}]},
                           "sha-flow-2", "", "ALEX")

    assert stock.on_hand_map(db_path)["SPIT FIX"] == 600
    rows, _chain, _window = ddmrp.buffers(db_path)
    line = next(row for row in rows if row["article"] == "SPIT FIX")
    # Six hundred, not two hundred: the four hundred left the shelf, it did not leave twice.
    assert line["open_demand"] == 0
    assert line["net_flow"] == 600
    assert line["buffer_share"] > 0


def test_an_order_that_has_not_gone_out_yet_still_counts_as_demand(db_path):
    from inventory import ddmrp

    today = date.today()
    db.save_note(db_path, make_note(delivery_date=(today - timedelta(days=30)).isoformat(),
                                    order_date=(today - timedelta(days=40)).isoformat(),
                                    lines=[Line(article="SPIT FIX", quantity=1000)]),
                 "sha-today-1", "", "ALEX")
    db.save_purchase_order(db_path, {"list_date": today.isoformat(), "site": "Villa Project",
                                     "lines": [{"article": "SPIT FIX", "quantity": 250}]},
                           "sha-today-2", "", "ALEX")

    rows, _chain, _window = ddmrp.buffers(db_path)
    line = next(row for row in rows if row["article"] == "SPIT FIX")
    assert line["open_demand"] == 250
    assert line["net_flow"] == line["on_hand"] - 250


def test_the_ddmrp_screen_is_named_on_the_menu_bar():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    bar = page[page.index('<nav class="menubar"'):page.index("</nav>")]
    assert 'id="menu-ddmrp"' in bar and ">DDMRP<" in bar
    # The menu that used to live inside the screen now hangs off that name.
    assert 'id="ddmrp-tools"' not in page
    assert 'id="tools-drop"' in bar


def test_the_menu_bar_ends_with_system_and_help():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    bar = page[page.index('<nav class="menubar"'):page.index("</nav>")]
    # The gap pushes the last two names to the far end, over Remember me and Exit.
    assert bar.index('class="menubar-gap"') < bar.index('id="system-menu"') < bar.index('id="menu-help"')


def test_the_tool_row_keeps_nothing_but_remember_me_and_exit():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    row = page[page.index('<div class="toolbar">'):page.index('<div class="workspace">')]
    for gone in ['id="command"', 'id="tool-refresh"', 'id="tool-find"',
                 'id="tool-export"', 'id="tool-planner"']:
        assert gone not in row
    assert 'id="lock-form"' in row and 'id="quit-form"' in row


def test_the_inventory_name_rolls_its_three_cards_down():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    bar = page[page.index('<nav class="menubar"'):page.index("</nav>")]
    assert 'id="inv-drop"' in bar
    screen = page[page.index('id="panel-inventory"'):page.index('id="panel-query"')]
    for card in ["What the shelf holds", "Partly delivered", "Delivered lines"]:
        assert f"<h2>{card}</h2>" in screen


def test_the_purchase_orders_card_asks_three_things_and_no_search():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    card = page[page.index('id="stored-po"'):page.index('id="panel-quotes"')]
    assert "<h2>Purchase orders</h2>" in card
    for pick in ['id="o-site"', 'id="o-work"', 'id="o-state"']:
        assert pick in card
    assert 'id="o-search"' not in page
    # The same orders sit behind the Purchase orders line, so the menu drops its double.
    assert '"Stored PO"' not in page


def test_the_tree_folds_and_carries_a_quote_and_a_dots_order():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    tree = page[page.index('<aside class="tree"'):page.index("</aside>")]
    assert 'id="tree-toggle"' in tree and 'id="tree-list"' in tree
    assert 'id="tree-new-quote"' in tree and 'id="tree-plutus"' in tree
    # The sites hang off the Purchase orders menu now, not off the tree.
    assert 'id="tree-sites"' not in page


# ---- the gate on what the bot buys ----------------------------------------

def a_basket(**kw):
    base = dict(article="CHEVILLE SPIT FIX", quantity=100, unit_price=0.62,
                supplier="FOUSSIER")
    base.update(kw)
    return base


def test_a_price_is_remembered_and_the_usual_one_is_the_middle(db_path):
    from inventory import prices

    for price in (0.60, 0.62, 9.90):
        prices.remember(db_path, "CHEVILLE SPIT FIX", price, supplier="FOUSSIER",
                        source="order")
    # The middle, not the mean: one fat-fingered price must not move the usual one.
    assert prices.usual(db_path, "CHEVILLE SPIT FIX") == 0.62
    assert prices.usual(db_path, "NOTHING HERE") is None
    assert prices.remember(db_path, "BAD", None) is False
    assert prices.remember(db_path, "BAD", -3) is False


def test_the_first_time_an_article_is_bought_the_gate_holds_it(db_path):
    from inventory import gate

    reading = gate.look(db_path, [a_basket()])
    assert reading["held"]
    assert "first-article" in reading["reasons"]
    assert reading["total"] == 62.0


def test_an_article_the_notes_already_know_goes_through(db_path):
    from inventory import gate, prices

    db.save_note(db_path, make_note(lines=[Line(article="CEMENT 35KG", quantity=40)]),
                 "a", "a.jpg")
    prices.remember(db_path, "CEMENT 35KG", 7.0, supplier="ACME SUPPLIES", source="order")

    reading = gate.look(db_path, [a_basket(article="CEMENT 35KG", quantity=2,
                                           unit_price=7.0, supplier="ACME SUPPLIES")])
    assert reading["reasons"] == []
    assert reading["held"] is False


def test_a_new_supplier_for_a_known_article_holds_it(db_path):
    from inventory import gate, prices

    db.save_note(db_path, make_note(lines=[Line(article="CEMENT 35KG", quantity=40)]),
                 "a", "a.jpg")
    prices.remember(db_path, "CEMENT 35KG", 7.0, supplier="ACME SUPPLIES", source="order")

    reading = gate.look(db_path, [a_basket(article="CEMENT 35KG", quantity=1,
                                           unit_price=7.0, supplier="SOMEONE ELSE")])
    assert reading["reasons"] == ["new-supplier"]


def test_a_dear_line_and_a_basket_over_the_ceiling_hold_it(db_path):
    from inventory import gate, prices

    db.save_note(db_path, make_note(lines=[Line(article="CEMENT 35KG", quantity=40)]),
                 "a", "a.jpg")
    for _ in range(3):
        prices.remember(db_path, "CEMENT 35KG", 7.0, supplier="ACME SUPPLIES", source="order")

    dear = gate.look(db_path, [a_basket(article="CEMENT 35KG", quantity=1, unit_price=20.0,
                                        supplier="ACME SUPPLIES")])
    assert "dear-line" in dear["reasons"]

    plenty = gate.look(db_path, [a_basket(article="CEMENT 35KG", quantity=100, unit_price=7.0,
                                          supplier="ACME SUPPLIES")])
    assert "over-ceiling" in plenty["reasons"]
    assert plenty["total"] == 700.0


def test_a_line_with_no_price_cannot_be_checked_so_it_is_held(db_path):
    from inventory import gate

    reading = gate.look(db_path, [a_basket(unit_price=None)])
    assert "no-price" in reading["reasons"]
    assert reading["unpriced"] == 1


def test_the_gate_log_is_sealed_and_an_edit_breaks_it(db_path):
    from inventory import gate

    reading = gate.look(db_path, [a_basket()])
    request_id = gate.ask(db_path, reading, channel="42", shop="foussier.fr", asked_by="ALEX")
    gate.record_check(db_path, request_id, "same", "Terminus knows this one")
    gate.approve(db_path, request_id, "ALEX")

    whole, broken, entries = gate.verify_chain(db_path)
    assert whole and broken is None and entries == 3

    db.write(db_path, "UPDATE gate_log SET body = ? WHERE entry_id = 2", ('{"verdict":"new"}',))
    whole, broken, entries = gate.verify_chain(db_path)
    assert whole is False and broken == 2


def test_only_an_approved_request_may_be_ordered(db_path):
    from inventory import gate

    reading = gate.look(db_path, [a_basket()])
    request_id = gate.ask(db_path, reading, asked_by="ALEX")
    allowed, why = gate.may_order(db_path, request_id)
    assert allowed is False and "not approved" in why

    gate.approve(db_path, request_id, "ALEX")
    assert gate.may_order(db_path, request_id)[0] is True

    # A price that has moved since the yes has to be asked again, however old the yes is.
    moved = [a_basket(unit_price=0.90)]
    allowed, why = gate.may_order(db_path, request_id, moved)
    assert allowed is False and "approved again" in why


def test_a_cancelled_request_cannot_be_ordered_and_leaves_its_own_entry(db_path):
    from inventory import gate

    request_id = gate.ask(db_path, gate.look(db_path, [a_basket()]), asked_by="ALEX")
    gate.approve(db_path, request_id, "ALEX")
    gate.cancel(db_path, request_id, "ALEX", "changed my mind")

    assert gate.one(db_path, request_id)["state"] == "cancelled"
    assert gate.may_order(db_path, request_id)[0] is False
    events = [entry["event"] for entry in gate.chain(db_path, request_id)]
    assert events == ["requested", "approved", "cancelled"]
    with pytest.raises(ValueError):
        gate.mark_ordered(db_path, request_id, "ALEX")


def test_an_ordered_request_feeds_the_price_history(db_path):
    from inventory import gate, prices

    request_id = gate.ask(db_path, gate.look(db_path, [a_basket()]), asked_by="ALEX")
    gate.approve(db_path, request_id, "ALEX")
    gate.mark_ordered(db_path, request_id, "ALEX", reference="FOUSSIER 7781")

    assert gate.one(db_path, request_id)["state"] == "ordered"
    assert prices.usual(db_path, "CHEVILLE SPIT FIX") == 0.62
    # And the next basket for the same article no longer counts as a first purchase.
    assert "first-article" not in gate.look(db_path, [a_basket()])["reasons"]


def test_terminus_never_opens_the_gate_when_it_fails(db_path, monkeypatch):
    from inventory import llm, verify

    async def boom(self, prompt, schema, system=None):
        raise llm.ExtractionError("no claude here")

    monkeypatch.setattr(llm.ClaudeCodeBackend, "answer", boom)
    answer = asyncio.run(verify.check(db_path, [a_basket()]))
    assert answer["verdict"] == "unsure"
    assert "Terminus could not answer" in answer["note"]


def test_terminus_takes_the_most_doubtful_line_as_its_answer():
    from inventory import verify

    assert verify.overall([{"verdict": "same"}, {"verdict": "same"}]) == "same"
    assert verify.overall([{"verdict": "same"}, {"verdict": "new"}]) == "new"
    assert verify.overall([{"verdict": "new"}, {"verdict": "unsure"}]) == "unsure"
    assert verify.overall([]) == "unsure"


def test_the_shortlist_puts_the_nearest_known_name_first():
    from inventory import verify

    known = ["343546 - CHEVILLE SPIT FIX 3 10X95/36-26MT BTE50", "RONDELLE M8", "VIS TH 6X25"]
    near = verify.shortlist("CHEVILLE SPIT FIX 10X95", known)
    assert near and near[0].endswith("BTE50")
    assert verify.shortlist("NOTHING LIKE IT AT ALL", ["RONDELLE M8"]) == []


def test_a_basket_line_can_carry_the_price_off_the_shop_page():
    from inventory import baskets

    read = baskets.parse_items("12 vis th 6x25 @ 0.11, 4 rondelle m8, 3 cheville @ 0,62 EUR")
    assert [(row["article"], row["quantity"], row["unit_price"]) for row in read] == [
        ("VIS TH 6X25", 12.0, 0.11),
        ("RONDELLE M8", 4.0, None),
        ("CHEVILLE", 3.0, 0.62),
    ]


def test_the_landing_page_carries_the_approval_window():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    home = page[page.index('id="panel-home"'):page.index('id="panel-capture"')]
    assert 'id="gate-card"' in home and "<h2>Waiting for you</h2>" in home
    assert 'id="gate-list"' in home


# ---- the Slack door -------------------------------------------------------

def test_the_same_words_come_out_in_slack_markup():
    from inventory import chat

    assert chat.to_slack("**Basket** (2 line(s)) for **foussier.fr**") == \
        "*Basket* (2 line(s)) for *foussier.fr*"
    # Discord's italics are Slack's italics, and must not be eaten by the bold pass.
    assert chat.to_slack("· 12 × VIS  _ALEX_") == "· 12 × VIS  _ALEX_"
    assert chat.to_slack("nothing to do here") == "nothing to do here"


def test_both_doors_say_the_same_thing(db_path):
    from inventory import chat, discord_bot

    # The formatters live in one place now; the Discord module still answers for them.
    assert discord_bot.basket_text is chat.basket_text
    assert discord_bot.purchase_text is chat.purchase_text
    assert discord_bot.allowed is chat.allowed


def test_slack_needs_both_tokens_before_it_is_ready(monkeypatch):
    from inventory import slack_bot

    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("SLACK_APP_TOKEN", "")
    assert slack_bot.status()["ready"] is False

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    assert slack_bot.status()["ready"] is False
    assert slack_bot.status()["bot_token_set"] is True

    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_CHANNELS", "C01, C02")
    now = slack_bot.status()
    assert now["channels"] == ["C01", "C02"]
    assert now["ready"] is now["installed"]


def test_the_slack_door_builds_its_commands(tmp_path, monkeypatch):
    pytest.importorskip("slack_bolt")
    from inventory import slack_bot

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    app = slack_bot.build_app(make_config(tmp_path), ["C01"])
    # Building must not reach out to Slack: no network, no token check.
    assert app is not None


def test_a_slack_dialog_carries_the_fields_discord_had(monkeypatch):
    pytest.importorskip("slack_bolt")
    from inventory import slack_bot

    view = slack_bot.basket_dialog("C01")
    assert view["callback_id"] == slack_bot.BASKET_VIEW
    assert view["private_metadata"] == "C01"
    assert [block["block_id"] for block in view["blocks"]] == \
        ["shop", "items", "site", "work_item", "supplier"]
    # Only the shop and the items are asked for; the rest may be left alone.
    assert [block["optional"] for block in view["blocks"]] == [False, False, True, True, True]


def test_reading_one_field_out_of_a_submitted_dialog():
    from inventory import slack_bot

    view = {"state": {"values": {"items": {"value": {"value": "12 vis th 6x25 @ 0.11"}},
                                 "site": {"value": {"value": None}}}}}
    assert slack_bot.typed(view, "items") == "12 vis th 6x25 @ 0.11"
    assert slack_bot.typed(view, "site") == ""
    assert slack_bot.typed(view, "nothing here") == ""


# ---- signing in to Outlook with a token ------------------------------------

def test_the_mailbox_counts_as_set_up_when_it_has_a_token_instead_of_a_password(tmp_path):
    from inventory.config import MailConfig

    base = dict(host="outlook.office365.com", port=993, user="stock@company.com",
                folder="INBOX", security="ssl", insecure=False, mark_read=True,
                every_minutes=15, provider="outlook")
    assert MailConfig(password="", **base).enabled is False
    assert MailConfig(password="hunter2", **base).enabled is True
    # Microsoft takes a token where everyone else still takes a password.
    assert MailConfig(password="", oauth=True, **base).enabled is True


def test_the_xoauth2_string_is_the_one_microsoft_asks_for():
    from inventory import mail_oauth

    assert mail_oauth.sasl("stock@company.com", "ey.TOKEN") == \
        "user=stock@company.com\x01auth=Bearer ey.TOKEN\x01\x01"


def test_nobody_is_signed_in_before_anybody_signs_in(tmp_path, monkeypatch):
    from inventory import mail_oauth

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    assert mail_oauth.signed_in() is False
    assert mail_oauth.who() is None

    now = mail_oauth.state()
    assert now["signed_in"] is False and now["waiting"] is False
    assert now["tenant"] == "common" and now["on"] is False
    # Without an application id there is nothing to ask Microsoft with.
    with pytest.raises(mail_oauth.OAuthError):
        mail_oauth.start()


def test_the_token_cache_is_the_owner_s_to_read_and_can_be_forgotten(tmp_path, monkeypatch):
    from inventory import mail_oauth

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    store = type("Store", (), {"has_state_changed": True,
                               "serialize": lambda self: '{"RefreshToken": {}}'})()
    mail_oauth._save(store)

    path = mail_oauth.cache_file()
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"

    assert mail_oauth.forget() is True
    assert path.exists() is False


def test_a_mailbox_with_no_sign_in_says_so_rather_than_opening(tmp_path, monkeypatch):
    from inventory import mail_oauth, mailbox
    from inventory.config import MailConfig

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("AZURE_CLIENT_ID", "a-registered-app")

    def nobody():
        raise mail_oauth.OAuthError("Nobody is signed in to Outlook yet.")

    monkeypatch.setattr(mail_oauth, "token", nobody)
    monkeypatch.setattr(mailbox.imaplib, "IMAP4_SSL",
                        lambda *a, **kw: type("Box", (), {"authenticate": lambda *x: None})())

    settings = MailConfig(host="outlook.office365.com", port=993, user="stock@company.com",
                          password="", folder="INBOX", security="ssl", insecure=False,
                          mark_read=True, every_minutes=15, provider="outlook", oauth=True)
    with pytest.raises(mailbox.MailError) as raised:
        mailbox.connect(settings)
    assert "signed in" in str(raised.value)


def test_the_mailbox_card_asks_for_the_microsoft_sign_in():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    card = page[page.index("<h2>Mailbox</h2>"):page.index("<h2>Chat bot</h2>")]
    for box in ['id="mail-oauth"', 'id="mail-client-id"', 'id="mail-tenant"',
                'id="mail-oauth-start"', 'id="mail-oauth-digits"']:
        assert box in card


# ---- the two agents -------------------------------------------------------

def test_the_app_runs_two_agents_and_says_what_each_one_does():
    from inventory import agents

    now = agents.status()
    assert {one["name"] for one in now["agents"]} == {agents.PLUTUS, agents.TERMINUS}
    # There is no key to show: the agents run the command line on this machine.
    assert "key" not in str(now).lower()


def test_plutus_asks_claude_and_hands_back_what_it_said(monkeypatch):
    from inventory import agents

    asked = {}

    async def fake(prompt, schema, system=None, model=None, timeout=agents.TIMEOUT):
        asked.update({"prompt": prompt, "schema": schema, "system": system})
        return {"lines": [{"article": "RONDELLE M8", "quantity": 500, "unit": "piece",
                           "supplier": "FOUSSIER", "reference": None,
                           "reason": "under the reorder point"}],
                "note": "one line, the thin one."}

    monkeypatch.setattr(agents, "ask", fake)
    monkeypatch.setattr(agents, "available", lambda: True)
    answer = asyncio.run(agents.draft_order({"articles": [{"article": "RONDELLE M8"}]}))

    assert answer["lines"][0]["article"] == "RONDELLE M8"
    assert "RONDELLE M8" in asked["prompt"]
    # The buyer's own instructions ride as the system prompt, not inside the question.
    assert "purchase order" in asked["system"].lower()
    assert asked["schema"] is llm.ORDER_SCHEMA


def test_plutus_says_so_when_the_command_line_is_not_there(monkeypatch):
    from inventory import agents

    monkeypatch.setattr(agents, "available", lambda: False)
    with pytest.raises(llm.ExtractionError) as raised:
        asyncio.run(agents.draft_order({"articles": []}))
    assert "claude" in str(raised.value).lower()


def test_both_agents_go_out_through_the_same_door(monkeypatch, db_path):
    from inventory import agents, verify

    seen = []

    async def fake(prompt, schema, system=None, model=None, timeout=agents.TIMEOUT):
        seen.append(schema)
        return {"lines": [], "note": "", "verdict": "new"}

    monkeypatch.setattr(agents, "ask", fake)
    monkeypatch.setattr(agents, "available", lambda: True)
    asyncio.run(verify.check(db_path, [{"article": "RONDELLE M8"}]))
    asyncio.run(agents.draft_order({"articles": []}))
    # One door, two questions: the verification schema, then the order one.
    assert seen == [verify.SCHEMA, llm.ORDER_SCHEMA]


def test_the_purchase_card_is_plutus_now():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    assert "<h2>Plutus orders</h2>" in page
    assert 'id="ask-agent"' in page and 'id="agent-note"' in page
    # The button asked for a route that does not exist, so it only ever answered 404.
    assert "/api/forecast/dotsDraft" not in page
    assert '"/api/forecast/draft"' in page


def test_an_empty_draft_is_an_answer_not_a_failure(tmp_path, monkeypatch):
    """The agent is told to leave out what is comfortable, so nothing is a real reply."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    import importlib

    from inventory import agents
    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)
        db.save_note(server.config.db_path,
                     make_note(lines=[Line(article="BOLT M6", quantity=500)]), "a", "a.jpg")

        async def nothing(planner, model=None):
            return {"lines": [], "note": "Every shelf is comfortable."}

        monkeypatch.setattr(agents, "draft_order", nothing)
        answer = client.post("/api/forecast/draft", headers=mine,
                             json={"only": ["BOLT M6"]})
        assert answer.status_code == 200
        assert answer.json()["lines"] == []
        assert "comfortable" in answer.json()["note"]


def test_an_article_typed_by_hand_finds_the_one_the_shelf_calls_it(tmp_path, monkeypatch):
    """Nobody types "343546 - CHEVILLE SPIT FIX 3 10X95/36-26MT BTE50" into a box."""
    from inventory import server as server_module

    rows = [{"article": "RONDELLE M8"}, {"article": "RONDELLE M6"},
            {"article": "343546 - CHEVILLE SPIT FIX 3 10X95"}]

    assert [row["article"] for row in server_module._named(rows, ["rondelle m8"])] == \
        ["RONDELLE M8"]
    # A part of the name is enough when nothing matches it exactly.
    assert [row["article"] for row in server_module._named(rows, ["spit fix"])] == \
        ["343546 - CHEVILLE SPIT FIX 3 10X95"]
    assert [row["article"] for row in server_module._named(rows, ["rondelle"])] == \
        ["RONDELLE M8", "RONDELLE M6"]
    assert server_module._named(rows, ["nothing like it"]) == []


def test_naming_an_article_asks_plutus_for_that_one(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    import importlib

    from inventory import agents
    from inventory import config as config_module
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)
        db.save_note(server.config.db_path,
                     make_note(lines=[Line(article="RONDELLE M8", quantity=5000)]), "a", "a.jpg")

        handed = {}

        async def spy(planner, model=None):
            handed.update(planner)
            return {"lines": [{"article": "RONDELLE M8", "quantity": 500, "unit": "piece",
                               "supplier": "ACME", "reference": None, "reason": "asked for"}],
                    "note": "the one you named."}

        monkeypatch.setattr(agents, "draft_order", spy)
        answer = client.post("/api/forecast/draft", headers=mine,
                             json={"only": ["rondelle m8"]}).json()

        # The agent is told these were named, which is not the same as being thin.
        assert handed["asked_for_by_name"] == ["rondelle m8"]
        assert [row["article"] for row in handed["articles"]] == ["RONDELLE M8"]
        assert answer["asked_for"] == ["rondelle m8"]
        assert answer["read"] == ["RONDELLE M8"]

        missing = client.post("/api/forecast/draft", headers=mine,
                              json={"only": ["unobtainium"]})
        assert missing.status_code == 422
        assert "unobtainium" in missing.json()["detail"]


def test_the_plutus_card_asks_for_an_article_too():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    card = page[page.index('id="plutus-card"'):page.index('id="ask-agent"')]
    for box in ['id="order-site"', 'id="order-work"', 'id="order-article"']:
        assert box in card


def test_the_slack_test_says_which_token_is_wrong(monkeypatch):
    from inventory import slack_bot

    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("SLACK_APP_TOKEN", "")
    assert slack_bot.check() == (False, "No token stored yet.")

    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    ok, said = slack_bot.check()
    assert ok is False and "bot token" in said

    # The two fail in different ways, and each is answered for on its own.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "")
    monkeypatch.setattr(slack_bot, "available", lambda: True)

    import slack_sdk

    monkeypatch.setattr(slack_sdk.WebClient, "auth_test",
                        lambda self: {"user": "plutus", "team": "the workshop"})
    ok, said = slack_bot.check()
    assert ok is False and "app token" in said


def test_a_slack_test_that_works_names_the_bot_and_never_the_token(monkeypatch):
    from inventory import slack_bot

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-secret")
    monkeypatch.setenv("SLACK_CHANNELS", "C01")
    monkeypatch.setattr(slack_bot, "available", lambda: True)

    import slack_sdk

    monkeypatch.setattr(slack_sdk.WebClient, "auth_test",
                        lambda self: {"user": "plutus", "team": "the workshop"})
    opened = {}

    def connections_open(self, app_token=None, **kw):
        # Slack wants the app token as an argument here, not as the client's own token.
        opened["app_token"] = app_token
        return {"url": "wss://nowhere"}

    monkeypatch.setattr(slack_sdk.WebClient, "apps_connections_open", connections_open)

    ok, said = slack_bot.check()
    assert opened["app_token"] == "xapp-secret"
    assert ok is True
    assert "plutus in the workshop" in said and "C01" in said
    assert "xoxb-secret" not in said and "xapp-secret" not in said


def test_the_slack_card_can_test_the_connection():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    assert 'id="slack-check"' in page


# ---- the blockchain screen ------------------------------------------------

def test_the_bar_carries_the_blockchain_screen():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    bar = page[page.index('<nav class="menubar"'):page.index("</nav>")]
    assert 'id="menu-chain"' in bar and ">Blockchain<" in bar
    screen = page[page.index('id="panel-chain"'):page.index('id="panel-query"')]
    assert "<h2>blockchain</h2>" in screen
    assert "<h2>Plutus draft</h2>" in screen
    # The round is drawn, with somewhere to say which step is live and a way out of it.
    assert 'id="chain-round"' in screen and 'id="chain-now"' in screen
    assert 'id="chain-doing"' in screen and 'id="chain-warn"' in screen


def test_the_chain_screen_counts_what_each_bot_has_done(db_path):
    from inventory import gate

    quiet = gate.doings(db_path)
    assert quiet["terminus"]["read"] == 0 and quiet["plutus"]["drafted"] == 0

    first = gate.ask(db_path, gate.look(db_path, [a_basket()]), asked_by="ALEX")
    gate.record_check(db_path, first, "same", "known")
    gate.approve(db_path, first, "ALEX")
    second = gate.ask(db_path, gate.look(db_path, [a_basket(article="NEW THING")]),
                      asked_by="ALEX")
    gate.record_check(db_path, second, "unsure", "cannot tell")

    now = gate.doings(db_path)
    assert now["terminus"]["read"] == 2
    assert now["terminus"]["verdicts"] == {"same": 1, "unsure": 1}
    assert now["requests"] == {"approved": 1, "checked": 1}
    # A draft is not a gate: Plutus never writes to this chain.
    assert now["plutus"]["drafted"] == 0
    assert all(entry["event"] != "drafted" for entry in gate.chain(db_path))


def test_the_chain_route_answers_with_the_log_and_the_bots(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    import importlib

    from inventory import config as config_module
    from inventory import gate
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)
        request_id = gate.ask(server.config.db_path,
                              gate.look(server.config.db_path, [a_basket()]), asked_by="ALEX")
        gate.approve(server.config.db_path, request_id, "ALEX")

        answer = client.get("/api/chain").json()
        assert answer["log"]["whole"] is True and answer["log"]["entries"] == 2
        assert len(answer["log"]["last_seal"]) == 64
        assert [row["event"] for row in answer["entries"]] == ["approved", "requested"]
        assert answer["doings"]["requests"] == {"approved": 1}
        assert {one["name"] for one in answer["agents"]["agents"]} == {"plutus", "terminus"}


def test_the_round_has_seven_steps_and_says_which_are_not_built():
    from inventory import gate

    assert [number for number, _, _ in gate.STEPS] == [1, 2, 3, 4, 5, 6, 7]
    # The names are reworded by hand often enough that pinning the prose only breaks the
    # suite; what has to hold is that every step has one, and that it is short enough to
    # sit outside a wedge.
    for _, name, _ in gate.STEPS:
        assert name.strip() and len(name) <= 40
    # Nothing here places an order with a supplier, and nothing holds money.
    assert gate.UNBUILT == (6, 7)
    for number, _, detail in gate.STEPS:
        if number in gate.UNBUILT:
            assert detail.startswith("not built")


def test_the_round_says_where_a_request_has_got_to(db_path):
    from inventory import gate

    assert gate.where_we_are(db_path)["step"] == 0

    request_id = gate.ask(db_path, gate.look(db_path, [a_basket()]), asked_by="ALEX")
    waiting = gate.where_we_are(db_path)
    assert waiting["step"] == 3 and waiting["request_id"] == request_id

    gate.record_check(db_path, request_id, "unsure", "cannot tell")
    asked = gate.where_we_are(db_path)
    assert asked["step"] == 5 and "yours" in asked["why"]

    gate.approve(db_path, request_id, "ALEX")
    # Approved and nothing places it, so the round stops at the step that is not built.
    placing = gate.where_we_are(db_path)
    assert placing["step"] == 7 and "by hand" in placing["why"]

    gate.mark_ordered(db_path, request_id, "ALEX")
    assert gate.where_we_are(db_path)["step"] == 0


def test_a_shelf_already_full_of_it_is_said_out_loud(db_path):
    from inventory import gate

    db.save_note(db_path, make_note(lines=[Line(article="RONDELLE M8", quantity=1000)]),
                 "a", "a.jpg")

    plenty = gate.dwelling(db_path, [{"article": "RONDELLE M8", "quantity": 100}])
    assert plenty and plenty[0]["times"] == 10.0

    # Asking for about what is there is not dwelling stock, it is restocking.
    assert gate.dwelling(db_path, [{"article": "RONDELLE M8", "quantity": 900}]) == []
    assert gate.dwelling(db_path, [{"article": "NOT HERE", "quantity": 5}]) == []


def test_the_command_line_is_found_even_when_the_path_misses_it(monkeypatch, tmp_path):
    """A desktop launcher does not get the shell's PATH; the app still has to find it."""
    from inventory import agents

    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    monkeypatch.setattr(agents, "LIKELY", ())
    assert agents.claude_binary() is None
    assert agents.status()["ready"] is False

    pretend = tmp_path / "claude"
    pretend.write_text("#!/bin/sh\n")
    pretend.chmod(0o755)
    monkeypatch.setattr(agents, "LIKELY", (str(pretend),))
    assert agents.claude_binary() == str(pretend)
    assert agents.status()["found_at"] == str(pretend)

    # A file that is there but cannot be run is not a command line.
    pretend.chmod(0o644)
    assert agents.claude_binary() is None


def test_the_blockchain_name_rolls_down_to_what_sits_under_the_round():
    from inventory import server as server_module

    page = server_module.PAGE.read_text()
    bar = page[page.index('<nav class="menubar"'):page.index("</nav>")]
    assert 'id="chain-drop"' in bar and ">Blockchain<" in bar

    screen = page[page.index('id="panel-chain"'):page.index('id="panel-query"')]
    assert 'id="chain-card"' in screen          # the round, always there
    assert "<h2>Held baskets</h2>" in screen
    assert "<h2>Baskets nobody has bought</h2>" in screen
    assert "<h2>Plutus draft</h2>" in screen
    assert 'id="table-approval"' in screen and 'id="table-unbought"' in screen


def test_the_chain_answers_with_what_waits_and_what_nobody_bought(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    import importlib

    from inventory import baskets
    from inventory import config as config_module
    from inventory import gate
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        request_id = gate.ask(server.config.db_path,
                              gate.look(server.config.db_path, [a_basket()]), asked_by="ALEX")
        gate.record_check(server.config.db_path, request_id, "unsure", "cannot tell")
        baskets.add(server.config.db_path, "C01",
                    baskets.parse_items("12 vis th 6x25 @ 0.11"), "foussier.fr", who="SOFIANE")

        answer = client.get("/api/chain").json()
        assert [row["request_id"] for row in answer["approval"]] == [request_id]
        assert answer["approval"][0]["agent_verdict"] == "unsure"

        # A basket is emptied when it becomes an order, so this one is unbought.
        assert [row["channel"] for row in answer["baskets"]] == ["C01"]
        assert answer["baskets"][0]["lines"] == 1

        baskets.to_purchase_order(server.config.db_path, "C01", "ALEX")
        assert client.get("/api/chain").json()["baskets"] == []


def test_a_held_basket_is_not_also_counted_as_one_nobody_bought(tmp_path, monkeypatch):
    """The two tables mean different things, and a basket belongs to one of them."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    import importlib

    from inventory import baskets
    from inventory import config as config_module
    from inventory import gate
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)

        # C99 asked and was held; C01 asked for nothing yet.
        baskets.add(server.config.db_path, "C99",
                    baskets.parse_items("3 widget neuf @ 9"), "foussier.fr", who="ALEX")
        baskets.add(server.config.db_path, "C01",
                    baskets.parse_items("4 rondelle m8 @ 0.03"), "foussier.fr", who="LUC")
        reading = gate.look(server.config.db_path,
                            baskets.lines(server.config.db_path, "C99"))
        gate.ask(server.config.db_path, reading, channel="C99", asked_by="ALEX")

        answer = client.get("/api/chain").json()
        # The held one is in flight and is answered for once, not twice.
        assert [row["channel"] for row in answer["approval"]] == ["C99"]
        assert [row["channel"] for row in answer["baskets"]] == ["C01"]


def test_red_is_only_ever_something_actually_wrong(tmp_path, monkeypatch):
    """Green is behind, yellow is the one pending, red is a fault and nothing else."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    import importlib

    from inventory import agents
    from inventory import config as config_module
    from inventory import gate
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)

    mine = {"origin": "http://testserver"}
    with TestClient(server.app) as client:
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)
        monkeypatch.setattr(agents, "claude_binary", lambda: "/somewhere/claude")

        request_id = gate.ask(server.config.db_path,
                              gate.look(server.config.db_path, [a_basket()]), asked_by="ALEX")
        gate.record_check(server.config.db_path, request_id, "same", "known")

        quiet = client.get("/api/chain").json()
        assert quiet["faults"] == {}

        # Terminus with no command line to run is a fault, and it belongs to its own step.
        monkeypatch.setattr(agents, "claude_binary", lambda: None)
        missing = client.get("/api/chain").json()
        assert "3" in missing["faults"] and "Terminus" in missing["faults"]["3"]

        # A seal that does not hold is a fault of the round, wherever it is standing.
        monkeypatch.setattr(agents, "claude_binary", lambda: "/somewhere/claude")
        db.write(server.config.db_path,
                 "UPDATE gate_log SET body = ? WHERE entry_id = 1", ('{"tampered": true}',))
        edited = client.get("/api/chain").json()
        assert edited["log"]["whole"] is False
        at = str(edited["now"]["step"])
        assert "broken" in edited["faults"][at]
