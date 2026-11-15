import datetime as dt

from inventory import db, outbound, prices


def _fresh(tmp_path):
    path = tmp_path / "t.duckdb"
    db.init(path)
    return path


def _history(path, supplier, days, n=4):
    for k in range(n):
        db.write(path, "INSERT INTO inventory_lines (note_id, line_no, supplier, order_date, delivery_date, "
                       "article, quantity) VALUES (?, 1, ?, ?, ?, 'X', 1) RETURNING note_id",
                 [abs(hash((supplier, k))) % 10**6, supplier, dt.date(2026, 1, 1),
                  dt.date(2026, 1, 1) + dt.timedelta(days=days)])


def _stock_bolts(path):
    db.write(path, "INSERT INTO inventory_lines (note_id, line_no, supplier, delivery_date, article, quantity) "
                   "VALUES (1, 1, 'S', ?, 'BOLT', 10) RETURNING note_id", [dt.date(2026, 1, 1)])


def test_cheapest_is_plain_price_without_history(tmp_path):
    path = _fresh(tmp_path)
    prices.remember(path, "x", 0.10, "A")
    prices.remember(path, "x", 0.09, "B")
    assert prices.cheapest(path, "x")["supplier"] == "B"


def test_slow_supplier_loses_a_small_price_gap(tmp_path):
    path = _fresh(tmp_path)
    prices.remember(path, "x", 0.10, "A")
    prices.remember(path, "x", 0.09, "B")
    _history(path, "A", 2)
    _history(path, "B", 30)
    best = prices.cheapest(path, "x")
    assert best["supplier"] == "A" and best["passed_over"]["supplier"] == "B"


def test_auto_dispatch_sends_only_what_the_shelf_holds(tmp_path):
    path = _fresh(tmp_path)
    _stock_bolts(path)
    list_id = db.save_purchase_order(path, {"site": "A", "lines": [
        {"article": "BOLT", "quantity": 4}, {"article": "NUT", "quantity": 2},
        {"article": "BOLT", "quantity": 7}]}, None, None, "u")
    made = outbound.auto_dispatch(path, list_id, dt.date(2026, 11, 13))
    assert [(l["article"], l["quantity"]) for l in made["lines"]] == [("BOLT", 4.0)]
    assert made["planned_on"] == "2026-11-16"
    assert outbound.auto_dispatch(path, list_id) is None


def test_no_dispatch_without_a_site_or_for_plutus(tmp_path):
    path = _fresh(tmp_path)
    _stock_bolts(path)
    plain = db.save_purchase_order(path, {"lines": [{"article": "BOLT", "quantity": 1}]}, None, None, "u")
    robot = db.save_purchase_order(path, {"site": "A", "drafter": "PLUTUS",
                                          "lines": [{"article": "BOLT", "quantity": 1}]}, None, None, "u")
    assert outbound.auto_dispatch(path, plain) is None
    assert outbound.auto_dispatch(path, robot) is None
