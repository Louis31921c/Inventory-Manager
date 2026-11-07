"""Tests: stock."""

from datetime import date, timedelta

import pytest

from inventory import db
from inventory.models import Line
from helpers import _ticked_list, make_note


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


def test_a_missing_line_settles_itself_when_a_later_note_brings_it(db_path):
    from inventory import backorders

    first = make_note(lines=[Line(article="MESH ST25", quantity=5, backorder=True),
                             Line(article="CEMENT 35KG", quantity=40, backorder=True)])
    db.save_note(db_path, first, "sha-first", "", "ALEX")
    assert backorders.summary(db_path)["lines"] == 2

    later = make_note(lines=[Line(article="MESH ST25", quantity=5)])
    db.save_note(db_path, later, "sha-later", "", "ALEX")

    waiting = backorders.rows(db_path)
    assert [line["article"] for line in waiting] == ["CEMENT 35KG"]
    assert backorders.rows(db_path, settled=True)[0]["article"] == "MESH ST25"


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


def test_a_line_ticked_to_order_waits_fifteen_days_then_is_ordered(db_path):
    from inventory import reorder

    list_id = _ticked_list(db_path, [False, True, False, None])
    day = date(2026, 11, 3)
    assert reorder.collect(db_path, day) == {"added": 2, "dropped": 0}
    assert reorder.collect(db_path, day) == {"added": 0, "dropped": 0}   # once each
    waiting = reorder.listing(db_path, day)
    assert waiting["counts"] == {"waiting": 2, "due": 0, "ordered": 0}
    assert {row["due_on"] for row in waiting["rows"]} == {"2026-11-18"}

    # Nothing is ordered early.
    assert reorder.place_due(db_path, day + timedelta(days=14))["orders"] == []
    assert db.read(db_path, "SELECT count(*) FROM purchase_orders")[0][0] == 1

    # One of the two is ticked back to in stock: it leaves the queue.
    db.set_in_stock(db_path, list_id, 3, True)
    assert reorder.collect(db_path, day + timedelta(days=1))["dropped"] == 1

    done = reorder.place_due(db_path, day + timedelta(days=15))
    assert [(o["site"], o["work_item"], o["lines"]) for o in done["orders"]] == [("NORD", "W1", 1)]
    made = db.read(db_path, "SELECT drafter FROM purchase_orders WHERE list_id <> ?", [list_id])
    assert len(made) == 1 and made[0][0].upper() == "PLUTUS"
    assert reorder.listing(db_path, day)["counts"] == {"waiting": 0, "due": 0, "ordered": 1}
    # Ordered lines are not read a second time.
    assert reorder.collect(db_path, day + timedelta(days=16))["added"] == 0


def test_an_order_is_checked_against_what_came_in(db_path):
    from inventory import matching

    today = date(2026, 11, 3)
    list_id = db.save_purchase_order(db_path, {
        "list_date": date(2026, 10, 20), "site": "NORD", "work_item": "W1", "drafter": "Plutus",
        "lines": [{"article": "BOLT", "quantity": 10.0}, {"article": "NUT", "quantity": 4.0}]},
        None, None, "T")
    db.save_note(db_path, make_note(supplier="ACME", delivery_date="2026-10-25",
                                    order_date="2026-10-20", site="NORD", work_item="W1",
                                    lines=[Line(article="BOLT", quantity=10)]), "o1", "", "T")
    found = matching.orders(db_path, today)
    held = found["orders"][0]
    assert held["list_id"] == list_id and held["state"] == "part delivered"
    states = {line["article"]: line["state"] for line in held["lines"]}
    assert states == {"BOLT": "complete", "NUT": "not delivered"}


