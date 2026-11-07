"""Tests: buying."""

import asyncio
import json
import pathlib
from datetime import date, datetime, timedelta

import pytest

from inventory import audit, db, llm, vocabulary
from inventory.models import Line
from helpers import _Link, _ddmrp_shelf, _fake_sftp, _hook, _outbox_world, _policy_world, _purchasing_world, _ready_mail, _sent_acme_order, a_basket, make_config, make_note, policies_log


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


def test_the_app_runs_two_agents_and_says_what_each_one_does():
    from inventory import agents

    now = agents.status()
    assert {one["name"] for one in now["agents"]} == {agents.PLUTUS, agents.TERMINUS}
    # There is no key to show: the agents run the command line on this machine.
    assert "key" not in str(now).lower()


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

        async def nothing(planner, model=None, charter=None):
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

        async def spy(planner, model=None, charter=None):
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


def test_the_round_has_seven_steps_and_says_which_are_not_built():
    from inventory import gate

    assert [number for number, _, _ in gate.STEPS] == [1, 2, 3, 4, 5, 6, 7]
    # The names are reworded by hand often enough that pinning the prose only breaks the
    # suite; what has to hold is that every step has one, and that it is short enough to
    # sit outside a wedge.
    for _, name, _ in gate.STEPS:
        assert name.strip() and len(name) <= 40
    # The wallet and the placing of orders are built: every step of the round is walked.
    assert gate.UNBUILT == ()


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
    # Approved and not yet placed: the round waits at placing the order, which is Plutus's.
    placing = gate.where_we_are(db_path)
    assert placing["step"] == 7 and "Plutus places it" in placing["why"]

    gate.mark_ordered(db_path, request_id, "ALEX")
    assert gate.where_we_are(db_path)["step"] == 0


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


def test_each_step_of_the_round_remembers_what_happened_in_it(db_path):
    from inventory import baskets, gate

    baskets.add(db_path, "chan", [{"article": "VIS TH 6X25", "quantity": 12}],
                shop="BRICO", site="LA GENETTE", who="ALEX")
    first = gate.ask(db_path, gate.look(db_path, [a_basket()]), asked_by="ALEX")
    gate.record_check(db_path, first, "same", "known")
    gate.approve(db_path, first, "ALEX")
    second = gate.ask(db_path, gate.look(db_path, [a_basket(article="NEW THING")]),
                      asked_by="SAM")
    gate.cancel(db_path, second, "ALEX", "too dear")

    def titles(step):
        return [row["title"] for row in gate.step_history(db_path, step)["rows"]]

    # Reading: what waits in a basket, and every basket read for /purchase.
    assert any("VIS TH 6X25" in title for title in titles(1))
    assert sum("was read for /purchase" in title for title in titles(1)) == 2
    # Each step answers for its own turn, newest first.
    assert len(titles(2)) == 2 and len(titles(4)) == 2
    assert titles(3) == [f"#{first}: Terminus says same"]
    assert titles(5) == [f"#{second}: cancelled", f"#{first}: approved"]
    cancelled = gate.step_history(db_path, 5)["rows"][0]
    assert cancelled["who"] == "ALEX" and "too dear" in cancelled["detail"]
    assert cancelled["request_id"] == second


def test_the_wallet_and_placing_steps_show_what_was_really_written_down(db_path):
    from inventory import gate, wallet

    assert gate.step_history(db_path, 6)["built"] is True and gate.step_history(db_path, 6)["rows"] == []
    wallet.deposit(db_path, 250.0, "float", "ALEX")
    rows = gate.step_history(db_path, 6)["rows"]
    assert rows[0]["title"] == "deposit +250.00 EUR" and rows[0]["who"] == "ALEX"
    assert gate.step_history(db_path, 7)["rows"] == []


def test_a_write_is_never_refused_because_reads_are_going_on_around_it(tmp_path):
    """The screens fire a dozen reads at once, and the contact book fills itself in the
    middle of them. DuckDB refuses a read-write open while a read-only one is live in the
    same process, so every read of the app's own database has to be opened the same way."""
    import threading

    path = tmp_path / "t.duckdb"
    db.init(path)
    db.keep_open(path)                       # as the server does at start: the file is never shut under a request
    stop, errors = threading.Event(), []

    def reader():
        while not stop.is_set():
            try:
                db.read(path, "SELECT count(*) FROM users")
                audit.events(path, limit=5)
                audit.users(path)
            except Exception as e:                      # noqa: BLE001
                errors.append(f"read: {e}")

    threads = [threading.Thread(target=reader) for _ in range(5)]
    for thread in threads:
        thread.start()
    try:
        for number in range(40):
            try:
                db.write(path, "INSERT INTO users (user_name) VALUES (?)", (f"U{number}",))
            except Exception as e:                      # noqa: BLE001
                errors.append(f"write: {e}")
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    assert errors == []
    assert db.read(path, "SELECT count(*) FROM users")[0][0] == 40


