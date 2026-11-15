"""The freight broker, offline: no web, a tiny world, the same rules."""

import copy
from datetime import date, timedelta

import pytest

from inventory.broker import agent as hermes_mod
from inventory.broker import engine
from inventory.broker import network as net
from inventory.broker.sources import Sources


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    sources = Sources(tmp_path_factory.mktemp("cache"), offline=True)
    w = net.build(sources, clients=5, seed=7)
    km, minutes, road = sources.road_matrix([{"lat": p.lat, "lon": p.lon} for p in w.places()])
    assert "fallback" in road
    return w, km, minutes


def play(world, policy, agent=None, days=15, log=None):
    w, km, minutes = world
    return engine.run(copy.deepcopy(w), km, minutes, policy, engine.Fleet(**engine.VEHICLES["van"]),
                      engine.Settings(), 100, days, agent=agent, log=log)


def test_zones_follow_ddmrp():
    adu, tor, toy, tog = engine.zones([10.0] * 20, lead=2, cycle=7)
    assert adu == 10
    assert tor == pytest.approx(10 * 2 * 0.7 * 1.35)
    assert toy == pytest.approx(tor + 20)
    assert tog == pytest.approx(toy + 70)
    assert engine.zones([0.0] * 20, 2, 7) == (0.0, 0.0, 0.0, 0.0)


def test_no_money_or_fuel_in_the_results(world):
    result = play(world, "broker")
    assert not {"cost_eur", "co2_kg", "litres", "cost_per_pallet_eur", "avg_stock_value_eur"} & set(result)
    assert result["hours"] > 0 and result["avg_stock_days"] > 0
    assert set(vars(engine.Fleet())) == {"capacity", "stop_minutes"}


def test_offline_world_says_it_is_a_fallback(world):
    w, _km, _min = world
    assert len(w.clients) == 5 and w.suppliers
    assert all(p.located_by.startswith("town") for p in w.suppliers.values())   # no address base offline


def test_broker_never_late_and_drives_less_than_each_supplier_alone(world):
    solo, broker = play(world, "solo"), play(world, "broker")
    assert broker["late_releases"] == 0
    assert broker["km"] < solo["km"]
    assert broker["service_level"] >= solo["service_level"] - 0.01


def test_pooled_ships_the_same_orders_as_solo(world):
    solo, pooled = play(world, "solo"), play(world, "pooled")
    assert pooled["orders"] == solo["orders"]
    assert pooled["served"] == pytest.approx(solo["served"])


class Scripted:
    """A dispatcher that answers what the test tells it to, through the real limits."""

    def __init__(self, make_actions, days=3):
        self.hermes = hermes_mod.Hermes(backend=None, days=days, ask=self._ask)
        self.make_actions = make_actions
        self.briefs = []

    def _ask(self, brief):
        self.briefs.append(brief)
        return {"actions": self.make_actions(brief), "note": "scripted"}

    def review(self, *args):
        return self.hermes.review(*args)


def test_hermes_cannot_invent_or_exceed_limits(world):
    def actions(brief):
        out = [{"id": "O999999", "action": "pull_in", "reason": "made up"},
               {"id": "NOPE", "action": "drop", "reason": "not riding"}]
        far = [c for c in brief["not_taken"] if (c["extra_km_best_tour"] or 0) > 15]
        if far:
            out.append({"id": far[0]["id"], "action": "pull_in", "reason": "too far"})
        return out
    scripted = Scripted(actions)
    log = []
    result = play(world, "broker", agent=scripted, log=log)
    assert scripted.briefs, "Hermes was never asked"
    assert result["agent_changes"] == 0
    assert result["agent_refused"] >= 2 * len(scripted.briefs)
    assert result["late_releases"] == 0
    results = [c["result"] for d in log if d["agent"] for c in d["agent"]["changes"]]
    assert all(r.startswith("refused") for r in results)


def test_hermes_change_cap(world):
    def actions(brief):
        return [{"id": o["id"], "action": "drop", "reason": "test"} for o in brief["riding_early"]]
    scripted = Scripted(actions)
    scripted.hermes.limits["max_changes_per_day"] = 1
    log = []
    play(world, "broker", agent=scripted, log=log)
    for d in log:
        if d["agent"] and d["agent"]["asked"]:
            assert d["agent"]["applied"] <= 1


