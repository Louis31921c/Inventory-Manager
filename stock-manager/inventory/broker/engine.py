"""The broker's engine: it decides when each order leaves and which vehicle carries it.

Three ways of running the same weeks, on the same demand, so the difference is the policy alone:

- `solo`: what happens today. Each client orders when its buffer says so, and each supplier
  delivers its own orders of the day from its own yard, in its own van, and drives home.
- `pooled`: the same orders on the same days, but one shared fleet from a hub collects from
  every supplier and delivers to every client in shared tours.
- `broker`: pooled, and the broker also chooses *when* each order leaves. Every order has a
  window: from the day it is written to the last day it can leave and still arrive before the
  client's stock reaches the top of red (DDMRP). An order whose window is still open waits unless
  a vehicle is passing anyway; an article close to ordering can be ordered early when it rides
  almost free. No order ever leaves after its last day: those are the "must" orders.

Buffers are DDMRP as the app works them out (FEATURES.md, section 3), from a warm-up period.
"""

import math
from dataclasses import dataclass, field
from statistics import mean, pstdev

TRANSIT = 1                         # working days from leaving to being on the client's shelf


@dataclass
class Fleet:
    capacity: float = 18.0          # pallets: a 19 t rigid
    stop_minutes: float = 20.0      # time at each pickup or drop


VEHICLES = {
    # Iveco Daily 3.5 t: what a trade merchant delivers screws, profiles and glass in.
    "van": {"capacity": 8.0, "stop_minutes": 15.0},
    "truck": {},                    # the Fleet defaults: a 19 t rigid
}


@dataclass
class Settings:
    lead_days: int = 2              # DDMRP lead: decide, pick, carry
    cycle_days: int = 7
    max_hold_days: int = 4          # the broker never holds an order longer than this
    pull_in_band: float = 0.35      # share of green above the top of yellow where ordering early is allowed
    pull_in_max_km: float = 6.0     # an early order rides only if it adds at most this
    insert_max_km: float = 40.0     # a must order may always join a tour within this detour ...
    insert_share: float = 0.8       # ... and beyond it, when the detour is under this share of a van of its own
    remote_km: float = 60.0         # a supplier further than this is visited on pickup days only (broker)
    remote_band: float = 0.6        # on its pickup day, its articles this close to ordering come too


@dataclass
class Pair:
    client: str
    article: str
    supplier: str
    pallets: float
    adu: float
    tor: float
    toy: float
    tog: float
    on_hand: float
    open: list = field(default_factory=list)

    @property
    def net_flow(self):
        return self.on_hand + sum(o.qty for o in self.open)


@dataclass
class Order:
    id: str
    client: str
    article: str
    supplier: str
    qty: float
    pallets: float
    created: int
    latest: int
    released: int = None
    arrives: int = None
    early: bool = False
    by: str = "buffer"
    stage: str = ""                 # "far": waits for its distant supplier's pickup day


def zones(series, lead, cycle):
    """(adu, top of red, top of yellow, top of green) from a demand series."""
    adu = sum(series) / max(len(series), 7)
    if adu <= 0:
        return 0.0, 0.0, 0.0, 0.0
    weeks = [sum(series[i:i + 5]) for i in range(0, len(series) - 4, 5)]
    spread = pstdev(weeks) / mean(weeks) if weeks and mean(weeks) > 0 else 0.0
    variability = 0.35 if spread <= 0.5 else 0.5 if spread <= 1.0 else 0.7
    lead_factor = 0.7 if lead <= 7 else 0.5 if lead <= 21 else 0.3
    red = adu * lead * lead_factor * (1 + variability)
    yellow = adu * lead
    green = max(adu * lead * lead_factor, adu * cycle)
    return adu, red, red + yellow, red + yellow + green


# Tours --------------------------------------------------------------------------------------

def _nearest(start, stops, km):
    order, left, here = [], set(stops), start
    while left:
        here = min(left, key=lambda s: km[here][s])
        order.append(here)
        left.discard(here)
    return order


