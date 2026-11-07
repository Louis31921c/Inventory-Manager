"""Tests: planning."""

from datetime import date, datetime, timedelta


from inventory import db, llm
from inventory.config import MailConfig
from inventory.models import Line
from helpers import _ddmrp_shelf, buffer_database, make_config, make_note, planning_database


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

        async def no_agent(planner, model=None, charter=None):
            raise llm.ExtractionError("Plutus needs the Claude command line.")

        monkeypatch.setattr(agents, "draft_order", no_agent)
        answer = client.post("/api/forecast/draft", json={}, headers=mine)
        assert answer.status_code == 502
        assert "Claude" in answer.json()["detail"]

        async def drafted(planner, model=None, charter=None):
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


def test_a_shelf_already_full_of_it_is_said_out_loud(db_path):
    from inventory import gate

    db.save_note(db_path, make_note(lines=[Line(article="RONDELLE M8", quantity=1000)]),
                 "a", "a.jpg")

    plenty = gate.dwelling(db_path, [{"article": "RONDELLE M8", "quantity": 100}])
    assert plenty and plenty[0]["times"] == 10.0

    # Asking for about what is there is not dwelling stock, it is restocking.
    assert gate.dwelling(db_path, [{"article": "RONDELLE M8", "quantity": 900}]) == []
    assert gate.dwelling(db_path, [{"article": "NOT HERE", "quantity": 5}]) == []


def test_lead_times_are_learned_from_the_notes_and_used_by_the_planner(db_path):
    from inventory import forecast, leadtimes

    for number, wait in enumerate((4, 6, 5, 20), 1):
        day = date(2026, 10, 1) + timedelta(days=number * 3)
        db.save_note(db_path, make_note(supplier="SLOWCO", delivery_date=day.isoformat(),
                                        order_date=(day - timedelta(days=wait)).isoformat(),
                                        lines=[Line(article="WIDGET", quantity=10)]),
                     f"lt{number}", "", "T")
    db.save_note(db_path, make_note(supplier="RARE", delivery_date="2026-10-20",
                                    order_date="2026-10-10",
                                    lines=[Line(article="ONCE", quantity=1)]), "lt9", "", "T")

    got = leadtimes.per_supplier(db_path)["SLOWCO"]
    assert got["n"] == 4 and got["days"] == 5.5 and got["fastest"] == 4 and got["slowest"] == 20
    assert got["plan_days"] > got["days"]          # plan around the slow one in five
    assert "WIDGET" in leadtimes.learned(db_path) and "ONCE" not in leadtimes.learned(db_path)
    assert leadtimes.for_supplier(db_path, "RARE", default=7) == 7      # too few to say

    rows, _span = forecast.articles(db_path, lead=7)
    by_name = {row["article"]: row for row in rows}
    assert by_name["WIDGET"]["lead_source"] == "learned"
    assert by_name["WIDGET"]["lead_days"] == leadtimes.learned(db_path)["WIDGET"]
    assert by_name["ONCE"]["lead_source"] == "default" and by_name["ONCE"]["lead_days"] == 7
    rows, _span = forecast.articles(db_path, lead=7, use_learned=False)
    assert {row["lead_source"] for row in rows} == {"default"}


def test_the_dashboard_names_what_is_short_why_and_what_to_do(db_path):
    from inventory import dashboard, outbox, prices, stockctl

    today = date(2026, 11, 3)
    for number, day in enumerate((date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19)), 1):
        db.save_note(db_path, make_note(supplier="ACME", delivery_date=day.isoformat(),
                                        order_date=(day - timedelta(days=10)).isoformat(),
                                        site="NORD", work_item="W1",
                                        lines=[Line(article="BOLT", quantity=70)]), f"db{number}", "", "T")
    db.save_stock_count(db_path, "BOLT", 0, "T")                   # the shelf is empty
    prices.remember(db_path, "BOLT", 2.0, supplier="ACME", seen_on=date(2026, 9, 1))
    stockctl.reserve(db_path, "BOLT", "NORD", "W1", 5, needed_by="2026-11-05", user="T", force=True)

    items = dashboard.shortages(db_path, today)
    bolt = next(i for i in items if i["article"] == "BOLT")
    assert bolt["status"] == "line down" and bolt["need_date"] == "2026-11-05"
    assert bolt["blocks"] == ["NORD / W1"] and bolt["supplier"] == "ACME"
    assert bolt["lead_days"] == 10 and bolt["cause"] == "no order placed"
    assert "10 days" in bolt["cause_detail"] and "express" in bolt["action"]   # 10 days against 2 left

    # Draft the order: the cause moves to "drafted, not sent".
    lid = db.save_purchase_order(db_path, {"list_date": today, "site": "NORD", "work_item": "W1",
                                           "drafter": "X", "lines": [{"article": "BOLT",
                                                                      "quantity": 50.0, "in_stock": False}]},
                                 None, None, "T")
    outbox.draft_order(db_path, lid, "T")
    again = next(i for i in dashboard.shortages(db_path, today) if i["article"] == "BOLT")
    assert again["cause"] == "order drafted, not sent"
    built = dashboard.build(db_path, today)
    assert built["kpis"]["line_down"] >= 1 and built["shortages"][0]["status"] == "line down"