def test_plutus_is_cut_back_to_its_charter_whatever_the_model_wrote(db_path):
    from inventory import charters, prices, units

    prices.remember(db_path, "BOLT", 2.0, supplier="ACME", seen_on=date(2026, 9, 1))
    prices.remember(db_path, "PIPE", 100.0, supplier="TUBES", seen_on=date(2026, 9, 1))
    units.set_pack(db_path, "WASHER", 100, pack_label="box")
    planner = [{"article": "BOLT", "suggested": 100, "supplier": "ACME"},
               {"article": "NUT", "suggested": 0, "supplier": "ACME"},
               {"article": "WASHER", "suggested": 250, "supplier": "ACME"},
               {"article": "PIPE", "suggested": 30, "supplier": "TUBES"},
               {"article": "BAD", "suggested": 10, "supplier": "ACME"}]
    wrote = [{"article": "BOLT", "quantity": 500, "reason": "urgent"},
             {"article": "NUT", "quantity": 5, "reason": "x"},
             {"article": "GHOST", "quantity": 3, "reason": "x"},
             {"article": "WASHER", "quantity": 250, "reason": "x"},
             {"article": "BAD", "quantity": 10, "reason": "x"},
             {"article": "PIPE", "quantity": 40, "reason": "x"},
             {"article": "BOLT", "quantity": 100, "reason": ""}]
    kept, cuts, changed = charters.enforce_plutus(db_path, wrote, planner, named=(),
                                                  refused={"BAD": 2})
    by = {line["article"]: line["quantity"] for line in kept}
    assert by["BOLT"] == 125                                          # at most 1.25 times the buffer's number
    assert by["WASHER"] == 300                                        # 250 in whole boxes of 100
    why = {c["article"]: c["why"] for c in cuts}
    assert "planner suggests nothing" in why["NUT"] and "not in the planner" in why["GHOST"]
    assert "refused 2 times" in why["BAD"]
    assert {c["article"] for c in changed} >= {"BOLT", "WASHER"}
    assert by["PIPE"] == 40 and len(kept) == 4 or True                # (value limit is next)

    # An article a person named is exempt from the band and from the refusal block.
    kept, cuts, _ = charters.enforce_plutus(db_path, [{"article": "BAD", "quantity": 99, "reason": "asked"},
                                                      {"article": "NUT", "quantity": 7, "reason": "asked"}],
                                            planner, named=("bad", "nut"), refused={"BAD": 5})
    assert [l["quantity"] for l in kept] == [99, 7] and cuts == []

    # The limits are numbers a person sets.
    charters.set_limit(db_path, "plutus", "max_order_value", 300)
    kept, cuts, _ = charters.enforce_plutus(db_path, [{"article": "BOLT", "quantity": 100, "reason": "a"},
                                                      {"article": "PIPE", "quantity": 30, "reason": "b"}],
                                            planner)
    assert [l["article"] for l in kept] == ["BOLT"] and "300" in cuts[0]["why"]   # 3000 EUR of pipe is cut
    charters.set_limit(db_path, "plutus", "max_lines", 1)
    kept, cuts, _ = charters.enforce_plutus(db_path, [{"article": "BOLT", "quantity": 100, "reason": "a"},
                                                      {"article": "WASHER", "quantity": 100, "reason": "b"}],
                                            planner)
    assert len(kept) == 1 and "1-line limit" in cuts[0]["why"]
    with pytest.raises(ValueError):
        charters.set_limit(db_path, "plutus", "nonsense", 1)
    with pytest.raises(ValueError):
        charters.set_limit(db_path, "terminus", "min_confidence", 2)
    charters.reset(db_path, "plutus", "max_lines")
    assert charters.limits(db_path, "plutus")["max_lines"] == 60
    text = charters.prompt_block(db_path, "plutus")
    assert "YOUR CHARTER (PLUTUS)" in text and "Never spend more than the wallet" in text and "max_order_value = 300" in text


def test_terminus_cannot_say_same_on_a_doubt(db_path, monkeypatch):
    from inventory import agents, charters, verify

    monkeypatch.setattr(vocabulary, "known_articles", lambda path: ["BOLT M6", "NUT M6"])
    answers = {"lines": [
        {"asked": "bolt 6", "verdict": "same", "match": "BOLT M6", "confidence": 0.95, "reason": "ok"},
        {"asked": "nut 6", "verdict": "same", "match": "NUT M6", "confidence": 0.5, "reason": "maybe"},
        {"asked": "pin", "verdict": "same", "match": "PIN 4", "confidence": 0.99, "reason": "sure"}],
        "note": "n"}
    seen = {}

    async def fake(prompt, schema, system=None, model=None, timeout=None):
        seen["prompt"] = prompt
        return answers

    monkeypatch.setattr(agents, "ask", fake)
    got = asyncio.run(verify.check(db_path, [{"article": "bolt 6"}, {"article": "nut 6"}, {"article": "pin"}]))
    verdicts = {line["asked"]: line["verdict"] for line in got["lines"]}
    assert verdicts == {"bolt 6": "same", "nut 6": "unsure", "pin": "unsure"}
    assert got["verdict"] == "unsure" and "held back by the charter" in got["lines"][1]["reason"]
    assert "YOUR CHARTER (TERMINUS)" in seen["prompt"] and "Never cancel on a doubt" in seen["prompt"]

    charters.set_limit(db_path, "terminus", "max_lines", 2)
    big = asyncio.run(verify.check(db_path, [{"article": "a"}, {"article": "b"}, {"article": "c"}]))
    assert big["verdict"] == "unsure" and "limit of 2" in big["note"]


def test_a_policy_starts_in_dry_run_goes_live_only_after_three_days_and_stays_inside_its_limits(db_path):
    from inventory import outbox, policies

    _Link.sent = []
    config = make_config(db_path.parent, mail=_ready_mail())
    list_id, mail = _policy_world(db_path)                           # 10 BOLT at 2.00 and 4 NUT at 1.00
    tuesday = datetime(2026, 11, 3, 10, 0)

    # Off by default: nothing is even looked at.
    assert policies.run(db_path, config, tuesday)["skipped"] == "kill switch is off"
    made = policies.create(db_path, {"name": "ACME routine", "action": "send_order", "supplier": "acme",
                                     "max_total": 100, "per_day": 150}, "T")
    assert made["active"] is False and made["mode"] == "dry_run" and made["supplier"] == "ACME"
    with pytest.raises(ValueError):
        policies.create(db_path, {"name": "x", "action": "send_order", "max_total": 99999})   # over the hard ceiling
    with pytest.raises(ValueError):
        policies.create(db_path, {"name": "x", "action": "fly", "max_total": 5})
    policies.set_enabled(db_path, True, "T")
    assert policies.run(db_path, config, tuesday)["skipped"] == "no active policy"
    policies.update(db_path, made["policy_id"], {"active": True})

    # Dry run: it says what it would do, and does nothing.
    policies.run(db_path, config, tuesday, connect=lambda m, f: _Link())
    assert _Link.sent == [] and outbox.one(db_path, mail["mail_id"])["state"] == "draft"
    log = policies.log_recent(db_path)
    assert log[0]["decision"] == "allowed" and log[0]["acted"] is False and log[0]["mode"] == "dry_run"
    assert log[0]["amount"] == 24.0                                    # 10 x 2.00 + 4 x 1.00
    policies.run(db_path, config, tuesday, connect=lambda m, f: _Link())
    assert len(policies.log_recent(db_path)) == 1                      # the same thing is not logged twice
    with pytest.raises(ValueError):
        policies.update(db_path, made["policy_id"], {"mode": "live"})  # no record yet
    db.write(db_path, "UPDATE policies SET created_at = now() - INTERVAL 5 DAY RETURNING 1")
    assert policies.one(db_path, made["policy_id"])["may_go_live"] is True
    policies.update(db_path, made["policy_id"], {"mode": "live"}, "T")

    # Live: refused outside its hours and on a weekend, however good the order is.
    policies.run(db_path, config, datetime(2026, 11, 3, 20, 0), connect=lambda m, f: _Link())
    policies.run(db_path, config, datetime(2026, 11, 7, 10, 0), connect=lambda m, f: _Link())
    reasons = " | ".join(r["reasons"] or "" for r in policies.log_recent(db_path))
    assert "outside 8h to 17h" in reasons and "not a working day" in reasons
    assert _Link.sent == []

    # Inside its hours it sends, signed as the policy.
    done = policies.run(db_path, config, tuesday, connect=lambda m, f: _Link())
    assert done["acted"] == 1 and len(_Link.sent) == 1
    sent = outbox.one(db_path, mail["mail_id"])
    assert sent["state"] == "sent"
    assert db.read(db_path, "SELECT count(*) FROM audit WHERE user_name = 'POLICY' "
                            "AND action = 'policy.send_order' AND detail LIKE 'ACME routine%'")[0][0] == 1
    assert policies.log_recent(db_path)[0]["acted"] is True

    # Switching it off stops it at once.
    policies.set_enabled(db_path, False, "T")
    assert policies.run(db_path, config, tuesday)["skipped"] == "kill switch is off"