def test_stock_control_moves_reserves_adjusts_and_counts(db_path):
    from inventory import stock, stockctl

    day = date.today() - timedelta(days=10)
    db.save_note(db_path, make_note(supplier="ACME", delivery_date=day.isoformat(),
                                    order_date=day.isoformat(), site="NORD", work_item="W1",
                                    lines=[Line(article="BOLT", quantity=100)]), "c1", "", "T")

    def row(site):
        return next(r for r in stockctl.site_stock(db_path)
                    if r["article"] == "BOLT" and r["site"] == site)

    assert row("NORD")["available"] == 100

    stockctl.transfer(db_path, "bolt", "nord", "sud", 30, "T")
    assert (row("NORD")["on_hand"], row("SUD")["on_hand"]) == (70, 30)
    with pytest.raises(ValueError):
        stockctl.transfer(db_path, "bolt", "nord", "sud", 500)
    with pytest.raises(ValueError):
        stockctl.transfer(db_path, "bolt", "nord", "nord", 1)

    held = stockctl.reserve(db_path, "bolt", "nord", "w1", 50, user="T")
    assert (row("NORD")["reserved"], row("NORD")["available"]) == (50, 20)
    with pytest.raises(ValueError):
        stockctl.reserve(db_path, "bolt", "nord", "w2", 30)

    # An order for that work item uses the reservation up.
    db.save_purchase_order(db_path, {"list_date": date.today(), "site": "NORD",
                                     "work_item": "W1", "drafter": "X",
                                     "lines": [{"article": "BOLT", "quantity": 20.0}]},
                           None, None, "T")
    assert stockctl.reservations(db_path)[0]["left"] == 30
    assert stockctl.release(db_path, held["reservation_id"]) is True
    assert stockctl.reservations(db_path) == []

    with pytest.raises(ValueError):
        stockctl.adjust(db_path, "bolt", "nord", -5, " ")
    stockctl.adjust(db_path, "bolt", "nord", -5, "broken in the van", "T")
    assert row("NORD")["on_hand"] == 65
    assert stock.on_hand_map(db_path)["BOLT"] == 95      # the shelf as a whole, 100 - 5

    # A count replaces the sum, and what is adjusted afterwards is added back.
    done = stockctl.count(db_path, "bolt", 90, "T")
    assert (done["expected"], done["variance"]) == (95.0, -5.0)
    assert stock.on_hand_map(db_path)["BOLT"] == 90
    assert stockctl.moves(db_path)[0]["kind"] == "count"


def test_the_cycle_count_asks_for_busy_and_never_counted_articles_first(db_path):
    from inventory import stockctl

    today = date(2026, 11, 3)
    for article, shipped in (("FAST", 90), ("SLOW", 1), ("DEAD", 0)):
        db.save_note(db_path, make_note(delivery_date="2026-10-01", order_date="2026-09-20",
                                        lines=[Line(article=article, quantity=100)]),
                     f"cy-{article}", "", "T")
    db.save_purchase_order(db_path, {"list_date": date(2026, 10, 15), "site": "NORD",
                                     "drafter": "X", "lines": [
                                         {"article": "FAST", "quantity": 90.0},
                                         {"article": "SLOW", "quantity": 1.0}]},
                           None, None, "T")
    asked = stockctl.cycle_due(db_path, today)
    assert [row["article"] for row in asked["due"]][0] == "FAST"
    assert {row["article"] for row in asked["due"]} == {"FAST", "SLOW", "DEAD"}
    assert asked["due"][0]["class"] == "A"
    db.save_stock_count(db_path, "FAST", 10, "T")
    db.write(db_path, "UPDATE stock_counts SET counted_on = ?", (date(2026, 10, 25),))
    again = stockctl.cycle_due(db_path, today)
    assert "FAST" not in {row["article"] for row in again["due"]}      # counted 9 days ago


def test_stock_control_routes_refuse_in_words_and_answer_with_the_new_picture(tmp_path, monkeypatch):
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
        client.post("/unlock", data={"user": "alex", "password": "correct horse",
                                     "again": "correct horse"}, headers=mine)
        db.save_note(server.config.db_path, make_note(
            delivery_date="2026-10-01", order_date="2026-09-20", site="NORD",
            lines=[Line(article="BOLT", quantity=50)]), "r1", "", "T")

        refused = client.post("/api/stockctl/transfer", headers=mine, json={
            "article": "bolt", "from_site": "nord", "to_site": "sud", "quantity": 80})
        assert refused.status_code == 422 and "free" in refused.json()["detail"]

        done = client.post("/api/stockctl/transfer", headers=mine, json={
            "article": "bolt", "from_site": "nord", "to_site": "sud", "quantity": 20}).json()
        sites = {row["site"]: row["on_hand"] for row in done["control"]["sites"]}
        assert sites == {"NORD": 30, "SUD": 20}

        held = client.post("/api/stockctl/reserve", headers=mine, json={
            "article": "bolt", "site": "nord", "work_item": "w1", "quantity": 10}).json()
        rid = held["reservation_id"]
        assert client.get("/api/stockctl").json()["summary"]["reserved"] == 10
        gone = client.delete(f"/api/stockctl/reservations/{rid}", headers=mine)
        assert gone.json()["control"]["summary"]["reserved"] == 0
        assert client.delete(f"/api/stockctl/reservations/{rid}",
                             headers=mine).status_code == 404
        assert client.post("/api/stockctl/adjust", headers=mine, json={
            "article": "bolt", "site": "nord", "quantity": -1}).status_code == 422


