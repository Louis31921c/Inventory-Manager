"""The day's deliveries, planned and followed without a person.

Customer orders (every purchase order Plutus did not write) carry a ship-by day. Each evening, for the
next working day, every order that is due and has no dispatch yet becomes one:

  * the lines on the shelf go on it (lines ticked "to order" wait for their goods)
  * orders for the same site share a van, a stop; a van takes at most `dispatch_max_stops` stops a day
  * what does not fit a van goes to the default carrier (carriers.py), booked on the spot
  * with no van and no carrier it is planned without one, and says so as an alert

After the day, a dispatch nobody has signed for is a problem that grows louder each day: the
board lists it, the watch raises it, and a carrier shipment is asked about by the tracking pass.
Nothing here moves money; the setting `dispatch_auto` switches the planning off.
"""

from datetime import date, datetime, timedelta

from . import carriers, db, outbound, routing, workdays

LOOKBACK_DAYS = 3                  # an order whose ship-by day is older than this was missed, not due
DEFAULTS = {"dispatch_auto": "1", "dispatch_max_stops": "6", "dispatch_cutoff_hour": "14"}


def settings(db_path):
    kept = dict(db.read(db_path, "SELECT key, value FROM policy_settings WHERE key LIKE 'dispatch_%'"))
    merged = {**DEFAULTS, **kept}
    return {"auto": merged["dispatch_auto"] == "1", "max_stops": int(merged["dispatch_max_stops"]),
            "cutoff_hour": int(merged["dispatch_cutoff_hour"])}


def save_settings(db_path, auto=None, max_stops=None, cutoff_hour=None):
    for key, value in (("dispatch_auto", None if auto is None else ("1" if auto else "0")),
                       ("dispatch_max_stops", None if max_stops is None else str(int(max_stops))),
                       ("dispatch_cutoff_hour", None if cutoff_hour is None else str(int(cutoff_hour)))):
        if value is None:
            continue
        if key == "dispatch_max_stops" and not 1 <= int(value) <= 50:
            raise ValueError("A van takes between 1 and 50 stops.")
        if key == "dispatch_cutoff_hour" and not 0 <= int(value) <= 23:
            raise ValueError("The hour is from 0 to 23.")
        db.write(db_path, "INSERT INTO policy_settings (key, value) VALUES (?, ?) "
                          "ON CONFLICT (key) DO UPDATE SET value = excluded.value RETURNING key", [key, value])
    return settings(db_path)


def due(db_path, day):
    """Orders to ship by `day` that have no dispatch yet, oldest first, and the ones missed."""
    rows = db.read(db_path, """
            SELECT h.list_id, coalesce(h.site, ''), coalesce(h.work_item, ''), h.ship_by
            FROM purchase_orders h
            WHERE upper(coalesce(h.drafter, '')) <> 'PLUTUS' AND h.ship_by IS NOT NULL AND h.ship_by <= ?
              AND NOT EXISTS (SELECT 1 FROM dispatches d WHERE d.list_id = h.list_id AND d.state <> 'failed')
              AND EXISTS (SELECT 1 FROM purchase_order_lines l WHERE l.list_id = h.list_id
                          AND l.quantity > 0 AND l.in_stock IS NOT FALSE)
            ORDER BY h.ship_by, h.site, h.list_id""", [day])
    fresh, missed = [], []
    for list_id, site, work, ship_by in rows:
        (missed if ship_by < day - timedelta(days=LOOKBACK_DAYS) else fresh).append(
            {"list_id": list_id, "site": site, "work_item": work, "ship_by": ship_by.isoformat()})
    return fresh, missed


def _stops_by_van(db_path, day):
    """vehicle_id -> sites already on its dispatches that day."""
    held = {}
    for vehicle_id, site in db.read(db_path, """
            SELECT vehicle_id, coalesce(site, '') FROM dispatches
            WHERE planned_on = ? AND vehicle_id IS NOT NULL AND state <> 'failed'""", [day]):
        held.setdefault(vehicle_id, set()).add(site)
    return held