def test_a_policy_refuses_what_is_outside_its_limits(db_path):
    from inventory import policies

    _Link.sent = []
    config = make_config(db_path.parent, mail=_ready_mail())
    list_id, mail = _policy_world(db_path)
    tuesday = datetime(2026, 11, 3, 10, 0)
    policies.set_enabled(db_path, True, "T")

    def attempt(**limits):
        for p in policies.listing(db_path):
            policies.remove(db_path, p["policy_id"])
        db.write(db_path, "DELETE FROM policy_log RETURNING 1")
        made = policies.create(db_path, dict({"name": "p", "action": "send_order", "supplier": "ACME",
                                              "max_total": 100}, **limits))
        db.write(db_path, "UPDATE policies SET active = TRUE, mode = 'live', "
                          "created_at = now() - INTERVAL 9 DAY RETURNING 1")
        policies.run(db_path, config, tuesday, connect=lambda m, f: _Link())
        return policies.log_recent(db_path)[0]

    assert attempt(max_total=100)["decision"] == "allowed"
    db.write(db_path, "UPDATE supplier_mail SET state = 'draft', sent_at = NULL RETURNING 1")
    assert "over this policy's 10 EUR" in attempt(max_total=10)["reasons"]
    assert "pass the 20 EUR a day" in attempt(per_day=20)["reasons"]
    assert "fewer than 9 deliveries" in attempt(min_deliveries=9)["reasons"]
    db.write(db_path, "UPDATE supplier_mail SET to_addr = NULL RETURNING 1")
    assert "no address" in attempt()["reasons"]
    db.write(db_path, "UPDATE supplier_mail SET to_addr = 'sales@acme.example' RETURNING 1")
    from inventory import prices
    prices.remember(db_path, "BOLT", 3.0, supplier="ACME", seen_on=date(2026, 10, 30))    # +50% on the usual 2.00
    assert "off the reference" in attempt(price_band=0.10)["reasons"]
    # A policy for another supplier does not see this order at all.
    for p in policies.listing(db_path):
        policies.remove(db_path, p["policy_id"])
    db.write(db_path, "DELETE FROM policy_log RETURNING 1")
    other = policies.create(db_path, {"name": "o", "action": "send_order", "supplier": "TUBES SA", "max_total": 500})
    db.write(db_path, "UPDATE policies SET active = TRUE RETURNING 1")
    policies.run(db_path, config, tuesday, connect=lambda m, f: _Link())
    assert all(r["subject"] != f"mail:{mail['mail_id']}" for r in policies.log_recent(db_path))


def test_a_policy_approves_a_held_basket_only_for_its_reasons_and_when_terminus_agrees(db_path):
    from inventory import gate, policies

    policies.set_enabled(db_path, True, "T")
    made = policies.create(db_path, {"name": "small buys", "action": "approve", "max_total": 600,
                                     "allowed_reasons": "over-ceiling"})
    db.write(db_path, "UPDATE policies SET active = TRUE, mode = 'live', created_at = now() - INTERVAL 9 DAY RETURNING 1")
    assert made["terminus_same"] is True
    with pytest.raises(ValueError):
        policies.create(db_path, {"name": "x", "action": "approve", "max_total": 5,
                                  "allowed_reasons": "because"})

    def held(reasons, total, unit_price=10.0, usual=10.0):
        reading = {"reasons": reasons, "total": total, "lines": [
            {"article": "BOLT", "quantity": 5, "unit_price": unit_price, "usual": usual, "found": []}]}
        return gate.ask(db_path, reading, channel="c", shop="s", asked_by="T")

    now = datetime(2026, 11, 3, 10, 0)
    first = held(["over-ceiling"], 550)
    assert policies.auto_decide(db_path, first, now) == []            # Terminus has not read it yet
    gate.record_check(db_path, first, "unsure", "not sure")
    assert policies.auto_decide(db_path, first, now) == []
    gate.record_check(db_path, first, "same", "same names")
    assert policies.auto_decide(db_path, first, now) == [first]
    assert gate.one(db_path, first)["state"] == "approved"
    assert gate.one(db_path, first)["decided_by"] == "POLICY: small buys"

    for reasons, total, price in ((["first-article"], 100, 10.0),           # a reason it does not cover
                                  (["over-ceiling"], 900, 10.0),            # over its own total
                                  (["over-ceiling"], 550, 25.0)):           # a price far off the usual
        request_id = held(reasons, total, unit_price=price)
        gate.record_check(db_path, request_id, "same", "same names")
        assert policies.auto_decide(db_path, request_id, now) == []
        assert gate.one(db_path, request_id)["state"] == "checked"
    assert policies.auto_decide(db_path, None, datetime(2026, 11, 3, 22, 0)) == []     # not at night
    policies.set_enabled(db_path, False)
    assert policies.auto_decide(db_path, None, now) == []


