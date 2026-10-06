import json
import pathlib
from datetime import date
from types import SimpleNamespace

import pytest

from inventory import audit, db, llm, vocabulary
from inventory.config import Config, SmsConfig
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
    base = dict(backend="claude", gemini_api_key="", photo_retention_days=42,
                data_dir=tmp_path, model="", user="ALEX", sms=sms)
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
    assert csv_lines[0] == ("article;quantity;supplier;delivery_date;weekday;order_date;site;"
                            "work_item;backorder;designation;note_id")
    assert csv_lines[1].startswith("CEMENT 35KG;40.0;ACME SUPPLIES;2026-09-21;Monday;")
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
    for dataset in db.EXPORTS:
        columns, rows = db.export_query(db_path, dataset)
        assert columns, dataset
        for suffix in (".csv", ".parquet"):
            out = tmp_path / (dataset + suffix)
            db.export_to_file(db_path, dataset, out)
            assert out.stat().st_size > 0, (dataset, suffix)
    assert "article" in db.export_query(db_path, "inventory")[0]
    assert "aliases" in db.export_query(db_path, "vocabulary")[0]
    assert "user_name" in db.export_query(db_path, "audit")[0]


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
    from inventory.config import Config, SmsConfig

    sms = SmsConfig(provider="none", to="", sender="", account_sid="", auth_token="",
                    webhook_url="", webhook_token="", max_per_hour=6)
    return Config(backend="claude", gemini_api_key="", photo_retention_days=42, data_dir=tmp_path,
                  model="", user="ALEX", sms=sms)


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
        assert "first run" in client.get("/unlock").text

        assert client.post("/unlock", data={"user": "alex", "password": "x"},
                           headers={"origin": "http://evil.example"}).status_code == 403
        assert client.post("/unlock", data={"user": "alex", "password": "x"}).status_code == 403

        answer = client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                              "again": "nope"}, headers=mine,
                             follow_redirects=False)
        assert "match" in answer.text
        answer = client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                              "again": "correct horse"}, headers=mine,
                             follow_redirects=False)
        assert answer.status_code == 303
        assert client.get("/api/data").json()["user"] == "ALEX"

        assert client.post("/lock", headers=mine, follow_redirects=False).status_code == 303
        assert client.get("/api/data").status_code == 401
        assert "SIGN IN" in client.get("/unlock").text

        assert "Wrong password" in client.post("/unlock", data={"password": "guess"},
                                               headers=mine).text
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
    rows = {row["id"]: row for row in models_info.status("claude")}
    assert set(rows) == {"claude", "gemini", "openai"}
    assert rows["claude"]["current"] is True
    assert rows["openai"]["state"] == "unsupported"

    monkeypatch.setenv("GEMINI_API_KEY", "key")
    gemini = {row["id"]: row for row in models_info.status("gemini")}["gemini"]
    assert gemini["state"] in ("ready", "configured") and gemini["current"] is True

    ok, detail = models_info.probe("openai")
    assert ok is False and "not built" in detail

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