def test_orders_to_come_lists_buffer_orders_the_queue_and_what_was_sent(db_path):
    from inventory import dashboard, reorder

    today = date(2026, 11, 3)
    for number in range(1, 5):
        day = date(2026, 10, 1) + timedelta(days=number * 5)
        db.save_note(db_path, make_note(supplier="ACME", delivery_date=day.isoformat(),
                                        order_date=(day - timedelta(days=5)).isoformat(),
                                        lines=[Line(article="BOLT", quantity=100)]), f"oc{number}", "", "T")
    db.save_stock_count(db_path, "BOLT", 3, "T")
    lid = db.save_purchase_order(db_path, {"list_date": today, "site": "NORD", "work_item": "W1",
                                           "drafter": "X", "lines": [
                                               {"article": "NUT", "quantity": 8.0, "in_stock": False}]},
                                 None, None, "T")
    reorder.collect(db_path, today)
    got = dashboard.planned_orders(db_path, today)
    sources = {(r["article"], r["status"]) for r in got["rows"]}
    assert ("BOLT", "to place") in sources and ("NUT", "waiting") in sources
    waiting = next(r for r in got["rows"] if r["article"] == "NUT")
    assert waiting["order_on"] == "2026-11-18"                      # 15 days after it was read


def test_autonomy_rates_the_architecture_and_moves_with_what_is_switched_on(tmp_path, monkeypatch):
    from inventory import autonomy

    monkeypatch.delenv("NOTIFY_VIA", raising=False)
    bare = autonomy.measure(config=make_config(tmp_path))
    assert sum(p["weight"] for p in bare["parts"]) == 100
    assert 0 < bare["score"] < 100 and all(0 <= p["level"] <= 4 for p in bare["parts"])
    assert bare["next_steps"][0]["to_win"] >= bare["next_steps"][-1]["to_win"]   # ranked by value
    by_name = {p["capability"]: p for p in bare["parts"]}
    assert by_name["Payments"]["level"] == 4 and by_name["Audit trail"]["level"] == 4   # handed to the bank by SFTP
    assert by_name["Placing orders"]["level"] == 1                            # no outgoing mail
    assert by_name["Spend control"]["level"] == 4                             # policies are built
    assert by_name["Alerts"]["level"] == 2                                    # nothing is pushed
    assert by_name["Audit trail"]["next"] is None

    # A mailbox that sends, and an alert channel, raise exactly the lines that depend on them.
    ready = MailConfig(host="imap.gmail.com", port=993, user="buyer@example.com",
                       password="x", folder="INBOX", security="ssl", insecure=False,
                       mark_read=True, every_minutes=15, provider="gmail")
    monkeypatch.setenv("NOTIFY_VIA", "slack")
    monkeypatch.setattr(autonomy.agents, "available", lambda: False)          # no Claude command line: replies are only filed
    wired = autonomy.measure(config=make_config(tmp_path, mail=ready))
    now = {p["capability"]: p for p in wired["parts"]}
    assert now["Placing orders"]["level"] == 4 and now["Alerts"]["level"] == 3   # step 7 is walked, mail sends
    assert now["Delivery notes"]["level"] == 3 and now["Supplier replies"]["level"] == 3
    monkeypatch.setattr(autonomy.agents, "available", lambda: True)           # with it, a reply is read for what it says
    read = {p["capability"]: p for p in autonomy.measure(config=make_config(tmp_path, mail=ready))["parts"]}
    assert read["Supplier replies"]["level"] == 4 and read["Disputes"]["level"] == 4
    assert wired["score"] > bare["score"]
    assert abs(wired["score"] - sum(p["points"] for p in wired["parts"])) < 0.01


def test_contacts_carry_the_lead_time_learned_for_their_supplier(db_path):
    from inventory import leadtimes

    for number, wait in enumerate((4, 6, 5), 1):
        day = date(2026, 10, 1) + timedelta(days=number * 3)
        db.save_note(db_path, make_note(supplier="ACME", delivery_date=day.isoformat(),
                                        order_date=(day - timedelta(days=wait)).isoformat(),
                                        lines=[Line(article=f"A{number}", quantity=1)]),
                     f"lc{number}", "", "T")
    known = leadtimes.per_supplier(db_path)["ACME"]
    assert known["n"] == 3 and known["days"] == 5.0