def _two_opt(start, order, end, km):
    def length(seq):
        path = [start, *seq, end]
        return sum(km[path[i]][path[i + 1]] for i in range(len(path) - 1))
    best, improved = length(order), True
    while improved and len(order) > 2:
        improved = False
        for i in range(len(order) - 1):
            for j in range(i + 1, len(order)):
                trial = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if (got := length(trial)) < best - 1e-9:
                    order, best, improved = trial, got, True
    return order


def route(orders, base, km, at, own_yard=None):
    """The tour that carries `orders`: from `base`, collect, deliver, come back.

    With `own_yard` (a supplier delivering its own), the goods are on board from the start.
    Gives back {stops, km, empty_km, pallet_km, pallets, visits}.
    """
    drops = {}
    for o in orders:
        drops[at[o.client]] = drops.get(at[o.client], 0.0) + o.pallets
    total = sum(drops.values())
    if own_yard is None:
        pickups = {}
        for o in orders:
            pickups[at[o.supplier]] = pickups.get(at[o.supplier], 0.0) + o.pallets
        first = _two_opt(base, _nearest(base, pickups, km), base, km) if pickups else []
        last_pick = first[-1] if first else base
    else:
        pickups, first, last_pick = {}, [], base
    second = _two_opt(last_pick, _nearest(last_pick, drops, km), base, km)
    path = [base, *first, *second, base]
    load, distance, empty, pallet_km = (0.0 if own_yard is None else total), 0.0, 0.0, 0.0
    for i in range(len(path) - 1):
        leg = km[path[i]][path[i + 1]]
        distance += leg
        if load <= 1e-9:
            empty += leg
        pallet_km += load * leg
        nxt = path[i + 1]
        if i + 1 <= len(first):
            load += pickups.get(nxt, 0.0)
        elif i + 1 < len(path) - 1:
            load -= drops.get(nxt, 0.0)
    return {"stops": path, "km": distance, "empty_km": empty, "pallet_km": pallet_km,
            "pallets": total, "visits": len(first) + len(second)}


def _length(orders, base, km, at, own_yard=None):
    return route(orders, base, km, at, own_yard)["km"] if orders else 0.0


def build_tours(must, base, km, at, fleet, settings, own_yard=None):
    """Group the orders that must leave today into as few, as short tours as the vehicles allow."""
    left = sorted(must, key=lambda o: -km[base][at[o.client]])
    tours = []
    while left:
        tour = [left.pop(0)]
        load = tour[0].pallets
        while True:
            here = _length(tour, base, km, at, own_yard)
            best = None
            for o in left:
                if load + o.pallets > fleet.capacity:
                    continue
                extra = _length(tour + [o], base, km, at, own_yard) - here
                alone = _length([o], base, km, at, own_yard)
                allowed = max(settings.insert_max_km, settings.insert_share * alone)
                if extra <= allowed and (best is None or extra < best[0]):
                    best = (extra, o)
            if best is None:
                break
            tour.append(best[1])
            left.remove(best[1])
            load += best[1].pallets
        tours.append(tour)
    return tours


def best_slot(tours, order, base, km, at, fleet):
    """(extra km, tour index) of the cheapest tour that has room for `order`, or (None, None)."""
    best = (None, None)
    for i, tour in enumerate(tours):
        if sum(o.pallets for o in tour) + order.pallets > fleet.capacity:
            continue
        extra = _length(tour + [order], base, km, at) - _length(tour, base, km, at)
        if best[0] is None or extra < best[0]:
            best = (extra, i)
    return best


# The run ------------------------------------------------------------------------------------

def _pairs(network, warmup, settings):
    pairs = []
    for client, articles in network.demand.items():
        for name, series in articles.items():
            article = network.articles[name]
            adu, tor, toy, tog = zones(series[:warmup], settings.lead_days, settings.cycle_days)
            if adu <= 0:
                continue
            pairs.append(Pair(client, name, article.supplier, article.pallets,
                              adu, tor, toy, tog, on_hand=round((toy + tog) / 2, 2)))
    return pairs