def plan(db_path, day, user="DISPATCH", book=carriers.book, post=None):
    """Plan every due order for `day`. Gives back {planned: [...], unplaced: [...], missed: [...]}."""
    fresh, missed = due(db_path, day)
    vans = routing.vehicles(db_path)
    limit = settings(db_path)["max_stops"]
    on_van = _stops_by_van(db_path, day)
    chosen_carrier = carriers.default(db_path)
    planned, unplaced = [], []
    for order in fresh:
        site = order["site"]
        vehicle = next((v["vehicle_id"] for v in vans if site in on_van.get(v["vehicle_id"], set())), None)
        if vehicle is None:
            room = [(len(on_van.get(v["vehicle_id"], set())), v["vehicle_id"]) for v in vans
                    if len(on_van.get(v["vehicle_id"], set())) < limit]
            vehicle = min(room)[1] if room else None
        try:
            made = outbound.create_from_order(db_path, order["list_id"], vehicle, day.isoformat(), user,
                                              "planned by the dispatcher", skip_to_order=True)
        except ValueError as e:
            unplaced.append({**order, "why": str(e)})
            continue
        if vehicle is not None:
            on_van.setdefault(vehicle, set()).add(site)
            planned.append({**order, "dispatch_id": made["dispatch_id"], "by": made["vehicle"]})
            continue
        if chosen_carrier:
            try:
                shipment = book(db_path, made["dispatch_id"], chosen_carrier, user,
                                **({"post": post} if post else {}))
                planned.append({**order, "dispatch_id": made["dispatch_id"], "by": chosen_carrier,
                                "tracking": shipment["tracking"]})
                continue
            except ValueError as e:
                unplaced.append({**order, "dispatch_id": made["dispatch_id"], "why": str(e)})
                continue
        unplaced.append({**order, "dispatch_id": made["dispatch_id"], "why": "no van has room and no carrier is set up"})
    return {"day": day.isoformat(), "planned": planned, "unplaced": unplaced, "missed": missed}


def target_day(db_path, now):
    """Before the cut-off hour the day's own orders are planned; after it, the next working day's."""
    if now.hour < settings(db_path)["cutoff_hour"]:
        return now.date()
    return workdays.Calendar(db_path).next_working(now.date() + timedelta(days=1))


def run(db_path, now=None, **kwargs):
    now = now or datetime.now()
    if not settings(db_path)["auto"]:
        return {"skipped": "the dispatcher is off", "planned": [], "unplaced": [], "missed": []}
    return plan(db_path, target_day(db_path, now), **kwargs)


def unsigned(db_path, today=None):
    """Dispatches whose day has passed with nobody's signature, with how many days late."""
    today = today or date.today()
    out = []
    for d in outbound.listing(db_path, 300):
        if d["state"] in ("delivered", "failed") or not d["planned_on"]:
            continue
        days = (today - date.fromisoformat(d["planned_on"])).days
        if days >= 1:
            shipment = carriers.for_dispatch(db_path, d["dispatch_id"])
            out.append({**d, "days_late": days, "carrier": shipment["carrier"] if shipment else None,
                        "tracking": shipment["tracking"] if shipment else None,
                        "shipment_state": shipment["state"] if shipment else None})
    return sorted(out, key=lambda r: -r["days_late"])


def board(db_path, today=None):
    """What the Deliveries card shows: open dispatches and the last week's, with how each is carried."""
    today = today or date.today()
    rows = []
    for d in outbound.listing(db_path, 100):
        recent = d["planned_on"] and (today - date.fromisoformat(d["planned_on"])).days <= 7
        if d["state"] in ("delivered", "failed", "part delivered") and not recent:
            continue
        shipment = carriers.for_dispatch(db_path, d["dispatch_id"])
        days = ((today - date.fromisoformat(d["planned_on"])).days if d["planned_on"] else 0)
        rows.append({**d, "carried_by": shipment["carrier"] if shipment else (d["vehicle"] or None),
                     "tracking": shipment["tracking"] if shipment else None,
                     "shipment_state": shipment["state"] if shipment else None,
                     "eta": shipment["eta"] if shipment else None,
                     "late": d["state"] not in ("delivered", "failed", "part delivered") and days >= 1})
    return {"dispatches": rows, "settings": settings(db_path), "carriers": carriers.listing(db_path)}