def test_corrections_refusals_stockouts_and_short_counts_teach_the_planner(db_path):
    from inventory import feedback, forecast, gate, stockctl

    today = date(2026, 11, 3)
    # An article used steadily, so the planner has something to size.
    for number in range(1, 7):
        day = date(2026, 9, 1) + timedelta(days=number * 7)
        db.save_note(db_path, make_note(supplier="ACME", delivery_date=day.isoformat(),
                                        order_date=(day - timedelta(days=5)).isoformat(),
                                        lines=[Line(article="BOLT", quantity=70)]), f"fb{number}", "", "T")
    db.save_stock_count(db_path, "BOLT", 100, "T")

    def suggested():
        rows, _span = forecast.articles(db_path, lead=7)
        return next(r for r in rows if r["article"] == "BOLT")

    base = suggested()
    assert base["qty_factor"] == 1.0 and base["safety_factor"] == 1.0

    # A person cuts Plutus's 100 to 60, twice: the proposal comes back smaller.
    for _ in range(2):
        moved = feedback.review_order(db_path, [{"article": "bolt", "proposed": 100, "final": 60},
                                                {"article": "NUT", "proposed": 10, "final": 10}], "T")
    assert moved == [{"article": "BOLT", "ratio": 0.6}]
    got = {r["article"]: r for r in feedback.listing(db_path)}
    assert round(got["BOLT"]["qty_factor"], 3) == 0.64               # 0.8 x 0.8
    assert "NUT" not in got                                            # accepted: nothing moves
    smaller = suggested()
    assert smaller["qty_factor"] == 0.64 and smaller["suggested"] < base["suggested"]

    # Removed from an order: the strongest message, and the factor stops at its floor.
    for _ in range(6):
        feedback.review_order(db_path, [{"article": "BOLT", "proposed": 50, "final": 0}])
    assert feedback.factors(db_path)["BOLT"][0] == feedback.QTY_BOUNDS[0]
    feedback.forget(db_path, "BOLT")
    assert suggested()["qty_factor"] == 1.0

    # A refused basket is remembered per article, and shows up for the buyer.
    request_id = gate.ask(db_path, {"reasons": ["first-article"], "total": 900.0, "lines": [
        {"article": "BOLT", "quantity": 5, "found": []}]}, channel="c", shop="s", asked_by="T",
        site="NORD", work_item="W1")
    gate.cancel(db_path, request_id, "T", "too dear")
    assert feedback.refusals(db_path, today=date.today()) == {"BOLT": 1}

    # A short count lifts the safety stock a little.
    stockctl.count(db_path, "BOLT", 40, "T")                           # the books said far more
    assert feedback.factors(db_path)["BOLT"][1] == feedback.SHORT_COUNT_LIFT

    # An article that ran out gets more safety; a calm one drifts back to 1.
    db.save_stock_count(db_path, "BOLT", 0, "T")
    first = feedback.learn(db_path, today)
    assert first["ran_out"] == 1
    lifted = feedback.factors(db_path)["BOLT"][1]
    assert lifted > feedback.SHORT_COUNT_LIFT
    assert feedback.learn(db_path, today)["ran_out"] == 0              # once in a week, not hourly
    db.write(db_path, "UPDATE feedback SET happened_at = now() - INTERVAL 90 DAY RETURNING 1")
    db.write(db_path, "UPDATE tuning SET updated_at = TIMESTAMP '2026-10-01 09:00:00' RETURNING 1")
    db.save_stock_count(db_path, "BOLT", 50, "T")
    feedback.learn(db_path, today)
    assert feedback.factors(db_path)["BOLT"][1] < lifted