def run(network, km, minutes, policy, fleet, settings, warmup, horizon, agent=None, log=None):
    """Play `horizon` working days after `warmup` under one policy. Gives back the totals."""
    sim = _Run(network, km, minutes, policy, fleet, settings, warmup, agent)
    for t in range(warmup, warmup + horizon):
        day = sim.day(t)
        if log is not None:
            log.append(day)
    return _finish(sim.totals, horizon)


class _Run:
    """The state of one run, and the steps of one working day."""

    def __init__(self, network, km, minutes, policy, fleet, settings, warmup, agent):
        self.network, self.km, self.minutes = network, km, minutes
        self.policy, self.fleet, self.settings, self.agent = policy, fleet, settings, agent
        self.places = network.places()
        self.at = {p.name: i for i, p in enumerate(self.places)}
        self.hub = self.at[network.hub.name]
        self.pairs = _pairs(network, warmup, settings)
        self.pair_of = {(p.client, p.article): p for p in self.pairs}
        self.counter = 0
        self.waiting = []               # orders written, not yet left
        self.remote = ({s for s in network.suppliers if km[self.hub][self.at[s]] > settings.remote_km}
                       if policy == "broker" else set())
        self.totals = {"policy": policy, "tours": 0, "trucks": 0, "km": 0.0, "empty_km": 0.0,
                       "pallet_km": 0.0, "capacity_km": 0.0, "minutes": 0.0, "stops": 0, "pallets": 0.0,
                       "orders": 0, "early_orders": 0, "far_pickups": 0, "held_order_days": 0,
                       "late_releases": 0, "demand": 0.0, "served": 0.0, "stockout_events": 0,
                       "stock_days_sum": 0.0, "pair_days": 0, "agent_changes": 0, "agent_refused": 0}

    def day(self, t):
        log = {"day": t, "date": self.network.days[t].isoformat(), "tours": [], "pulled": [],
               "agent": None, "far_pickups": []}
        self._arrive(t)
        self._consume(t)
        self._write_orders(t)
        self._far_pickups(t, log)
        tours = self._plan(t, log)
        self._drive(t, tours, log)
        self.totals["held_order_days"] += len(self.waiting)
        return log

    # 1. What arrives today goes on the shelf.
    def _arrive(self, t):
        for pair in self.pairs:
            for o in [o for o in pair.open if o.arrives == t]:
                pair.on_hand += o.qty
                pair.open.remove(o)

    # 2. The clients use what they use; what is not on the shelf is a stockout.
    def _consume(self, t):
        for pair in self.pairs:
            wanted = self.network.demand[pair.client][pair.article][t]
            served = min(pair.on_hand, wanted)
            self.totals["demand"] += wanted
            self.totals["served"] += served
            if wanted - served > 1e-9:
                self.totals["stockout_events"] += 1
            pair.on_hand -= served
            self.totals["stock_days_sum"] += pair.on_hand / pair.adu       # days of use on the shelf
            self.totals["pair_days"] += 1

    # 3. A buffer at or under the top of yellow writes an order up to the top of green.
    def _write_orders(self, t):
        for pair in self.pairs:
            if pair.net_flow <= pair.toy:
                qty = math.ceil(pair.tog - pair.net_flow)
                coming = sum(o.qty for o in pair.open if o.released is not None)
                days_left = (pair.on_hand + coming - pair.tor) / pair.adu
                latest = t + max(0, min(self.settings.max_hold_days, int(days_left) - TRANSIT))
                self._new_order(pair, qty, t, latest, stage="far" if pair.supplier in self.remote else "")

    def _new_order(self, pair, qty, t, latest, early=False, by="buffer", stage=""):
        self.counter += 1
        o = Order(f"O{self.counter}", pair.client, pair.article, pair.supplier, qty, qty * pair.pallets,
                  t, latest, early=early, by=by, stage=stage)
        pair.open.append(o)
        self.waiting.append(o)
        self.totals["orders"] += 1
        self.totals["early_orders"] += early
        return o

    def _near_ordering(self, pair, band):
        """Nothing open, and at most `band` of green above the top of yellow."""
        return not pair.open and pair.net_flow <= pair.toy + band * (pair.tog - pair.toy)

    # 4a. Distant suppliers: their orders wait for one pickup day, the day the first of them can
    # wait no longer. Then everything of theirs that waits, and what is close to ordering, goes.
    def _far_pickups(self, t, log):
        for supplier in sorted(self.remote):
            theirs = [o for o in self.waiting if o.stage == "far" and o.supplier == supplier]
            if not any(o.latest <= t for o in theirs):
                continue
            for pair in self.pairs:
                if pair.supplier == supplier and self._near_ordering(pair, self.settings.remote_band):
                    qty = math.ceil(pair.tog - pair.net_flow)
                    theirs.append(self._new_order(pair, qty, t, t, early=True, by="pull-in"))
            for o in theirs:
                o.stage, o.early = "", o.early or o.latest > t
                o.latest = min(o.latest, t)             # all of it leaves today
            self.totals["far_pickups"] += 1
            log["far_pickups"].append({"supplier": supplier, "orders": [o.id for o in theirs],
                                       "pallets": round(sum(o.pallets for o in theirs), 3)})

    # 4b. Which orders leave today, and in which vehicles: [(base, orders, own yard or None)].
    def _plan(self, t, log):
        km, at, fleet, settings = self.km, self.at, self.fleet, self.settings
        if self.policy == "broker":
            must = [o for o in self.waiting if o.stage == "" and o.latest <= t]
            optional = [o for o in self.waiting if o.stage == "" and o.latest > t]
        else:
            must, optional = list(self.waiting), []

        if self.policy == "solo":
            by_supplier = {}
            for o in must:
                by_supplier.setdefault(o.supplier, []).append(o)
            return [(at[s], tour, at[s]) for s, orders in by_supplier.items()
                    for tour in build_tours(orders, at[s], km, at, fleet, settings, own_yard=at[s])]

        shared = build_tours(must, self.hub, km, at, fleet, settings)
        if self.policy == "broker" and shared:
            self._ride_along(t, shared, optional, log)
        return [(self.hub, tour, None) for tour in shared]

    # The broker's riders: open-window orders and near-orders on vans that pass anyway; then Hermes.
    def _ride_along(self, t, shared, optional, log):
        candidates = list(optional)
        for pair in self.pairs:
            if (pair.supplier not in self.remote and pair.net_flow > pair.toy
                    and self._near_ordering(pair, self.settings.pull_in_band)):
                qty = math.ceil(pair.tog - pair.net_flow)
                candidates.append(Order(f"P-{pair.client}-{pair.article}", pair.client, pair.article,
                                        pair.supplier, qty, qty * pair.pallets, t, t, early=True,
                                        by="pull-in"))
        taken, passed = _fill(shared, candidates, self.hub, self.km, self.at, self.fleet,
                              self.settings.pull_in_max_km)
        if self.agent is not None:
            brief = _brief(self.network, t, shared, taken, passed, self.waiting, self.hub, self.km, self.at,
                           self.fleet)
            changes = self.agent.review(brief, t, shared, taken, passed, self.hub, self.km, self.at, self.fleet)
            self.totals["agent_changes"] += changes["applied"]
            self.totals["agent_refused"] += changes["refused"]
            log["agent"] = changes
        for o in taken:
            if o.by == "pull-in":       # a near-order becomes a real order now that it rides
                self.counter += 1
                o.id = f"O{self.counter}"
                self.pair_of[(o.client, o.article)].open.append(o)
                self.totals["orders"] += 1
                self.totals["early_orders"] += 1
            else:
                o.early = True
            log["pulled"].append(o.id)

    # 5. The vehicles go.
    def _drive(self, t, tours, log):
        for base, tour, yard in tours:
            r = route(tour, base, self.km, self.at, own_yard=yard)
            drive = sum(self.minutes[r["stops"][i]][r["stops"][i + 1]] for i in range(len(r["stops"]) - 1))
            _account(self.totals, self.fleet, r["km"], r["empty_km"], r["pallet_km"], r["pallets"], drive,
                     r["visits"])
            for o in tour:
                if o.latest < t:
                    self.totals["late_releases"] += 1
                o.released, o.arrives = t, t + TRANSIT
                if o in self.waiting:
                    self.waiting.remove(o)
            log["tours"].append({"stops": [self.places[i].name for i in r["stops"]],
                                 "km": round(r["km"], 1), "pallets": round(r["pallets"], 2),
                                 "orders": [o.id for o in tour]})