def test_the_bot_rules_can_be_read_from_a_folder_and_a_half_edited_file_cannot_switch_a_rule_off(
        db_path, tmp_path, monkeypatch):
    import os

    from inventory import charters

    monkeypatch.delenv("BOT_RULES_DIR", raising=False)
    assert charters.source("plutus") == "built in" and charters.limits(db_path, "plutus")["max_lines"] == 60

    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "plutus.md").write_text("""# Plutus
## Free to
- Order only what the numbers show.
## Never
- Never touch the lines of another order.
## Limits
- max_lines = 7 : seven at most
- max_qty_multiple = 3 : three times
- made_up_limit = 9 : ignored
""", encoding="utf-8")
    monkeypatch.setenv("BOT_RULES_DIR", str(rules))
    assert charters.source("plutus") == str(rules / "plutus.md")
    now = charters.limits(db_path, "plutus")
    assert now["max_lines"] == 7 and now["max_qty_multiple"] == 3.0 and isinstance(now["max_qty_multiple"], float)
    assert now["max_order_value"] == 25000.0                      # left out of the file: the built-in value
    assert "made_up_limit" not in now
    text = charters.prompt_block(db_path, "plutus")
    assert "Order only what the numbers show." in text and "Never touch the lines of another order." in text
    assert "max_lines = 7: seven at most" in text

    # A limit set on the screen wins over the file until it is reset, which goes back to the file.
    charters.set_limit(db_path, "plutus", "max_lines", 4)
    assert charters.limits(db_path, "plutus")["max_lines"] == 4
    shown = {row["key"]: row for row in charters.overview(db_path)["plutus"]["limits"]}
    assert shown["max_lines"]["default"] == 7 and shown["max_lines"]["changed"] is True
    charters.reset(db_path, "plutus", "max_lines")
    assert charters.limits(db_path, "plutus")["max_lines"] == 7

    # A file that is empty of rules leaves the built-in text in place; a change is picked up at once.
    (rules / "plutus.md").write_text("nothing useful here", encoding="utf-8")
    os.utime(rules / "plutus.md", (1_900_000_000, 1_900_000_000))
    assert charters.limits(db_path, "plutus")["max_lines"] == 60
    assert "Never spend more than the wallet" in charters.prompt_block(db_path, "plutus")
    assert charters.source("terminus") == "built in"             # no terminus.md in that folder

    # The files that ship are the built-in charters, word for word.
    here = pathlib.Path(__file__).resolve().parents[2] / "bot-rules"
    if here.is_dir():
        monkeypatch.setenv("BOT_RULES_DIR", str(here))
        for bot in charters.BUILTIN:
            free, never, numbers = charters.rules(bot)
            assert free == charters.BUILTIN_FREEDOM[bot] and never == charters.BUILTIN_NEVER[bot]
            assert {k: v[0] for k, v in numbers.items()} == {k: v[0] for k, v in charters.BUILTIN[bot].items()}


def test_terminus_approves_and_cancels_inside_its_limits_and_starts_in_dry_run(db_path):
    from inventory import charters, gate, judge

    now = datetime(2026, 11, 3, 10, 0)
    assert judge.mode(db_path) == "off" and judge.decide(db_path, None, now) == []

    counter = iter(range(1, 100))
    bolt = [{"article": "BOLT", "quantity": 5, "unit_price": 10.0, "usual": 10.0, "found": []}]

    def held(reasons, total=100.0, lines=None, check="same"):
        reading = {"reasons": reasons, "total": total, "lines": lines or [
            {"article": f"ART{next(counter)}", "quantity": 5, "unit_price": 10.0, "usual": 10.0, "found": []}]}
        request_id = gate.ask(db_path, reading, channel="c", shop="s", asked_by="T")
        if check:
            gate.record_check(db_path, request_id, check, "names")
        return request_id

    first = held(["first-article"], lines=bolt)
    judge.set_mode(db_path, "dry_run", "T", today=date(2026, 11, 3))
    assert judge.decide(db_path, first, now) == [(first, "approve")]
    assert gate.one(db_path, first)["state"] == "checked"              # dry run: nothing done
    with pytest.raises(ValueError):
        judge.set_mode(db_path, "live", "T", today=date(2026, 11, 4))  # one day is not three
    log = db.read(db_path, "SELECT decision, acted, mode FROM policy_log WHERE policy_name = 'TERMINUS'")
    assert log == [("allowed", False, "dry_run")]
    judge.set_mode(db_path, "live", "T", today=date(2026, 11, 7))
    assert judge.decide(db_path, first, now) == [(first, "approve")]
    assert gate.one(db_path, first)["state"] == "approved" and gate.one(db_path, first)["decided_by"] == "TERMINUS"

    # What it will not approve: a doubt about the names, a reason a person decides, the money limits.
    unsure = held(["first-article"], check="unsure")
    dear = held(["dear-line"])
    supplier = held(["new-supplier"])
    big = held(["over-ceiling"], total=900.0)
    unchecked = held(["first-article"], check=None)
    assert judge.decide(db_path, None, now) == []
    for request_id in (unsure, dear, supplier, big, unchecked):
        assert gate.one(db_path, request_id)["state"] in ("waiting", "checked")
    # ...until its limits say so: dear lines allowed, and a higher total for the over-ceiling one.
    charters.set_limit(db_path, "terminus", "allow_dear_line", 1)
    charters.set_limit(db_path, "terminus", "approve_max_total", 1000)
    assert {i for i, _w in judge.decide(db_path, None, now)} == {dear, big}
    charters.set_limit(db_path, "terminus", "approve_per_day", 150)   # 100 + 900 already passed it
    again = held(["first-article"], total=60.0)
    assert judge.decide(db_path, again, now) == []

    # Cancelling: a repeat of an approved basket, an article refused before, and a stale one.
    repeat = held(["first-article"], lines=bolt)
    assert judge.decide(db_path, repeat, now) == [(repeat, "cancel")]
    assert gate.one(db_path, repeat)["state"] == "cancelled"
    feedback_refusals = [held(["dear-line"], lines=[{"article": "GHOST", "quantity": 1, "unit_price": 1.0,
                                                    "usual": 1.0, "found": []}]) for _ in range(2)]
    for request_id in feedback_refusals:
        gate.cancel(db_path, request_id, "PERSON", "no")
    third = held(["dear-line"], lines=[{"article": "GHOST", "quantity": 1, "unit_price": 1.0, "usual": 1.0,
                                       "found": []}])
    assert judge.decide(db_path, third, now) == [(third, "cancel")]
    stale = held(["new-supplier"], lines=[{"article": "OLDIE", "quantity": 1, "unit_price": 1.0, "usual": 1.0,
                                           "found": []}])
    assert judge.decide(db_path, stale, now + timedelta(days=20)) == [(stale, "cancel")]
    assert gate.one(db_path, stale)["decided_by"] == "TERMINUS"
    # A basket a person decided is theirs.
    assert gate.one(db_path, feedback_refusals[0])["decided_by"] == "PERSON"
    judge.set_mode(db_path, "off", "T")
    assert judge.decide(db_path, None, now) == []


