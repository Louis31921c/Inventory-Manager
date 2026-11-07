"""Tests: route."""

from datetime import date

import pytest

from inventory import db
from helpers import a_basket


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


def test_the_step_route_answers_for_each_wedge_and_refuses_one_that_is_not_there(tmp_path,
                                                                                monkeypatch):
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

        for number in range(1, 8):
            answer = client.get(f"/api/chain/step/{number}").json()
            assert answer["step"] == number and answer["name"] and "rows" in answer
        approved = client.get("/api/chain/step/5").json()
        assert [row["title"] for row in approved["rows"]] == [f"#{request_id}: approved"]
        assert client.get("/api/chain/step/8").status_code == 404
        assert client.get("/api/chain/step/0").status_code == 404


def test_a_dispatch_runs_from_an_order_to_a_signature_and_prints_its_paper(db_path):
    from inventory import documents, outbound

    list_id = db.save_purchase_order(db_path, {
        "list_date": date(2026, 11, 2), "site": "NORD", "work_item": "W1", "drafter": "X",
        "lines": [{"article": "BOLT", "quantity": 10.0, "designation": "Bolt M6"},
                  {"article": "NUT", "quantity": 4.0}]}, None, None, "T")
    made = outbound.create_from_order(db_path, list_id, planned_on="2026-11-04", user="T")
    assert made["state"] == "planned" and [l["quantity"] for l in made["lines"]] == [10.0, 4.0]
    with pytest.raises(ValueError):
        outbound.create_from_order(db_path, 999)
    assert outbound.load(db_path, made["dispatch_id"])["state"] == "loaded"
    with pytest.raises(ValueError):
        outbound.load(db_path, made["dispatch_id"])
    with pytest.raises(ValueError):
        outbound.deliver(db_path, made["dispatch_id"], " ")                 # nobody signed

    part = outbound.deliver(db_path, made["dispatch_id"], "M. Durand", {2: 3})
    assert part["state"] == "part delivered" and part["received_by"] == "M. Durand"
    assert [l["delivered"] for l in part["lines"]] == [10.0, 3.0]
    assert outbound.listing(db_path)[0]["dispatch_id"] == made["dispatch_id"]

    pytest.importorskip("reportlab")
    for pdf in (documents.delivery_note_pdf(db_path, made["dispatch_id"]),
                documents.labels_pdf(db_path, made["dispatch_id"]),
                documents.order_pdf(db_path, list_id, "ACME", [("BOLT", "Bolt M6", "10")])):
        assert pdf.startswith(b"%PDF")
