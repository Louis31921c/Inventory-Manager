"""Faults are planted in a quiet history, and the engine is asked to find exactly them."""
import datetime as dt

from inventory import anomalies, db, prices

TODAY = dt.date(2026, 11, 13)


def _fresh(tmp_path):
    path = tmp_path / "t.duckdb"
    db.init(path)
    return path


def _use(path, article, weekly):
    """weekly[i] is what left in the i-th week back (0 = the last 7 days)."""
    for i, quantity in enumerate(weekly):
        day = TODAY - dt.timedelta(days=7 * i + 2)
        db.save_purchase_order(path, {"site": "S", "list_date": day,
                                      "lines": [{"article": article, "quantity": quantity}]}, None, None, "u")


def _deliveries(path, supplier, days_list):
    for k, days in enumerate(days_list):          # newest first
        done = TODAY - dt.timedelta(days=5 * k + 1)
        db.write(path, "INSERT INTO inventory_lines (note_id, line_no, supplier, order_date, delivery_date, "
                       "article, quantity) VALUES (?, 1, ?, ?, ?, 'A', 1) RETURNING note_id",
                 [abs(hash((supplier, k))) % 10**6, supplier, done - dt.timedelta(days=days), done])


def _bill(path, supplier, number, total, day=TODAY):
    db.write(path, "INSERT INTO bills (supplier, number, bill_date, total) VALUES (?, ?, ?, ?) RETURNING bill_id",
             [supplier, number, day, total])


def test_a_spike_and_a_collapse_are_found_and_a_quiet_week_is_not(tmp_path):
    path = _fresh(tmp_path)
    steady = [10, 11, 9, 10, 12, 10, 9, 11, 10, 10, 11, 9]
    _use(path, "STEADY", [10] + steady)
    _use(path, "SPIKE", [60] + steady)
    _use(path, "GONE", [0] + steady)
    keys = {f["key"] for f in anomalies.consumption(path, TODAY)}
    assert "anomaly-use:SPIKE" in keys and "anomaly-use:GONE" in keys
    assert "anomaly-use:STEADY" not in keys


def test_lumpy_demand_is_not_a_fault_but_a_record_week_is(tmp_path):
    path = _fresh(tmp_path)
    lumpy = [0, 40, 0, 0, 35, 0, 0, 45, 0, 40, 0, 0]
    _use(path, "LUMPY", [40] + lumpy)               # a pallet in a week that often has one
    _use(path, "RECORD", [200] + lumpy)             # five times the biggest week ever
    keys = {f["key"] for f in anomalies.consumption(path, TODAY)}
    assert "anomaly-use:LUMPY" not in keys and "anomaly-use:RECORD" in keys


def test_a_price_is_judged_against_the_same_suppliers_history(tmp_path):
    path = _fresh(tmp_path)
    for p in (1.00, 1.01, 0.99, 1.00, 1.01, 1.00):
        prices.remember(path, "X", p, "CHEAP", seen_on=TODAY - dt.timedelta(days=30))
    for p in (1.30, 1.31, 1.29, 1.30, 1.31, 1.30):
        prices.remember(path, "X", p, "DEAR", seen_on=TODAY - dt.timedelta(days=30))
    prices.remember(path, "X", 1.30, "DEAR", seen_on=TODAY)      # normal for DEAR, odd beside CHEAP
    assert anomalies.prices(path, TODAY) == []


def test_a_slowing_supplier_is_found_and_a_steady_one_is_not(tmp_path):
    path = _fresh(tmp_path)
    usual = [5, 6, 5, 4, 5, 6, 5, 5, 4, 6]
    _deliveries(path, "SLOW", [14, 15, 13, 16] + usual)
    _deliveries(path, "STEADY", [5, 6, 5, 5] + usual)
    keys = {f["key"] for f in anomalies.lead_times(path, TODAY)}
    assert keys == {"anomaly-lead:SLOW"}


def test_a_price_typo_is_found_and_a_normal_wobble_is_not(tmp_path):
    path = _fresh(tmp_path)
    for p in (1.00, 1.02, 0.99, 1.01, 1.00, 1.01):
        prices.remember(path, "TYPO", p, "S", seen_on=TODAY - dt.timedelta(days=30))
    prices.remember(path, "TYPO", 0.10, "S", seen_on=TODAY)
    for p in (1.00, 1.02, 0.99, 1.01, 1.00, 1.03):
        prices.remember(path, "FINE", p, "S", seen_on=TODAY - dt.timedelta(days=30))
    prices.remember(path, "FINE", 1.04, "S", seen_on=TODAY)
    keys = {f["key"] for f in anomalies.prices(path, TODAY)}
    assert keys == {"anomaly-price:TYPO:S"}


def test_the_same_bill_twice_is_found_by_number_and_by_amount(tmp_path):
    path = _fresh(tmp_path)
    _bill(path, "ACME", "F-100", 500.0)
    _bill(path, "ACME", "f-100", 500.0)            # same number, other case
    _bill(path, "BOLTS", "A", 80.0)
    _bill(path, "BOLTS", "B", 80.0)                # same amount, same day
    _bill(path, "BOLTS", "C", 81.0)                # fine
    found = anomalies.duplicate_bills(path, TODAY)
    assert len(found) == 2 and all(f["severity"] == "high" for f in found)


def test_the_findings_come_out_costliest_first_and_reach_the_notices(tmp_path):
    path = _fresh(tmp_path)
    _bill(path, "ACME", "F-1", 900.0)
    _bill(path, "ACME", "F-1", 900.0)
    for p in (1.00, 1.02, 0.99, 1.01, 1.00, 1.01):
        prices.remember(path, "TYPO", p, "S", seen_on=TODAY - dt.timedelta(days=30))
    prices.remember(path, "TYPO", 0.10, "S", seen_on=TODAY)
    found = anomalies.find(path, TODAY)
    assert [f["impact"] for f in found] == sorted((f["impact"] for f in found), reverse=True)
    from inventory import watch
    watch.scan(path, TODAY)
    assert any(n["key"].startswith("anomaly-") for n in watch.open_notices(path))