def test_hermes_failure_keeps_the_plan(world):
    def boom(brief):
        raise RuntimeError("no model")
    agent = hermes_mod.Hermes(backend=None, days=2, ask=boom)
    with_agent, without = play(world, "broker", agent=agent), play(world, "broker")
    assert with_agent["km"] == pytest.approx(without["km"])
    assert with_agent["agent_changes"] == 0


# A hand-made map: hub at 0, a near supplier, a far one (100 km), two clients in the same town.
def tiny_world(days=40):
    hub = net.Place("HUB", "hub", 0, 0)
    near = net.Place("NEAR", "supplier", 0, 0)
    far = net.Place("FAR", "supplier", 0, 0)
    c1, c2 = net.Place("C1", "client", 0, 0), net.Place("C2", "client", 0, 0)
    articles = {"A": net.Article("A", "NEAR", 10, 1.0, 0.01), "B": net.Article("B", "FAR", 10, 1.0, 0.01)}
    # C2 starts using more B after the warm-up, so the two clients reorder B on different days.
    demand = {"C1": {"A": [3.0] * days, "B": [2.0] * days}, "C2": {"A": [2.0] * days, "B": [3.0] * 12 + [6.0] * (days - 12)}}
    w = net.Network(hub, {"NEAR": near, "FAR": far}, {"C1": c1, "C2": c2}, articles,
                    [date(2026, 1, 1) + timedelta(days=i) for i in range(days)], demand)
    #        HUB  NEAR  FAR   C1   C2
    km = [[0,   10,  100,  50,  51],
          [10,  0,   100,  45,  46],
          [100, 100, 0,    90,  90],
          [50,  45,  90,   0,   1],
          [51,  46,  90,   1,   0]]
    minutes = [[d * 1.2 for d in row] for row in km]
    return w, km, minutes


def test_far_must_order_joins_a_tour_that_passes_its_town():
    w, km, _m = tiny_world()
    at = {p.name: i for i, p in enumerate(w.places())}
    passing = engine.Order("O1", "C2", "A", "NEAR", 10, 0.1, 0, 0)      # the farther client: starts the tour
    same_town = engine.Order("O2", "C1", "B", "FAR", 10, 0.1, 0, 0)     # detour over the 40 km cap
    settings = engine.Settings(insert_max_km=40)
    tours = engine.build_tours([passing, same_town], 0, km, at, engine.Fleet(), settings)
    assert len(tours) == 1          # alone it would cost a 241 km tour: joining is cheaper
    settings.insert_share = 0.0     # the old rule: the fixed cap only
    assert len(engine.build_tours([passing, same_town], 0, km, at, engine.Fleet(), settings)) == 2


def test_distant_supplier_is_visited_less_and_never_late():
    w, km, minutes = tiny_world()
    fleet = engine.Fleet(**engine.VEHICLES["van"])
    log, pooled_log = [], []
    broker = engine.run(copy.deepcopy(w), km, minutes, "broker", fleet, engine.Settings(), 10, 25, log=log)
    pooled = engine.run(copy.deepcopy(w), km, minutes, "pooled", fleet, engine.Settings(), 10, 25,
                        log=pooled_log)
    visits = lambda days: sum("FAR" in tour["stops"] for d in days for tour in d["tours"])
    assert broker["far_pickups"] > 0
    assert broker["late_releases"] == 0
    assert visits(log) < visits(pooled_log)
    assert broker["km"] < pooled["km"]
    assert broker["service_level"] >= pooled["service_level"] - 0.02


def test_nothing_driven_is_not_fully_loaded():
    out = engine._finish({"km": 0.0, "empty_km": 0.0, "pallet_km": 0.0, "capacity_km": 0.0, "minutes": 0.0,
                          "served": 0.0, "demand": 0.0, "stock_days_sum": 0.0, "pair_days": 0}, 5)
    assert out["loaded_share"] == 0.0
    assert out["fill_rate"] == 0.0 and out["hours"] == 0.0


def test_client_towns_are_never_repeated(tmp_path):
    sources = Sources(tmp_path, offline=True)
    w = net.build(sources, clients=50, seed=3)
    assert len(w.clients) == len(set(w.clients))
    assert len(w.clients) < 50 and any("clients: only" in n for n in w.notes)