def test_a_site_request_holds_back_stock_orders_the_rest_and_reports_its_state(db_path):
    from inventory import gate, requisitions, stockctl

    db.save_note(db_path, make_note(supplier="ACME", site="NORD", work_item="W0", delivery_date="2026-10-01",
                                    lines=[Line(article="BOLT", quantity=6)]), "r1", "", "T")
    asked = requisitions.submit(db_path, "marc", "nord", "w1", "2026-11-20",
                                [{"article": "bolt", "quantity": 10}, {"article": "ODDITY", "quantity": 2}],
                                check_names=False)
    bolt, oddity = asked["lines"]
    assert bolt["reserved"] == 6 and bolt["to_order"] == 4 and bolt["state"] == "waiting"
    assert oddity["reserved"] == 0 and oddity["to_order"] == 2
    assert stockctl.reservations(db_path)[0]["quantity"] == 6
    request = gate.one(db_path, asked["gate_request_id"])
    assert request["channel"] == f"req:{asked['req_id']}" and request["site"] == "NORD"
    assert {l["article"]: l["quantity"] for l in request["lines"]} == {"BOLT": 4.0, "ODDITY": 2.0}

    gate.approve(db_path, asked["gate_request_id"], "ALEX")
    assert requisitions.status(db_path, asked["req_id"])["lines"][0]["state"] == "approved"
    gate.mark_ordered(db_path, asked["gate_request_id"], "PLUTUS", reference="PO #1")
    now = requisitions.status(db_path, asked["req_id"], date(2026, 11, 25))
    assert now["lines"][0]["state"] == "ordered" and now["late"] is True and now["summary"].startswith("LATE")

    # The goods arrive on delivery notes for that site and work item: lines turn delivered, then done.
    today = date.today().isoformat()
    db.save_note(db_path, make_note(supplier="ACME", site="NORD", work_item="W1", delivery_date=today,
                                    lines=[Line(article="BOLT", quantity=4), Line(article="ODDITY", quantity=2)]),
                 "r2", "", "T")
    assert [l["state"] for l in requisitions.status(db_path, asked["req_id"])["lines"]] == ["delivered", "delivered"]
    finished = requisitions.refresh(db_path)
    assert [r["req_id"] for r in finished] == [asked["req_id"]] and finished[0]["state"] == "done"
    assert requisitions.listing(db_path) == [] and requisitions.refresh(db_path) == []

    # Everything on the shelf: held back, nothing ordered, done at once. Cancelling releases it.
    db.save_note(db_path, make_note(supplier="ACME", site="NORD", work_item="W9", delivery_date=today,
                                    lines=[Line(article="BOLT", quantity=5)]), "r3", "", "T")
    easy = requisitions.submit(db_path, "marc", "nord", "w2", None, [{"article": "BOLT", "quantity": 3}],
                               check_names=False)
    assert easy["state"] == "done" and easy["gate_request_id"] is None
    with pytest.raises(ValueError):
        requisitions.submit(db_path, "marc", "", "w2", None, [{"article": "BOLT", "quantity": 3}])
    with pytest.raises(ValueError):
        requisitions.submit(db_path, "marc", "nord", "w2", None, [])
    pending = requisitions.submit(db_path, "marc", "nord", "w3", None, [{"article": "NEWTHING", "quantity": 1}],
                                  check_names=False)
    gone = requisitions.cancel(db_path, pending["req_id"], "marc")
    assert gone["state"] == "cancelled" and gate.one(db_path, pending["gate_request_id"])["state"] == "cancelled"


