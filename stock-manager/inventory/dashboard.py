"""The landing dashboard: what is short, what it is blocking, why, and what is open.

A shortage is an article that will not cover what is needed. What is needed comes from two
places: stock held back for a work item (a reservation with a day it is needed by), and the
lines of a stored order ticked "to order". An article that is used but is at zero, or will run
out before a delivery could arrive, is a shortage too, even when nothing names it.

Each shortage gets a status, the day it is needed, the work it blocks, the supplier and how
long that supplier really takes, and the root cause: the first of these that is true, from
the nearest to the shelf to the furthest away.
"""

from datetime import date, datetime, timedelta

from . import db, forecast, gate, leadtimes, outbox, stock, stockctl, watch, workdays

HORIZON_DAYS = 14
LINE_DOWN_DAYS = 3
CRITICAL_DAYS = 7
RANK = {"line down": 0, "critical": 1, "at risk": 2}

ACTIONS = {
    "no order placed": "draft the order to the supplier",
    "waiting in the 15-day queue": "order now, or accept the wait",
    "order drafted, not sent": "send the draft",
    "order sent, no confirmation": "chase the supplier",
    "supplier ETA after the need date": "ask for a partial delivery or another supplier",
    "order late": "chase the supplier",
    "held for approval": "approve or cancel the basket",
    "part delivered, the rest is owed": "chase the rest of the delivery",
}


def requirements(db_path, today, horizon=HORIZON_DAYS):
    """article -> [{need_date, site, work_item, quantity, source}] for the next `horizon` days."""
    until = today + timedelta(days=horizon)
    out = {}
    for held in stockctl.reservations(db_path, today):
        if held["left"] <= 0:
            continue
        need = date.fromisoformat(held["needed_by"]) if held["needed_by"] else today
        if need <= until:
            out.setdefault(held["article"], []).append({
                "need_date": need, "site": held["site"], "work_item": held["work_item"],
                "quantity": held["left"], "source": "reservation"})
    for article, quantity, site, work, ship_by, day in db.read(db_path, """
            SELECT l.article, l.quantity, h.site, h.work_item, h.ship_by,
                   coalesce(h.list_date, CAST(h.created_at AS DATE))
            FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
            WHERE l.in_stock = FALSE AND l.quantity IS NOT NULL
              AND upper(coalesce(h.drafter, '')) <> 'PLUTUS'"""):
        need = ship_by or max(day, today)
        if need <= until:
            out.setdefault(article, []).append({
                "need_date": need, "site": site or "", "work_item": work or "",
                "quantity": float(quantity), "source": "order line"})
    return out


def _cause(db_path, article, supplier, lead, days_left, today):
    """The first reason this article is not being fixed, nearest the shelf first."""
    for held in gate.waiting(db_path):
        if any((line.get("article") or "").upper() == article for line in held["lines"]):
            return "held for approval", f"basket #{held['request_id']} is waiting for you"
    like = f"%{article}%"
    for thread in outbox.threads(db_path, today):
        body = db.read(db_path, "SELECT body FROM supplier_mail WHERE mail_id = ?", [thread["mail_id"]])
        if thread["kind"] == "order" and body and article in (body[0][0] or "").upper():
            expected = date.fromisoformat(thread["expected_on"])
            if thread["state"] == "sent" and thread["needs_chase"]:
                return "order sent, no confirmation", f"order #{thread['ref_id']} to {thread['supplier']}"
            if expected > today + timedelta(days=days_left):
                return "supplier ETA after the need date", f"expected {expected.isoformat()}"
            if expected < today:
                return "order late", f"order #{thread['ref_id']} was expected {expected.isoformat()}"
            return None, f"on order, expected {expected.isoformat()}"
    if db.read(db_path, "SELECT 1 FROM supplier_mail WHERE direction = 'out' AND kind = 'order' "
                        "AND state IN ('draft', 'failed') AND upper(body) LIKE ?", [like]):
        return "order drafted, not sent", "a draft is waiting"
    queued = db.read(db_path, "SELECT min(due_on) FROM order_queue WHERE state = 'waiting' "
                              "AND article = ?", [article])
    if queued and queued[0][0]:
        return f"Plutus orders it on {queued[0][0]}"
    if db.read(db_path, "SELECT 1 FROM inventory_lines WHERE article = ? AND backorder "
                        "AND settled_on IS NULL", [article]):
        return "part delivered, the rest is owed",
    if lead is not None and lead > max(days_left, 0):
        return "no order placed", (f"nothing is ordered, and the supplier takes {lead:g} days "
                                   f"against {max(days_left, 0)} left")


