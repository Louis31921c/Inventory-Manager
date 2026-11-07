"""Tests: extraction."""

import asyncio
import json
import pathlib
from datetime import date, datetime

import pytest

from inventory import db, llm, vocabulary
from inventory.config import MailConfig
from inventory.models import Line
from helpers import EMPTY_NOTE, _fake_sftp, _ok, _ready_mail, gemini, legacy_database, make_config, make_note, ok_result, temp_config, webhook_config


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

    assert answer == {"messages": 1, "queued": 1, "duplicates": 0, "names": ["BL-306260.pdf"],
                      "ids": answer["ids"]}
    assert len(answer["ids"]) == 1
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


def test_the_mailbox_counts_as_set_up_when_it_has_a_token_instead_of_a_password(tmp_path):

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


def test_mailed_delivery_notes_that_read_clean_are_stored_and_the_rest_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("STOCK_USER", raising=False)

    import importlib

    from inventory import config as config_module
    from inventory import intake
    from inventory import server as server_module

    importlib.reload(config_module)
    server = importlib.reload(server_module)
    db.init(server.config.db_path)

    class Reads:
        model = "fake"

        async def extract(self, raw, media_type, known):
            if raw.endswith(b"broken"):
                return make_note(supplier="", lines=[])
            return make_note()

    server.backend = Reads()
    ids = [intake.add(server.config, raw, name, "application/pdf", source="mail")["intake_id"]
           for raw, name in ((b"%PDF good", "bon.pdf"), (b"%PDF broken", "bon2.pdf"),
                             (b"%PDF bill", "facture-octobre.pdf"))]
    ids.append(intake.add(server.config, b"a;b\n1;2\n", "lines.csv", "text/csv")["intake_id"])

    done = asyncio.run(server._read_mail_notes(ids))
    assert done == {"stored": 1, "left": 3}
    notes = db.read(server.config.db_path, "SELECT confirmed_by FROM notes")
    assert notes == [("MAIL",)]
    waiting = {row["name"]: row["note"] for row in intake.waiting(server.config)}
    assert set(waiting) == {"bon2.pdf", "facture-octobre.pdf", "lines.csv"}
    assert waiting["bon2.pdf"].startswith("mail: to fix")
    assert "bill or a quote" in waiting["facture-octobre.pdf"]
    assert intake.counts(server.config)["read"] == 1


def test_a_photo_is_kept_upright_and_small_and_a_folder_can_be_shrunk(tmp_path):
    import io

    from PIL import Image

    from inventory import photos

    big = Image.effect_noise((3000, 2000), 60).convert("RGB")
    buffer = io.BytesIO()
    big.save(buffer, "JPEG", quality=95)
    raw = buffer.getvalue()

    kept = photos.shrink(raw, ".jpg")
    assert len(kept) < len(raw) / 2
    assert max(Image.open(io.BytesIO(kept)).size) == photos.LONG_SIDE
    # Something that is not a picture, or a PDF, is kept exactly as it came.
    assert photos.shrink(b"not an image", ".jpg") == b"not an image"
    assert photos.shrink(b"%PDF-1.4", ".pdf") == b"%PDF-1.4"
    # A picture that is already small is not made bigger.
    small = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(small, "JPEG", quality=30)
    assert len(photos.shrink(small.getvalue(), ".jpg")) <= len(small.getvalue())

    (tmp_path / "a.jpg").write_bytes(raw)
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
    done = photos.shrink_folder(tmp_path)
    assert done["files"] == 1 and done["after"] < done["before"]
    assert (tmp_path / "b.pdf").read_bytes() == b"%PDF-1.4"