def test_a_delivered_line_is_checked_against_the_orders_and_put_away_where_it_was_last(db_path):
    from inventory import receiving, watch

    db.save_purchase_order(db_path, {
        "list_date": date(2026, 11, 1), "site": "NORD", "work_item": "W1", "drafter": "PLUTUS",
        "lines": [{"article": "BOLT", "quantity": 10.0, "in_stock": False}]}, None, None, "PLUTUS")
    today = date.today().isoformat()
    first = db.save_note(db_path, make_note(supplier="ACME", site="NORD", delivery_date=today,
                                            lines=[Line(article="BOLT", quantity=8), Line(article="CHAIN", quantity=2)]),
                         "rv1", "", "T")
    assert receiving.sync(db_path) == 2 and receiving.sync(db_path) == 0             # safe to run again
    rows = {r["article"]: r for r in receiving.listing(db_path)}
    assert rows["BOLT"]["check_word"] == "as ordered" and rows["BOLT"]["suggested"] == "RECEIVING"
    assert rows["CHAIN"]["check_word"] == "not on any order"

    # 8 came, 2 more were expected: the next delivery of 5 is more than ordered.
    db.save_note(db_path, make_note(supplier="ACME", site="NORD", delivery_date=today, lines=[Line(article="BOLT", quantity=5)]),
                 "rv2", "", "T")
    receiving.sync(db_path)
    over = [r for r in receiving.listing(db_path) if r["article"] == "BOLT" and r["check_word"] == "more than ordered"]
    assert over and "5 came, 2 was still expected" in over[0]["check_detail"]
    watch.scan(db_path)
    assert any(n["kind"] == "receiving" and "delivery line(s) arrived not as ordered" in n["title"]
               and "CHAIN (not on any order)" in n["detail"] for n in watch.open_notices(db_path))

    with pytest.raises(ValueError):
        receiving.put_away(db_path, rows["BOLT"]["putaway_id"], "x" * 50)
    receiving.put_away(db_path, rows["BOLT"]["putaway_id"], "rack 3 / bay b", "marc")
    assert receiving.where(db_path, "BOLT") == [{"site": "NORD", "location": "RACK 3 / BAY B"}]
    with pytest.raises(ValueError):
        receiving.put_away(db_path, rows["BOLT"]["putaway_id"], "rack 4")             # already put away
    db.save_note(db_path, make_note(supplier="ACME", site="NORD", delivery_date=today, lines=[Line(article="BOLT", quantity=1)]),
                 "rv3", "", "T")
    receiving.sync(db_path)
    learned = [r for r in receiving.listing(db_path) if r["suggested"] == "RACK 3 / BAY B"]
    assert [r["quantity"] for r in learned] == [1.0]                  # the next delivery is suggested the same place
    assert receiving.confirm_suggested(db_path, "marc") == 1
    assert receiving.listing(db_path, "put away")[0]["location"] == "RACK 3 / BAY B"
    assert receiving.overview(db_path)["summary"]["waiting"] == len(receiving.listing(db_path))


def test_a_count_over_the_tolerance_needs_a_second_one_and_the_cause_is_looked_for(db_path):
    from inventory import counting, stock, watch

    today = date.today()
    # 100 CABLE came twice from ACME (one note stored twice by mistake) and 40 left on an order.
    for number in range(2):
        db.save_note(db_path, make_note(supplier="ACME", site="NORD", delivery_date=today.isoformat(),
                                        lines=[Line(article="CABLE", quantity=50)]), f"ct{number}", "", "T")
    assert stock.on_hand_map(db_path)["CABLE"] == 100

    # Inside the tolerance (2%, at least one unit): posted at once, and the count list loses it.
    assert counting.submit(db_path, "cable", 99)["state"] == "posted"
    assert stock.on_hand_map(db_path)["CABLE"] == 99

    # 50 on the shelf: the books are not touched, a recount is asked, and the duplicate note is named.
    asked = counting.submit(db_path, "cable", 49)
    assert asked["state"] == "recount" and stock.on_hand_map(db_path)["CABLE"] == 99
    assert "a delivery noted twice" in asked["causes_said"] and "50 from ACME" in asked["causes_said"]
    assert [r["article"] for r in counting.recounts(db_path)] == ["CABLE"]

    done = counting.submit(db_path, "cable", 50)                 # the second count agrees within a unit
    assert done["state"] == "confirmed" and done["variance"] == -49 and stock.on_hand_map(db_path)["CABLE"] == 50
    assert counting.recounts(db_path) == []

    # Two counts that disagree post nothing and wait for a person, who is told.
    counting.submit(db_path, "cable", 10)
    assert counting.submit(db_path, "cable", 30)["state"] == "disputed"
    assert stock.on_hand_map(db_path)["CABLE"] == 50
    watch.scan(db_path)
    assert any("Two counts of CABLE disagree" in n["title"] for n in watch.open_notices(db_path))
    check = counting.disputed(db_path)[0]
    counting.settle(db_path, check["check_id"], 12)
    assert stock.on_hand_map(db_path)["CABLE"] == 12 and counting.disputed(db_path) == []
    with pytest.raises(ValueError):
        counting.settle(db_path, check["check_id"], 12)
    with pytest.raises(ValueError):
        counting.submit(db_path, "cable", -1)

    # The daily list is made once a day and never says what the books expect.
    db.save_note(db_path, make_note(supplier="ACME", site="NORD", delivery_date=today.isoformat(),
                                    lines=[Line(article="HINGE", quantity=30)]), "ct9", "", "T")   # never counted: due
    added = counting.assign(db_path, today)
    assert added == ["HINGE"] and counting.assign(db_path, today) == []
    assert all(set(t) == {"article", "due_on", "where"} for t in counting.tasks(db_path))
    counting.submit(db_path, added[0], stock.on_hand_map(db_path)[added[0]])
    assert added[0] not in [t["article"] for t in counting.tasks(db_path)]