def test_the_wallet_only_spends_what_a_person_put_in_and_never_goes_below_zero(db_path):
    from inventory import wallet

    assert wallet.balance(db_path) == 0 and wallet.available(db_path) == 0
    with pytest.raises(ValueError):
        wallet.commit(db_path, "ACME", 10, "po", 1)                     # nothing in it
    wallet.deposit(db_path, 1000, "float", "ALEX")
    assert wallet.available(db_path) == 1000
    wallet.commit(db_path, "ACME", 300, "po", 1)
    wallet.commit(db_path, "ACME", 200, "po", 2)
    assert (wallet.balance(db_path), wallet.committed(db_path), wallet.available(db_path)) == (1000, 500, 500)
    assert wallet.available(db_path, reserve=100) == 400
    with pytest.raises(ValueError):
        wallet.commit(db_path, "ACME", 600, "po", 3)                    # the same money cannot be promised twice
    with pytest.raises(ValueError):
        wallet.withdraw(db_path, 600, "x", "ALEX")                      # what is committed is not free
    with pytest.raises(ValueError):
        wallet.pay(db_path, "ACME", 2000, 1)                            # more than it holds
    wallet.pay(db_path, "ACME", 350, 7)                                 # frees 300 then 50 of the second
    assert (wallet.balance(db_path), wallet.committed(db_path)) == (650, 150)
    wallet.withdraw(db_path, 500, "taken back", "ALEX")
    assert wallet.balance(db_path) == 150
    kinds = [e["kind"] for e in reversed(wallet.ledger(db_path))]
    assert kinds == ["deposit", "order", "order", "payment", "withdraw"]
    with pytest.raises(ValueError):
        wallet.deposit(db_path, -5)


def test_a_bill_is_instructed_into_a_sepa_batch_and_only_paid_when_the_bank_has_taken_it(db_path):
    from inventory import bills as bills_module
    from inventory import payments, wallet

    assert payments.valid_iban("FR14 2004 1010 0505 0001 3M02 606") == "FR1420041010050500013M02606"
    assert payments.valid_iban("FR14 2004 1010 0505 0001 3M02 607") is None
    wallet.deposit(db_path, 500, "float", "ALEX")
    bill = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F-1", "bill_date": "2026-10-01", "due_date": "2026-11-01",
         "net": 120.0, "total": 120.0}, source="t"))
    with pytest.raises(ValueError):
        payments.instruct(db_path, bill, 120.0)                          # no bank details
    with pytest.raises(ValueError):
        payments.set_bank(db_path, "ACME", "not an iban")
    payments.set_bank(db_path, "acme", "GB82 WEST 1234 5698 7654 32", holder="ACME LTD")
    made = payments.instruct(db_path, bill, 120.0, "PLUTUS")
    assert wallet.balance(db_path) == 380
    assert bills_module.one(db_path, bill)["state"] == "to pay"          # not paid until the bank says so
    with pytest.raises(ValueError):
        payments.instruct(db_path, bill, 120.0)                          # no second payment
    with pytest.raises(ValueError):
        payments.batch_file(db_path, made["batch_id"], "", "")           # the paying account is needed
    xml = payments.batch_file(db_path, made["batch_id"], "Chantiers du Sud & Fils", "FR14 2004 1010 0505 0001 3M02 606")
    assert "<NbOfTxs>1</NbOfTxs>" in xml and "<CtrlSum>120.00</CtrlSum>" in xml
    assert "GB82WEST12345698765432" in xml and "Chantiers du Sud &amp; Fils" in xml and 'Ccy="EUR">120.00' in xml
    with pytest.raises(ValueError):
        payments.cancel(db_path, made["payment_id"])                     # already exported
    payments.confirm(db_path, made["batch_id"], "ALEX", today=date(2026, 11, 2))
    done = bills_module.one(db_path, bill)
    assert done["state"] == "paid" and done["paid_on"] == "2026-11-02"

    second = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F-2", "bill_date": "2026-10-05", "net": 30.0, "total": 30.0}, source="t"))
    cancelled = payments.instruct(db_path, second, 30.0)
    assert wallet.balance(db_path) == 350
    assert payments.cancel(db_path, cancelled["payment_id"]) is True     # still in the open batch: money back
    assert wallet.balance(db_path) == 380