def test_an_old_photo_becomes_a_small_array_that_still_reads_as_a_page(tmp_path):
    import io

    from PIL import Image, ImageDraw

    from inventory import archive

    db_path = tmp_path / "a.duckdb"
    db.init(db_path)
    page = Image.new("RGB", (1500, 2000), (205, 200, 190))          # grey-ish paper, uneven light
    for y in range(0, 2000, 4):
        ImageDraw.Draw(page).line([(0, y), (1500, y)], fill=(215 - y // 40, 210 - y // 40, 200 - y // 40))
    draw = ImageDraw.Draw(page)
    for row in range(30):
        draw.text((80, 100 + row * 55), f"LINE {row} BOLT M6 x 100  23.09.2026", fill=(20, 20, 20))
    buffer = io.BytesIO()
    page.save(buffer, "JPEG", quality=92)
    raw = buffer.getvalue()

    images = tmp_path / "images"
    images.mkdir()
    shot = images / "abc.jpg"
    shot.write_bytes(raw)
    note_id = db.save_note(db_path, make_note(), "abc", str(shot), "T")
    db.write(db_path, "UPDATE notes SET confirmed_at = now() - INTERVAL 60 DAY WHERE note_id = ? "
                      "RETURNING note_id", [note_id])

    done = db.purge_photos(db_path, images, days=42)
    assert done["notes"] == 1 and not shot.exists()
    assert db.read(db_path, "SELECT image_path FROM notes WHERE note_id = ?", [note_id]) == [(None,)]
    stored = archive.summary(db_path)
    assert stored["pages"] == 1 and stored["bytes"] < len(raw) / 8        # a fraction of the photo

    back = Image.open(io.BytesIO(archive.get(db_path, "abc")))
    assert back.mode == "1" and back.size[0] <= archive.LONG_SIDE
    ink = sum(1 for px in back.convert("L").getdata() if px == 0)
    assert 0 < ink < back.size[0] * back.size[1] * 0.2                    # print, not a black page
    assert archive.get(db_path, "nope") is None
    assert archive.store(db_path, "bad", b"not a picture") is None

    # A PDF is kept as the file it is.
    pdf = images / "bill.pdf"
    pdf.write_bytes(b"%PDF-1.4 bill")
    db.write(db_path, "INSERT INTO bills (supplier, bill_date, total, image_sha256, image_path, "
                      "added_at) VALUES ('A', current_date, 1, 'pdfsha', ?, now() - INTERVAL 90 DAY) "
                      "RETURNING bill_id", [str(pdf)])
    db.purge_photos(db_path, images, days=42)
    assert pdf.exists()


def test_a_batch_of_payments_goes_to_the_bank_by_sftp_and_the_report_settles_it(db_path, tmp_path):
    from inventory import bankdrop, payments, purchasing, wallet

    key = tmp_path / "id"
    key.write_text("k")
    key.chmod(0o644)
    with pytest.raises(ValueError):
        bankdrop.save(db_path, "-oProxyCommand=x", "22", "bank", str(key), "/in", "/out")      # not a host name
    with pytest.raises(ValueError):
        bankdrop.save(db_path, "sftp.bank.example", "22", "bank", str(key), "/in", "/out")     # key readable by others
    key.chmod(0o600)
    bankdrop.save(db_path, "sftp.bank.example", "2222", "bank", str(key), "/in", "/out")
    assert bankdrop.ready(db_path) == (True, "")

    assert bankdrop.parse_status('<Document xmlns="x"><OrgnlGrpInfAndSts><GrpSts>ACCP</GrpSts></OrgnlGrpInfAndSts></Document>') == "accepted"
    assert bankdrop.parse_status("<a><GrpSts>RJCT</GrpSts></a>") == "rejected"
    assert bankdrop.parse_status("<a><GrpSts>PDNG</GrpSts></a>") == "pending"
    assert bankdrop.parse_status("not xml") == "pending"

    # A paid bill sits in the open batch; Plutus uploads it, then reads the bank's report.
    from inventory import bills as bills_module
    wallet.deposit(db_path, 500, "float", "ALEX")
    for key_, value in (("payer_name", "Chantiers"), ("payer_iban", "FR1420041010050500013M02606")):
        db.write(db_path, "INSERT INTO policy_settings (key, value) VALUES (?, ?) RETURNING key", [key_, value])
    payments.set_bank(db_path, "ACME", "GB82WEST12345698765432")
    bill = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F-1", "bill_date": "2026-10-20", "net": 120.0, "total": 120.0}, source="t"))
    made = payments.instruct(db_path, bill, 120.0)
    config = make_config(db_path.parent, mail=_ready_mail())
    purchasing.set_mode(db_path, "dry_run", "ALEX", today=date(2026, 11, 3))
    purchasing.set_mode(db_path, "live", "ALEX", today=date(2026, 11, 7))
    run = _fake_sftp(files=[])
    monday = datetime(2026, 11, 9, 10, 0)

    from inventory import charters
    charters.set_limit(db_path, "plutus", "bank_upload", 0)
    assert purchasing._bank(db_path, monday, "live", charters.limits(db_path, "plutus"), run) == 0
    assert run.calls == []                                           # switched off: nothing leaves
    charters.set_limit(db_path, "plutus", "bank_upload", 1)
    assert purchasing._bank(db_path, monday, "live", charters.limits(db_path, "plutus"), run) == 1
    command, script = run.calls[0]
    assert command[0] == "sftp" and "BatchMode=yes" in command and "StrictHostKeyChecking=yes" in command
    assert command[-1] == "bank@sftp.bank.example" and "2222" in command and script.startswith('put "')
    batch = db.read(db_path, "SELECT state, msg_id, uploaded_at IS NOT NULL FROM payment_batches")[0]
    assert batch[0] == "exported" and batch[1].startswith("STOCK-") and batch[2] is True

    # No report yet: nothing changes. An accepted report pays the bill.
    run2 = _fake_sftp(files=[])
    assert purchasing._bank(db_path, monday, "live", charters.limits(db_path, "plutus"), run2) == 0
    run3 = _fake_sftp(files=[f"/out/{batch[1]}.pain002.xml"])
    run3.data["xml"] = "<R><GrpSts>ACCP</GrpSts></R>"
    assert purchasing._bank(db_path, monday, "live", charters.limits(db_path, "plutus"), run3) == 1
    assert bills_module.one(db_path, bill)["state"] == "paid"
    assert db.read(db_path, "SELECT state, bank_status FROM payment_batches") == [("executed", "accepted")]