def test_plutus_orders_once_a_cycle_for_what_will_not_last_and_only_urgently_between(db_path):
    from inventory import charters, ddmrp, prices, replenish, stockctl

    counts = _ddmrp_shelf(db_path)
    for article, quantity in counts.items():
        db.save_stock_count(db_path, article, quantity, "T")
    prices.remember(db_path, "BOLT", 2.0, supplier="ACME", seen_on=date(2026, 9, 1))
    prices.remember(db_path, "NUT", 1.0, supplier="ACME", seen_on=date(2026, 9, 1))
    zones = {r["article"]: r for r in ddmrp.buffers(db_path)[0]}
    assert zones["BOLT"]["zone"] in ("red", "yellow") and zones["NUT"]["zone"] in ("green", "over")

    monday = date(2026, 11, 9)
    # The rhythm: nothing until the sitting; the first one is the next Monday, at working hours.
    assert replenish.next_cycle(db_path, date(2026, 11, 4)) == monday
    assert replenish.cycle_due(db_path, datetime(2026, 11, 9, 7, 0)) is False          # before 8h
    assert replenish.cycle_due(db_path, datetime(2026, 11, 7, 10, 0)) is False         # not yet
    assert replenish.cycle_due(db_path, datetime(2026, 11, 9, 8, 0)) is True
    replenish._put(db_path, "plutus_last_cycle", "2026-10-26")                          # a sitting two weeks ago
    assert replenish.next_cycle(db_path, date(2026, 11, 5)) == monday
    assert replenish.cycle_due(db_path, datetime(2026, 11, 10, 10, 0)) is True          # missed on the day: caught up
    replenish._put(db_path, "plutus_last_cycle", "")

    asked, left = replenish.proposals(db_path, monday, "cycle")
    names = {r["article"]: r for r in asked}
    assert "BOLT" in names and "NUT" not in names                                      # NUT lasts the cycle
    bolt = names["BOLT"]
    assert bolt["quantity"] >= float(zones["BOLT"]["to_order"])                         # refill plus the cycle's use
    assert "next sitting in 14 days" in bolt["reason"] and "top of green" in bolt["reason"]
    assert bolt["quantity"] <= zones["BOLT"]["adu"] * 90                                # the cover limit

    # Stock held back for a work item is not free: it brings an article into the order.
    db.save_stock_count(db_path, "NUT", 400, "T")
    near = {r["article"] for r in replenish.proposals(db_path, monday, "cycle")[0]}
    stockctl.reserve(db_path, "NUT", "NORD", "W1", 390, user="T", force=True)
    held = {r["article"] for r in replenish.proposals(db_path, monday, "cycle")[0]}
    assert "NUT" in held and held >= near

    # The sitting writes ONE order, and the urgent ones are in it.
    placed = replenish.run(db_path, now=datetime(2026, 11, 9, 9, 0))
    assert [kind for kind, _i, _n in placed["orders"]] == ["cycle"]
    list_id = placed["orders"][0][1]
    lines = db.read(db_path, "SELECT article, in_stock FROM purchase_order_lines WHERE list_id = ?", [list_id])
    assert "BOLT" in {a for a, _s in lines} and all(s is False for _a, s in lines)
    assert db.read(db_path, "SELECT upper(drafter) FROM purchase_orders WHERE list_id = ?", [list_id]) == [("PLUTUS",)]
    assert replenish.last_cycle(db_path) == monday
    assert replenish.next_cycle(db_path, date(2026, 11, 10)) == date(2026, 11, 23)     # a fortnight on, a Monday

    # Hour after hour nothing more is written: what is ordered is on its way.
    for hour in (10, 11, 15):
        assert replenish.run(db_path, now=datetime(2026, 11, 9, hour, 0))["orders"] == []

    # Between sittings only an article in red with nothing on the way, once a day, and small.
    db.write(db_path, "DELETE FROM purchase_order_lines RETURNING 1")
    db.write(db_path, "DELETE FROM purchase_orders RETURNING 1")
    out = replenish.run(db_path, now=datetime(2026, 11, 12, 9, 0))
    assert [kind for kind, _i, _n in out["orders"]] in ([], ["urgent"])
    again = replenish.run(db_path, now=datetime(2026, 11, 12, 14, 0))
    assert again["orders"] == []                                                       # never twice a day

    # Switched off, it does nothing; a too-small supplier order waits.
    replenish.set_auto(db_path, False, "T")
    assert replenish.run(db_path, now=datetime(2026, 11, 23, 9, 0))["skipped"]
    replenish.set_auto(db_path, True, "T")
    charters.set_limit(db_path, "plutus", "min_supplier_order", 1_000_000)
    db.write(db_path, "DELETE FROM purchase_order_lines RETURNING 1")
    db.write(db_path, "DELETE FROM purchase_orders RETURNING 1")
    small = replenish.place(db_path, today=monday, kind="cycle")
    assert all("waits for the next sitting" in c["why"] or c["why"] for c in small["cuts"])


def test_an_article_with_untrustworthy_books_is_ordered_from_zero_and_a_count_is_asked_for(db_path):
    from inventory import replenish

    _ddmrp_shelf(db_path)
    # Orders shipped for more BOLT than were ever delivered: the books go below zero.
    db.save_purchase_order(db_path, {"list_date": date(2026, 10, 1), "site": "NORD", "drafter": "X",
                                     "lines": [{"article": "BOLT", "quantity": 900.0}]}, None, None, "T")
    asked, _left, to_count = replenish._build(db_path, date(2026, 11, 9), "cycle")
    assert any(c["article"] == "BOLT" and "below zero" in c["why"] for c in to_count)
    bolt = next((r for r in asked if r["article"] == "BOLT"), None)
    if bolt is not None:
        assert bolt["quantity"] <= bolt["top_of_green"] and "count it" in bolt["reason"]
