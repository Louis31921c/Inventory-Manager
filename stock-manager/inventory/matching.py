"""Three-way match: what was ordered, what came, and what the bill says.

The delivery notes are the middle of the three. A bill is checked against the deliveries it
must be paying for: every delivery of that supplier since its previous bill, priced at what
that article cost on the day (the prices the app has seen on baskets, orders and bills).
The orders lines ticked "to order", and the ones Plutus drafted, are checked against what
has come in at the same site and work item since they were written.

Nothing here changes anything. It says what does not add up, and the Bills screen lets a
person mark a bill disputed.
"""

from datetime import date, timedelta

from . import db

# A bill is "matched" when it is within this share of what the deliveries come to, or this
# many euros, whichever is wider: a few cents of rounding is not a finding.
TOLERANCE = 0.02
SLACK = 1.0

# An order with nothing at all delivered after this many days is late, and a delivery not
# billed after this many is worth a call to the supplier's accounts.
LATE_ORDER_DAYS = 21
UNBILLED_DAYS = 45


def _prices(db_path):
    """article -> [(day, supplier, price)], oldest first."""
    found = {}
    for article, supplier, price, day in db.read(db_path, """
            SELECT article, supplier, unit_price, seen_on FROM article_prices
            ORDER BY seen_on, price_id"""):
        found.setdefault(article, []).append((day, supplier, float(price)))
    return found


def price_on(prices, article, supplier, day):
    """What `article` cost `supplier` on `day`: the last price seen by then from that
    supplier, else the last from anyone, else the earliest ever seen."""
    seen = prices.get(article) or []
    if not seen:
        return None
    before = [row for row in seen if day is None or row[0] <= day] or seen[:1]
    same = [row for row in before if row[1] == supplier]
    return (same or before)[-1][2]


def _deliveries(db_path):
    return db.read(db_path, """
            SELECT supplier, delivery_date, article, quantity, site, work_item, note_id
            FROM inventory_lines
            WHERE quantity IS NOT NULL AND NOT coalesce(backorder, FALSE)
              AND delivery_date IS NOT NULL
            ORDER BY supplier, delivery_date, note_id""")


def bills(db_path, today=None):
    """Every bill against the deliveries since that supplier's previous bill, plus the
    deliveries nobody has billed yet."""
    today = today or date.today()
    prices = _prices(db_path)
    by_supplier = {}
    for supplier, day, article, quantity, site, work_item, note_id in _deliveries(db_path):
        by_supplier.setdefault(supplier, []).append((day, article, float(quantity), note_id))

    heads = db.read(db_path, """
            SELECT bill_id, supplier, number, bill_date, net, total, order_ref, state
            FROM bills WHERE bill_date IS NOT NULL ORDER BY supplier, bill_date, bill_id""")
    orders = {row[0] for row in db.read(db_path, "SELECT list_id FROM purchase_orders")}

    out = []
    covered_until = {}
    for bill_id, supplier, number, bill_date, net, total, order_ref, state in heads:
        since = covered_until.get(supplier)
        mine = [row for row in by_supplier.get(supplier, [])
                if row[0] <= bill_date and (since is None or row[0] > since)]
        covered_until[supplier] = bill_date

        expected, unpriced = 0.0, 0
        for day, article, quantity, _note_id in mine:
            price = price_on(prices, article, supplier, day)
            if price is None:
                unpriced += 1
            else:
                expected += quantity * price
        billed = net if net is not None else total
        gap = None if billed is None else round(billed - expected, 2)
        if not mine:
            verdict = "nothing delivered"
        elif billed is None:
            verdict = "no amount"
        elif unpriced and abs(gap) > max(SLACK, TOLERANCE * expected):
            verdict = "unchecked"
        elif abs(gap) <= max(SLACK, TOLERANCE * expected):
            verdict = "matched"
        else:
            verdict = "over" if gap > 0 else "under"

        ref = str(order_ref or "").strip().lstrip("#")
        linked = int(ref) if ref.isdigit() and int(ref) in orders else None
        out.append({
            "bill_id": bill_id, "supplier": supplier, "number": number,
            "bill_date": bill_date.isoformat(), "billed": billed,
            "expected": round(expected, 2), "gap": gap, "deliveries": len(mine),
            "unpriced": unpriced, "verdict": verdict, "state": state,
            "order_ref": order_ref, "order_found": linked,
            "order_unknown": bool(ref) and linked is None,
        })

    unbilled = []
    for supplier, rows in by_supplier.items():
        left = [row for row in rows if covered_until.get(supplier) is None
                or row[0] > covered_until[supplier]]
        if not left:
            continue
        worth = sum(quantity * (price_on(prices, article, supplier, day) or 0.0)
                    for day, article, quantity, _n in left)
        oldest = min(row[0] for row in left)
        age = (today - oldest).days
        if age >= UNBILLED_DAYS:
            unbilled.append({"supplier": supplier, "deliveries": len({row[3] for row in left}),
                             "since": oldest.isoformat(), "days": age,
                             "worth": round(worth, 2)})
    unbilled.sort(key=lambda row: -row["days"])

    out.sort(key=lambda row: row["bill_date"], reverse=True)
    counts = {}
    for row in out:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    return {"bills": out, "unbilled": unbilled, "counts": counts}


def orders(db_path, today=None, days=120):
    """What was ordered from suppliers against what has come in since."""
    today = today or date.today()
    since = today - timedelta(days=days)
    lines = db.read(db_path, """
            SELECT h.list_id, coalesce(h.list_date, CAST(h.created_at AS DATE)), h.site,
                   h.work_item, l.line_no, l.article, l.quantity
            FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
            WHERE l.quantity IS NOT NULL
              AND (l.in_stock = FALSE OR upper(coalesce(h.drafter, '')) = 'PLUTUS')
              AND coalesce(h.list_date, CAST(h.created_at AS DATE)) >= ?
            ORDER BY h.list_id, l.line_no""", [since])
    came = {}
    for supplier, day, article, quantity, site, work_item, _note_id in _deliveries(db_path):
        came.setdefault((site, work_item, article), []).append((day, float(quantity)))

    by_order = {}
    for list_id, day, site, work_item, line_no, article, quantity in lines:
        arrived = sum(q for d, q in came.get((site, work_item, article), []) if d >= day)
        asked = float(quantity)
        if arrived <= 0:
            state = "not delivered"
        elif arrived + 1e-9 < asked:
            state = "part delivered"
        elif arrived > asked + 1e-9:
            state = "over delivered"
        else:
            state = "complete"
        held = by_order.setdefault(list_id, {
            "list_id": list_id, "list_date": day.isoformat(), "site": site,
            "work_item": work_item, "age_days": (today - day).days, "lines": []})
        held["lines"].append({"article": article, "ordered": asked, "delivered": arrived,
                              "state": state})

    out = []
    for held in by_order.values():
        states = {line["state"] for line in held["lines"]}
        if states == {"complete"}:
            held["state"] = "complete"
        elif states == {"not delivered"}:
            held["state"] = ("late" if held["age_days"] > LATE_ORDER_DAYS else "waiting")
        elif "over delivered" in states and not states & {"not delivered", "part delivered"}:
            held["state"] = "over delivered"
        else:
            held["state"] = "part delivered"
        out.append(held)
    out.sort(key=lambda row: (row["state"] == "complete", -row["list_id"]))
    counts = {}
    for row in out:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {"orders": out, "counts": counts}


def report(db_path, today=None):
    return {"bills": bills(db_path, today), "orders": orders(db_path, today)}
