"""Tests: database."""

import pathlib
from datetime import date, datetime, timedelta

import pytest

from inventory import audit, db, quotes
from inventory.models import Line, parse_date
from helpers import a_basket, make_config, make_note


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


def test_the_shortlist_puts_the_nearest_known_name_first():
    from inventory import verify

    known = ["343546 - CHEVILLE SPIT FIX 3 10X95/36-26MT BTE50", "RONDELLE M8", "VIS TH 6X25"]
    near = verify.shortlist("CHEVILLE SPIT FIX 10X95", known)
    assert near and near[0].endswith("BTE50")
    assert verify.shortlist("NOTHING LIKE IT AT ALL", ["RONDELLE M8"]) == []


def test_reading_one_field_out_of_a_submitted_dialog():
    from inventory import slack_bot

    view = {"state": {"values": {"items": {"value": {"value": "12 vis th 6x25 @ 0.11"}},
                                 "site": {"value": {"value": None}}}}}
    assert slack_bot.typed(view, "items") == "12 vis th 6x25 @ 0.11"
    assert slack_bot.typed(view, "site") == ""
    assert slack_bot.typed(view, "nothing here") == ""


def test_a_new_database_is_filled_with_a_year_that_ends_today(tmp_path, monkeypatch):
    from inventory import gate, simulation

    monkeypatch.setenv("SAMPLE_DATA", "1")
    monkeypatch.setattr(simulation, "SEED_DAYS", 20)
    path = tmp_path / "inventory.duckdb"
    simulation.init(path)

    assert db.read(path, "SELECT count(*) FROM inventory_lines")[0][0] > 0
    assert db.read(path, "SELECT count(*) FROM purchase_orders")[0][0] > 0
    assert db.read(path, "SELECT count(*) FROM contacts")[0][0] > 0
    last = db.read(path, "SELECT max(delivery_date) FROM inventory_lines")[0][0]
    assert date.today() - last < timedelta(days=5)
    # The blockchain screen has a round with something on it, and its seal holds.
    assert db.read(path, "SELECT count(*) FROM gate_requests")[0][0] > 0
    assert gate.verify_chain(path)[0] is True
    # It says it was made up, so it is not taken for the firm's own paperwork.
    assert any(event["action"] == "sample.seed" for event in audit.events(path, limit=500))


def test_a_database_that_already_exists_is_never_filled_or_touched(tmp_path, monkeypatch):
    from inventory import simulation

    path = tmp_path / "inventory.duckdb"
    db.init(path)
    db.save_note(path, make_note(), "sha-own", "", "ALEX")

    monkeypatch.setenv("SAMPLE_DATA", "1")
    monkeypatch.setattr(simulation, "SEED_DAYS", 20)
    simulation.init(path)

    # Only the firm's own note: nothing made up was added to a database somebody has.
    assert db.read(path, "SELECT count(*) FROM notes")[0][0] == 1
    assert db.read(path, "SELECT count(*) FROM gate_requests")[0][0] == 0


def test_the_sample_year_can_be_switched_off_for_an_empty_start(tmp_path, monkeypatch):
    from inventory import simulation

    path = tmp_path / "inventory.duckdb"
    for word in ("0", "false", "no", "off"):
        monkeypatch.setenv("SAMPLE_DATA", word)
        assert simulation.enabled() is False
    monkeypatch.setenv("SAMPLE_DATA", "0")
    simulation.init(path)
    assert db.read(path, "SELECT count(*) FROM inventory_lines")[0][0] == 0
    monkeypatch.delenv("SAMPLE_DATA")
    assert simulation.enabled() is True      # on unless it is said no


def test_notices_appear_once_clear_when_fixed_and_are_pushed_only_when_set(db_path, monkeypatch):
    from inventory import bills as bills_module
    from inventory import prices, watch

    today = date(2026, 11, 3)
    bill = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F1", "bill_date": "2026-09-01", "due_date": "2026-10-01",
         "net": 10.0, "total": 10.0}, source="t"))
    quotes.save(db_path, {"supplier": "ACME", "needed_by": "2026-11-05"},
                [{"article": "GRAVEL", "quantity": 3, "unit": "m3"}], "T")
    for number, price in enumerate((1.0, 1.0, 1.1, 1.0, 1.9), 1):
        prices.remember(db_path, "WASHER", price, supplier="ACME", seen_on=date(2026, 8, number))

    new = watch.scan(db_path, today)
    keys = set(new)
    assert f"due:{bill}" in keys and any(k.startswith("quote:") for k in keys)
    assert "price:WASHER" in keys
    assert watch.scan(db_path, today) == []                       # the same trouble is one notice
    titles = {n["key"]: n["severity"] for n in watch.open_notices(db_path)}
    assert titles[f"due:{bill}"] == "high"

    bills_module.update(db_path, bill, {"state": "paid"})
    watch.scan(db_path, today)
    assert f"due:{bill}" not in {n["key"] for n in watch.open_notices(db_path)}

    config = make_config(db_path.parent)
    sent = []
    monkeypatch.delenv("NOTIFY_VIA", raising=False)
    assert watch.run(db_path, config, today, send=lambda *a: sent.append(a) or True)["pushed"] == []
    assert sent == []                                              # nothing is pushed unless set

    monkeypatch.setenv("NOTIFY_VIA", "slack, mail")
    monkeypatch.setenv("NOTIFY_HOUR", "8")
    got = watch.run(db_path, config, today, now=datetime(2026, 11, 3, 7, 0),
                    send=lambda channel, text, subject: sent.append((channel, text)) or True)
    assert got["pushed"] == ["slack", "mail"] and "Quote #" in sent[0][1]
    assert watch.push_new(db_path, config, send=lambda *a: True) == []     # pushed already, once
    count = len(sent)
    watch.digest(db_path, config, datetime(2026, 11, 3, 9, 0), send=lambda c, t, s: sent.append((c, t)) or True)
    assert len(sent) == count + 2 and "need you" in sent[-1][1]            # the digest, both channels
    assert watch.digest(db_path, config, datetime(2026, 11, 3, 10, 0),
                        send=lambda *a: sent.append(a) or True) == []      # but only once a day