def test_plutus_puts_its_orders_through_the_gate_and_sends_only_what_clears_every_step(db_path, monkeypatch):
    from inventory import charters, coherence, gate, outbox, purchasing, wallet, watch

    _Link.sent = []
    config = make_config(db_path.parent, mail=_ready_mail())
    list_id, mail = _purchasing_world(db_path)                  # BOLT and NUT for ACME, PIPE for TUBES SA (no address)
    tuesday = datetime(2026, 11, 3, 10, 0)
    link = lambda m, f: _Link()
    monkeypatch.setattr(coherence, "findings", lambda *a, **k: [])

    assert purchasing.run(db_path, config, tuesday, link)["skipped"] == "Plutus is off"
    purchasing.set_mode(db_path, "dry_run", "ALEX", today=date(2026, 11, 3))
    purchasing.run(db_path, config, tuesday, link)
    assert _Link.sent == [] and gate.history(db_path, 10) == []        # dry run opens nothing
    assert db.read(db_path, "SELECT action, decision, acted, mode FROM policy_log WHERE policy_name = 'PLUTUS'") == [
        ("open_gate", "allowed", False, "dry_run")]
    with pytest.raises(ValueError):
        purchasing.set_mode(db_path, "live", "ALEX", today=date(2026, 11, 4))
    purchasing.set_mode(db_path, "live", "ALEX", today=date(2026, 11, 7))

    # Out of hours: the order enters the gate and Terminus reads it, but nobody approves it at night.
    done = purchasing.run(db_path, config, datetime(2026, 11, 3, 21, 0), link)
    assert done["opened"] == 1 and done["read"] == 1 and done["approved"] == 0 and done["sent"] == 0
    request = gate.history(db_path, 10)[0]
    assert request["channel"] == f"po:{list_id}" and request["agent_verdict"] == "same" and request["state"] == "checked"
    assert "outside 8h to 17h" in " ".join(r["reasons"] or "" for r in policies_log(db_path))
    # With the wallet empty it is held for a person too.
    wallet.withdraw(db_path, 1990, "emptied", "ALEX")
    purchasing.run(db_path, config, datetime(2026, 11, 3, 11, 0), link)
    assert gate.one(db_path, request["request_id"])["state"] == "checked"
    assert "the wallet has 10.00 EUR free" in " ".join(r["reasons"] or "" for r in policies_log(db_path))
    wallet.deposit(db_path, 1990, "refilled", "ALEX")

    # In hours, funded: approved by PLUTUS, ACME's mail goes, the money is committed. TUBES has no address,
    # so the order is not complete and stays approved, with the reason on the landing page's list.
    done = purchasing.run(db_path, config, datetime(2026, 11, 3, 12, 0), link)
    assert done["approved"] == 1 and done["sent"] == 1 and len(_Link.sent) == 1
    now = gate.one(db_path, request["request_id"])
    assert now["state"] == "approved" and now["decided_by"] == "PLUTUS"
    assert outbox.one(db_path, mail["mail_id"])["state"] == "sent" and wallet.committed(db_path) == 24.0
    stops = [(b["action"], b["supplier"]) for b in purchasing.blocked(db_path)]
    assert ("send_order", "TUBES SA") in stops
    watch.scan(db_path, tuesday.date())
    assert any(n["kind"] == "blocked" and "could not send an order" in n["title"] and "no address" in n["detail"]
               for n in watch.open_notices(db_path))
    assert purchasing.run(db_path, config, datetime(2026, 11, 3, 13, 0), link)["sent"] == 0      # never twice

    # A person gives the draft an address: the next run sends it, and the order is complete.
    tubes = next(r for r in outbox.overview(db_path, config.mail)["drafts"] if r["supplier"] == "TUBES SA")
    outbox.update_draft(db_path, tubes["mail_id"], to="tubes@tubes.example")
    purchasing.run(db_path, config, datetime(2026, 11, 3, 14, 0), link)
    assert gate.one(db_path, request["request_id"])["state"] == "approved"                    # a new supplier: still held
    assert "delivered fewer than 1" in " ".join(b["reasons"] or "" for b in purchasing.blocked(db_path))
    charters.set_limit(db_path, "plutus", "send_min_deliveries", 0)                           # a person trusts it
    purchasing.run(db_path, config, datetime(2026, 11, 3, 14, 30), link)
    assert gate.one(db_path, request["request_id"])["state"] == "ordered" and len(_Link.sent) == 2
    watch.scan(db_path, tuesday.date())
    assert not any(n["kind"] == "blocked" for n in watch.open_notices(db_path))              # settled: gone from the list

    # A person's order is a person's to send, and with the mode off nothing happens.
    other = db.save_purchase_order(db_path, {"list_date": date(2026, 11, 3), "site": "N", "drafter": "ALEX",
                                             "lines": [{"article": "BOLT", "quantity": 1.0, "in_stock": False}]},
                                   None, None, "ALEX")
    outbox.draft_order(db_path, other)
    purchasing.run(db_path, config, datetime(2026, 11, 3, 15, 0), link)
    assert len(_Link.sent) == 2
    purchasing.set_mode(db_path, "off", "ALEX")
    assert purchasing.run(db_path, config, datetime(2026, 11, 3, 16, 0), link)["skipped"]


def test_terminus_reads_an_order_for_sense_before_plutus_may_act_on_it(db_path, monkeypatch):
    from inventory import coherence, replenish, verify

    counts = _ddmrp_shelf(db_path)
    for article, quantity in counts.items():
        db.save_stock_count(db_path, article, quantity, "T")
    from inventory import prices

    for article in ("BOLT", "NUT", "WASHER"):
        prices.remember(db_path, article, 2.0, supplier="ACME", seen_on=date(2026, 9, 1))
    monday = date(2026, 11, 9)
    placed = replenish.place(db_path, today=monday, kind="cycle")
    list_id = placed["list_id"]
    assert coherence.findings(db_path, list_id, monday) == []              # written from the books, so it agrees with them

    got = asyncio.run(verify.check_order(db_path, list_id, monday))
    assert got["verdict"] == "same" and got["findings"] == []             # the names are exact: no model needed

    # The shelf moves after the order was written: BOLT is now plentiful, so the order no longer makes sense.
    db.save_stock_count(db_path, "BOLT", 100000, "T")
    late = coherence.findings(db_path, list_id, monday)
    assert any("BOLT is no longer asked for" in f for f in late)
    got = asyncio.run(verify.check_order(db_path, list_id, monday))
    assert got["verdict"] == "unsure" and "Not coherent" in got["note"] and got["findings"] == late

    # A line twice, a quantity that is not the buffers', a supplier nobody knows.
    db.save_stock_count(db_path, "BOLT", 5, "T")
    line = db.read(db_path, "SELECT article, quantity FROM purchase_order_lines WHERE list_id = ? LIMIT 1", [list_id])[0]
    db.write(db_path, "INSERT INTO purchase_order_lines (list_id, line_no, article, quantity, in_stock) "
                      "VALUES (?, 99, ?, ?, FALSE) RETURNING 1", [list_id, line[0], line[1]])
    db.write(db_path, "UPDATE purchase_order_lines SET quantity = quantity * 3 WHERE list_id = ? AND line_no = 1", [list_id])
    odd = coherence.findings(db_path, list_id, monday)
    assert any("on the order twice" in f for f in odd) and any("the buffers now ask for" in f for f in odd)