def _account(totals, fleet, km, empty_km, pallet_km, pallets, drive_minutes, visits):
    """Add one tour to the totals: vehicles, km, load and time."""
    trucks = max(1, math.ceil(pallets / fleet.capacity - 1e-9))
    totals["tours"] += 1
    totals["trucks"] += trucks
    totals["km"] += km * trucks
    totals["empty_km"] += empty_km * trucks
    totals["pallet_km"] += pallet_km
    totals["capacity_km"] += km * trucks * fleet.capacity
    totals["minutes"] += drive_minutes + visits * fleet.stop_minutes
    totals["stops"] += visits
    totals["pallets"] += pallets


def _fill(tours, candidates, base, km, at, fleet, max_km):
    """Put the cheapest riders on the tours that pass anyway. Gives back (taken, passed over)."""
    taken, passed = [], []
    scored = []
    for o in candidates:
        extra, i = best_slot(tours, o, base, km, at, fleet)
        scored.append((extra if extra is not None else math.inf, o))
    for _score, o in sorted(scored, key=lambda s: s[0]):
        extra, i = best_slot(tours, o, base, km, at, fleet)
        if extra is not None and extra <= max_km:
            tours[i].append(o)
            taken.append(o)
        else:
            passed.append((o, extra, i))
    return taken, passed


