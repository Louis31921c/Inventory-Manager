"""python -m inventory.broker run [--clients 12] [--days 60] [--agent] [--offline]"""

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import engine
from . import network as net
from .sources import Sources

POLICIES = ("solo", "pooled", "broker")
LABELS = {"solo": "Today (each supplier alone)", "pooled": "Shared fleet", "broker": "Broker (timing + shared)"}


def default_dir():
    return Path(os.environ.get("DATA_DIR") or "data").expanduser()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m inventory.broker")
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run", help="play the same weeks solo, pooled and with the broker, and compare")
    go.add_argument("--clients", type=int, default=12)
    go.add_argument("--days", type=int, default=60, help="working days to play after the warm-up")
    go.add_argument("--warmup", type=int, default=100, help="working days used to size the buffers")
    go.add_argument("--seed", type=int, default=2026)
    go.add_argument("--dept", default="33", help="French département the clients are in")
    go.add_argument("--vehicle", choices=sorted(engine.VEHICLES), default="van")
    go.add_argument("--capacity", type=float, default=None, help="pallets per vehicle (overrides the vehicle's)")
    go.add_argument("--pull-km", type=float, default=6.0, help="km an early order may add")
    go.add_argument("--hold", type=int, default=4, help="most days the broker may hold an order")
    go.add_argument("--agent", action="store_true", help="let Hermes (Claude) review the broker's days")
    go.add_argument("--agent-days", type=int, default=5, help="how many days Hermes is asked about")
    go.add_argument("--agent-desk", default=None,
                    help="folder: write each brief there and wait for an answer file, instead of asking Claude")
    go.add_argument("--offline", action="store_true", help="use only the cache, never the web")
    go.add_argument("--out", default=None, help="folder for the run's JSON (default DATA_DIR/broker_runs)")
    args = parser.parse_args(argv)

    base = default_dir()
    sources = Sources(base / "broker_cache", offline=args.offline)
    started = time.monotonic()
    say("Pulling data: towns, supplier addresses, road distances ...")
    world = net.build(sources, clients=args.clients, seed=args.seed, dept=args.dept)
    if args.warmup + args.days > len(world.days):
        sys.exit(f"Only {len(world.days)} working days of demand: lower --warmup or --days.")
    km, minutes, road = sources.road_matrix([{"lat": p.lat, "lon": p.lon} for p in world.places()])

    fleet = engine.Fleet(**engine.VEHICLES[args.vehicle])
    if args.capacity:
        fleet.capacity = args.capacity
    settings = engine.Settings(pull_in_max_km=args.pull_km, max_hold_days=args.hold)
    agent = None
    if args.agent_desk:
        from .agent import Desk, Hermes
        agent = Hermes(None, days=args.agent_days, ask=Desk(args.agent_desk))
        say(f"Hermes desk: briefs go to {args.agent_desk}, waiting for answer files")
    elif args.agent:
        from .. import llm
        from .agent import Hermes
        agent = Hermes(llm.ClaudeCodeBackend(timeout=180), days=args.agent_days)

    results, logs = {}, {}
    for policy in POLICIES:
        say(f"Playing {args.days} working days: {LABELS[policy]} ...")
        logs[policy] = []
        results[policy] = engine.run(copy.deepcopy(world), km, minutes, policy, fleet, settings,
                                     args.warmup, args.days,
                                     agent=agent if policy == "broker" else None, log=logs[policy])

    report = {
        "when": datetime.now().isoformat(timespec="seconds"),
        "period": [world.days[args.warmup].isoformat(), world.days[args.warmup + args.days - 1].isoformat()],
        "settings": vars(args), "fleet": vars(fleet), "broker": vars(settings),
        "data": {"roads": road, "sources": sources.used, "notes": world.notes,
                 "suppliers": {n: {"at": p.address, "located_by": p.located_by, "lat": p.lat, "lon": p.lon}
                               for n, p in world.suppliers.items()},
                 "clients": {n: {"town": p.address, "lat": p.lat, "lon": p.lon} for n, p in world.clients.items()}},
        "results": results, "days": logs,
    }
    out = Path(args.out) if args.out else base / "broker_runs"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"run-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, indent=1, default=str))
    print(render(report))
    say(f"Saved {path} ({time.monotonic() - started:.0f} s)")


def render(report):
    r, d = report["results"], report["data"]
    rows = [
        ("Vehicle tours", "tours", "{:,.0f}"), ("Vehicles sent", "trucks", "{:,.0f}"),
        ("Km driven", "km", "{:,.0f}"), ("Km empty", "empty_km", "{:,.0f}"),
        ("Loaded share of km", "loaded_share", "{:.1%}"), ("Vehicle fill (pallet-km)", "fill_rate", "{:.1%}"),
        ("Stops", "stops", "{:,.0f}"), ("Hours on the road (incl. stops)", "hours", "{:,.0f}"),
        ("Orders", "orders", "{:,.0f}"),
        ("  of which early (pulled in)", "early_orders", "{:,.0f}"),
        ("Pickup days, distant suppliers", "far_pickups", "{:,.0f}"),
        ("Order-days held back", "held_order_days", "{:,.0f}"), ("Orders released late", "late_releases", "{:,.0f}"),
        ("Service level (units served)", "service_level", "{:.2%}"), ("Stockout events", "stockout_events", "{:,.0f}"),
        ("Avg stock on shelves (days of use)", "avg_stock_days", "{:,.1f}"),
    ]
    if r["broker"]["agent_changes"] or r["broker"]["agent_refused"]:
        rows += [("Hermes changes applied", "agent_changes", "{:,.0f}"),
                 ("Hermes changes refused", "agent_refused", "{:,.0f}")]
    width = 34
    lines = [(f"\nPeriod {report['period'][0]} to {report['period'][1]}, "
              f"{len(d['clients'])} clients, {len(d['suppliers'])} suppliers, distances: {d['roads']}"),
             "", f"{'':{width}}" + "".join(f"{LABELS[p]:>28}" for p in POLICIES)]
    for label, key, fmt in rows:
        lines.append(f"{label:{width}}" + "".join(f"{fmt.format(r[p][key]):>28}" for p in POLICIES))
    solo, broker = r["solo"], r["broker"]
    if solo["km"]:
        lines += ["", (f"Broker vs today: {1 - broker['km'] / solo['km']:+.1%} km saved, "
                       f"{1 - broker['empty_km'] / (solo['empty_km'] or 1):+.1%} empty km saved, "
                       f"{1 - broker['trucks'] / (solo['trucks'] or 1):+.1%} vehicles saved, "
                       f"service {broker['service_level'] - solo['service_level']:+.2%} points")]
    return "\n".join(lines)


def say(text):
    print(text, file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