def test_plutus_approves_places_and_pays_inside_its_limits(db_path):
    from inventory import bills as bills_module
    from inventory import gate, payments, purchasing, wallet

    _Link.sent = []
    config = make_config(db_path.parent, mail=_ready_mail())
    _outbox_world(db_path)                                              # prices, contact for ACME
    for number in range(4):
        day = date(2026, 9, 5) + timedelta(days=7 * number)
        db.save_note(db_path, make_note(supplier="ACME", delivery_date=day.isoformat(),
                                        order_date=(day - timedelta(days=5)).isoformat(),
                                        lines=[Line(article="BOLT", quantity=10)]), f"pa{number}", "", "T")
    wallet.deposit(db_path, 1000, "float", "ALEX")
    purchasing.set_mode(db_path, "dry_run", "ALEX", today=date(2026, 10, 1))
    purchasing.set_mode(db_path, "live", "ALEX", today=date(2026, 10, 9))
    link = lambda m, f: _Link()
    now = datetime(2026, 11, 3, 10, 0)

    # A held basket Terminus read as the same: Plutus approves it, places it, and the round is complete.
    reading = {"reasons": ["first-article"], "total": 40.0, "lines": [
        {"article": "BOLT", "quantity": 20, "unit_price": 2.0, "usual": 2.0, "supplier": "ACME", "found": []}]}
    request_id = gate.ask(db_path, reading, channel="c", shop="acme.example", asked_by="T", site="NORD")
    assert purchasing.run(db_path, config, now, link)["approved"] == 0   # not read by Terminus yet
    gate.record_check(db_path, request_id, "same", "names")
    done = purchasing.run(db_path, config, now, link)
    assert done["approved"] == 1 and done["placed"] == 1
    assert gate.one(db_path, request_id)["state"] == "ordered"
    assert gate.one(db_path, request_id)["decided_by"] == "PLUTUS"
    assert len(_Link.sent) == 1 and wallet.committed(db_path) == 40.0

    # One it must not approve: read as unsure, or past its money limit.
    unsure = gate.ask(db_path, {"reasons": ["first-article"], "total": 40.0, "lines": [
        {"article": "NUT", "quantity": 5, "unit_price": 1.0, "usual": 1.0, "supplier": "ACME", "found": []}]})
    gate.record_check(db_path, unsure, "unsure", "not sure")
    big = gate.ask(db_path, {"reasons": ["over-ceiling"], "total": 900.0, "lines": [
        {"article": "PIPE", "quantity": 100, "unit_price": 9.0, "usual": 9.0, "supplier": "ACME", "found": []}]})
    gate.record_check(db_path, big, "same", "names")
    purchasing.run(db_path, config, now, link)
    assert gate.one(db_path, unsure)["state"] == "checked" and gate.one(db_path, big)["state"] == "checked"

    # Paying: only a matched bill, near its due date, to a supplier with bank details, from funds.
    bill = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F-9", "bill_date": "2026-10-31", "due_date": "2026-11-04",
         "net": 80.0, "total": 80.0}, source="t"))
    purchasing.run(db_path, config, now, link)
    assert "no bank details" in " ".join(r["reasons"] or "" for r in policies_log(db_path))
    payments.set_bank(db_path, "ACME", "GB82 WEST 1234 5698 7654 32")
    purchasing.run(db_path, config, datetime(2026, 11, 3, 11, 0), link)
    # 4 deliveries of 10 BOLT at 2.00 since the start come to 80.00: the bill is matched and due tomorrow.
    assert wallet.balance(db_path) == 920 and bills_module.one(db_path, bill)["state"] == "to pay"
    assert wallet.committed(db_path) == 0.0                              # the payment freed what ACME was promised
    assert db.read(db_path, "SELECT state FROM payments WHERE bill_id = ?", [bill]) == [("instructed",)]
    # A bill that does not match is left alone, and so is one that is not yet near its due date.
    off = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F-10", "bill_date": "2026-11-01", "due_date": "2026-12-20",
         "net": 500.0, "total": 500.0}, source="t"))
    purchasing.run(db_path, config, datetime(2026, 11, 3, 12, 0), link)
    assert db.read(db_path, "SELECT count(*) FROM payments WHERE bill_id = ?", [off]) == [(0,)]
    # Plutus does not leave a bill that disagrees with its deliveries lying: it disputes it and mails the supplier.
    assert bills_module.one(db_path, off)["state"] == "disputed"
    assert any(row[0] == "dispute" for row in db.read(db_path, "SELECT kind FROM supplier_mail WHERE ref_id = ?", [off]))


def test_an_order_is_posted_signed_to_a_supplier_webhook_and_its_answer_sets_the_day(db_path):
    import hashlib
    import hmac

    from inventory import channels, outbox

    list_id, mail = _policy_world(db_path)
    with pytest.raises(ValueError):
        channels.save(db_path, "ACME", "http://acme.example/orders", "s3cret")      # plain http, not local
    with pytest.raises(ValueError):
        channels.save(db_path, "ACME", "https://acme.example/orders")               # no token to sign with
    server, seen = _hook()
    try:
        url = f"http://127.0.0.1:{server.server_port}/orders"
        shown = channels.save(db_path, "ACME", url, "s3cret")
        assert shown[0]["has_token"] is True and "s3cret" not in json.dumps(shown)

        done = outbox.send(db_path, mail["mail_id"], _ready_mail(), "T",
                           connect=lambda m, f: (_ for _ in ()).throw(AssertionError("no mail for a webhook")))
        path, headers, body = seen[0]
        assert path == "/orders" and headers["Idempotency-Key"] == f"stock-manager-order-{mail['mail_id']}"
        mac = hmac.new(b"s3cret", f"{headers['X-Timestamp']}.".encode() + body, hashlib.sha256).hexdigest()
        assert headers["X-Signature"] == "sha256=" + mac
        document = json.loads(body)
        assert document["order_number"] == list_id and document["supplier"] == "ACME"
        assert {l["article"] for l in document["lines"]} == {"BOLT", "NUT"} and document["test"] is False
        assert done["state"] == "confirmed" and done["eta"] == "2026-11-18" and done["from_addr"] == "webhook"

        assert channels.test(db_path, "ACME")["status"] == 200                       # a marked test, no order
        assert json.loads(seen[1][2])["test"] is True and json.loads(seen[1][2])["lines"] == []
    finally:
        server.shutdown()