def _brief(network, t, tours, taken, passed, waiting, base, km, at, fleet):
    """What the dispatcher is shown each day: compact, numbers only."""
    def zone(place):
        p = network.places()[at[place]]
        return f"{p.lat:.2f},{p.lon:.2f}"
    coming = {}
    for o in waiting:
        if o not in taken and t < o.latest <= t + 3:
            coming.setdefault(o.client, []).append(o.latest - t)
    return {
        "day": network.days[t].isoformat(),
        "vehicle_capacity_pallets": fleet.capacity,
        "tours_today": [{"tour": i, "clients": sorted({o.client for o in tour}),
                         "suppliers": sorted({o.supplier for o in tour}),
                         "pallets": round(sum(o.pallets for o in tour), 2),
                         "km": round(_length(tour, base, km, at), 1)} for i, tour in enumerate(tours)],
        "riding_early": [{"id": o.id, "client": o.client, "supplier": o.supplier, "article": o.article,
                          "pallets": round(o.pallets, 3), "kind": o.by} for o in taken],
        "not_taken": [{"id": o.id, "client": o.client, "client_at": zone(o.client), "supplier": o.supplier,
                       "article": o.article, "pallets": round(o.pallets, 3), "kind": o.by,
                       "must_leave_in_days": max(0, o.latest - t),
                       "extra_km_best_tour": None if extra is None else round(extra, 1),
                       "best_tour": i}
                      for o, extra, i in passed if extra is not None][:40],
        "due_next_3_days_by_client": {c: sorted(d) for c, d in coming.items()},
    }


def _finish(totals, horizon):
    out = dict(totals)
    out["days"] = horizon
    out["loaded_share"] = 1 - totals["empty_km"] / totals["km"] if totals["km"] else 0.0
    out["fill_rate"] = totals["pallet_km"] / (totals["capacity_km"] or 1.0)
    out["service_level"] = totals["served"] / (totals["demand"] or 1.0)
    out["avg_stock_days"] = totals["stock_days_sum"] / (totals["pair_days"] or 1)
    out["hours"] = totals["minutes"] / 60
    return out