def shortages(db_path, today=None):
    today = today or date.today()
    plan, _span = forecast.articles(db_path)
    by_article = {row["article"]: row for row in plan}
    need = requirements(db_path, today)
    shelf = stock.on_hand_map(db_path)
    calendar = workdays.Calendar(db_path)
    out = []

    for article in sorted(set(need) | {r["article"] for r in plan if r["state"] == "short"
                                       or (r["on_hand"] is not None and r["on_hand"] <= 0
                                           and r["per_day"] > 0)}):
        row = by_article.get(article)
        have = shelf.get(article, 0.0)
        wanted = sum(item["quantity"] for item in need.get(article, []))
        lead_demand = row["lead_demand"] if row else 0.0
        units = max(0.0, wanted - have, (lead_demand - have) if row else 0.0)
        if units <= 0:
            continue
        needs = need.get(article, [])
        if needs:
            when = min(item["need_date"] for item in needs)
        else:
            cover = row["cover_days"] if row and row["cover_days"] is not None else 0
            when = calendar.next_working(today + timedelta(days=max(0, int(cover))))
        days_left = (when - today).days
        supplier = (row or {}).get("supplier") or outbox.supplier_of(db_path, article)
        lead = (row or {}).get("lead_days")
        if lead is None and supplier:
            lead = leadtimes.for_supplier(db_path, supplier)
        down = have <= 0 and (row is None or row["per_day"] > 0 or bool(needs))
        status = ("line down" if down and days_left <= LINE_DOWN_DAYS
                  else "critical" if days_left <= CRITICAL_DAYS else "at risk")
        found = _cause(db_path, article, supplier, lead, days_left, today)
        if isinstance(found, str):
            found = (found, "")
        cause, why = (tuple(found or ()) + (None, ""))[:2] if found else (None, "")
        blocks = []
        for item in sorted(needs, key=lambda i: i["need_date"]):
            label = " / ".join(x for x in (item["site"], item["work_item"]) if x) or "unassigned"
            if label not in blocks:
                blocks.append(label)
        out.append({
            "article": article, "status": status, "shortage": round(units, 2),
            "on_hand": round(have, 2), "need_date": when.isoformat(), "days_left": days_left,
            "blocks": blocks[:4], "supplier": supplier,
            "lead_days": round(lead, 1) if lead is not None else None,
            "cause": cause or "being handled", "cause_detail": why,
            "action": ("ask for an express delivery or another supplier"
                       if cause == "no order placed" and lead is not None and lead > max(days_left, 0)
                       else ACTIONS.get(cause, "none needed, an order is on its way"))})
    out.sort(key=lambda r: (RANK[r["status"]], r["need_date"], -r["shortage"]))
    return out


def build(db_path, today=None):
    today = today or date.today()
    items = shortages(db_path, today)
    actions = watch.open_notices(db_path, limit=12)
    return {
        "as_of": today.isoformat(),
        "kpis": {"line_down": sum(1 for r in items if r["status"] == "line down"),
                 "critical": sum(1 for r in items if r["status"] == "critical"),
                 "at_risk": sum(1 for r in items if r["status"] == "at risk"),
                 "shortage_units": round(sum(r["shortage"] for r in items), 1),
                 "open_actions": len(watch.open_notices(db_path, limit=1000))},
        "shortages": items[:25], "actions": actions,
    }


# ---- orders to come -------------------------------------------------------

def planned_orders(db_path, today=None):
    """Every order that is coming: what the buffers say to order now, what waits in the
    15-day queue, and what has been sent and not yet arrived."""
    from . import ddmrp, units

    today = today or date.today()
    now = datetime.combine(today, datetime.min.time()).replace(hour=9)
    rows = []

    buffers, _chain, _span = ddmrp.buffers(db_path)
    for row in buffers:
        if row["to_order"] <= 0 or row["zone"] not in ("red", "yellow"):
            continue
        supplier = row["supplier"] or outbox.supplier_of(db_path, row["article"])
        packed = units.to_packs(db_path, row["article"], supplier, row["to_order"])
        calendar = workdays.Calendar(db_path, supplier)
        rows.append({"article": row["article"], "quantity": float(row["to_order"]),
                     "packs": packed["packs"] if packed else None,
                     "pack_label": packed["pack_label"] if packed else None,
                     "supplier": supplier, "source": f"buffer, {row['zone']} zone",
                     "order_on": calendar.next_working(today).isoformat(),
                     "expected_on": calendar.arrival(now, row["lead_days"]).isoformat(),
                     "status": "to place"})

    for article, quantity, due, supplier_hint in db.read(db_path, """
            SELECT article, sum(quantity), min(due_on), NULL FROM order_queue
            WHERE state = 'waiting' GROUP BY article ORDER BY min(due_on)"""):
        supplier = outbox.supplier_of(db_path, article)
        calendar = workdays.Calendar(db_path, supplier)
        lead = leadtimes.for_supplier(db_path, supplier, default=7) if supplier else 7
        start = datetime.combine(max(due, today), datetime.min.time()).replace(hour=9)
        rows.append({"article": article, "quantity": float(quantity), "packs": None,
                     "pack_label": None, "supplier": supplier, "source": "ticked to order, 15-day wait",
                     "order_on": due.isoformat(), "expected_on": calendar.arrival(start, lead).isoformat(),
                     "status": "waiting"})

    for thread in outbox.threads(db_path, today):
        if thread["kind"] != "order":
            continue
        try:
            _site, _work, groups, _left = outbox._order_groups(db_path, thread["ref_id"])
        except ValueError:
            continue
        for article, _d, _r, quantity in groups.get(thread["supplier"], []):
            rows.append({"article": article, "quantity": float(quantity), "packs": None,
                         "pack_label": None, "supplier": thread["supplier"],
                         "source": f"order #{thread['ref_id']}",
                         "order_on": (thread["sent_at"] or "")[:10],
                         "expected_on": thread["expected_on"],
                         "status": "confirmed" if thread["state"] == "confirmed" else "sent"})
    rows.sort(key=lambda r: (r["expected_on"], r["article"]))
    return {"rows": rows, "counts": {s: sum(1 for r in rows if r["status"] == s)
                                     for s in ("to place", "waiting", "sent", "confirmed")}}