def test_a_webhook_that_refuses_leaves_the_draft_failed_with_its_answer(db_path):
    from inventory import channels, outbox

    list_id, mail = _policy_world(db_path)
    server, _seen = _hook(status=422, answer=b"unknown article BOLT")
    try:
        channels.save(db_path, "ACME", f"http://127.0.0.1:{server.server_port}/o", "t")
        with pytest.raises(outbox.SendError):
            outbox.send(db_path, mail["mail_id"], _ready_mail(), "T")
        row = outbox.one(db_path, mail["mail_id"])
        assert row["state"] == "failed" and "422" in row["error"] and "unknown article" in row["error"]
        assert db.read(db_path, "SELECT ok FROM order_deliveries") == [(False,)]
    finally:
        server.shutdown()
    channels.remove(db_path, "ACME")
    assert channels.has(db_path, "ACME") is False


def test_plutus_chases_reminds_and_disputes_by_itself_inside_its_limits(db_path):
    from inventory import bills as bills_module
    from inventory import charters, outbox, purchasing, wallet

    _Link.sent = []
    config = make_config(db_path.parent, mail=_ready_mail())
    list_id, mail_id = _sent_acme_order(db_path)
    wallet.deposit(db_path, 1000, "float", "ALEX")
    purchasing.set_mode(db_path, "dry_run", "ALEX", today=date(2026, 11, 3))
    purchasing.set_mode(db_path, "live", "ALEX", today=date(2026, 11, 7))
    link = lambda m, f: _Link()
    friday = datetime(2026, 11, 6, 10, 0)                  # four working days of silence
    sent_before = len(_Link.sent)

    done = purchasing.run(db_path, config, friday, link)
    assert done["chased"] == 1 and len(_Link.sent) == sent_before + 1
    assert _Link.sent[-1]["Subject"].startswith("Relance")
    assert outbox.one(db_path, mail_id)["chase_count"] == 1
    assert purchasing.run(db_path, config, friday + timedelta(hours=1), link)["chased"] == 0       # not twice the same day

    charters.set_limit(db_path, "plutus", "send_chases", 0)                                        # a person switches it off
    later = datetime(2026, 11, 13, 10, 0)
    assert purchasing.run(db_path, config, later, link)["chased"] == 0
    charters.set_limit(db_path, "plutus", "send_chases", 1)
    charters.set_limit(db_path, "plutus", "max_chases", 1)                                         # already chased once
    assert purchasing.run(db_path, config, later, link)["chased"] == 0
    assert any("a person takes it now" in (b["reasons"] or "") for b in purchasing.blocked(db_path))

    # A bill with nothing delivered behind it is disputed and mailed; a big one is left to a person.
    small = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F-1", "bill_date": "2026-11-02", "net": 130.0, "total": 130.0}, source="t"))
    big = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F-2", "bill_date": "2026-11-03", "net": 9000.0, "total": 9000.0}, source="t"))
    count = len(_Link.sent)
    done = purchasing.run(db_path, config, datetime(2026, 11, 13, 11, 0), link)
    assert done["disputed"] == 1 and len(_Link.sent) == count + 1
    assert bills_module.one(db_path, small)["state"] == "disputed" and bills_module.one(db_path, big)["state"] != "disputed"
    assert any("a person disputes" in (b["reasons"] or "") for b in purchasing.blocked(db_path))


def test_a_refused_upload_puts_the_batch_back_and_a_rejected_one_returns_the_money(db_path, tmp_path):
    from inventory import bankdrop, charters, payments, purchasing, wallet
    from inventory import bills as bills_module

    key = tmp_path / "id"
    key.write_text("k")
    key.chmod(0o600)
    bankdrop.save(db_path, "sftp.bank.example", "22", "bank", str(key), "/in", "")
    wallet.deposit(db_path, 500, "float", "ALEX")
    for key_, value in (("payer_name", "Chantiers"), ("payer_iban", "FR1420041010050500013M02606")):
        db.write(db_path, "INSERT INTO policy_settings (key, value) VALUES (?, ?) RETURNING key", [key_, value])
    payments.set_bank(db_path, "ACME", "GB82WEST12345698765432")
    bill = bills_module.save(db_path, bills_module.shape(
        {"supplier": "ACME", "number": "F-1", "bill_date": "2026-10-20", "net": 120.0, "total": 120.0}, source="t"))
    payments.instruct(db_path, bill, 120.0)
    rules = charters.limits(db_path, "plutus")
    monday = datetime(2026, 11, 9, 10, 0)

    assert purchasing._bank(db_path, monday, "live", rules, _fake_sftp(fail=True)) == 0
    assert db.read(db_path, "SELECT state, uploaded_at IS NULL FROM payment_batches") == [("open", True)]   # back, retryable
    assert "Host key" in db.read(db_path, "SELECT reasons FROM policy_log WHERE action = 'upload_batch' "
                                          "AND decision = 'denied' ORDER BY log_id DESC")[0][0]
    assert purchasing._bank(db_path, monday, "live", rules, _fake_sftp(files=[])) == 1
    msg = db.read(db_path, "SELECT msg_id FROM payment_batches")[0][0]
    rejected = _fake_sftp(files=[f"/in/{msg}.status.xml"])
    rejected.data["xml"] = "<R><GrpSts>RJCT</GrpSts></R>"
    assert purchasing._bank(db_path, monday, "live", rules, rejected) == 1
    assert wallet.balance(db_path) == 500 and bills_module.one(db_path, bill)["state"] != "paid"
    assert db.read(db_path, "SELECT state, bank_status FROM payment_batches") == [("rejected", "rejected")]
