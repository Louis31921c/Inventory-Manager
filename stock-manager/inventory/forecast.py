import math
from datetime import date, timedelta

from . import db

SERVICE_Z = {80: 0.84, 85: 1.04, 90: 1.28, 95: 1.65, 98: 2.05, 99: 2.33}

DEFAULT_LEAD = 7
DEFAULT_SERVICE = 95
DEFAULT_HORIZON = 14
MIN_DAYS = 7


def _service_z(service):
    service = int(service)
    if service in SERVICE_Z:
        return SERVICE_Z[service]
    closest = min(SERVICE_Z, key=lambda level: abs(level - service))
    return SERVICE_Z[closest]


def _window(db_path):
    rows = db.read(db_path, "SELECT min(delivery_date), max(delivery_date) FROM inventory_lines")
    first, last = rows[0] if rows else (None, None)
    if not first or not last:
        today = date.today()
        return today, today, MIN_DAYS
    return first, last, max((last - first).days + 1, MIN_DAYS)


def _usage(db_path):
    rows = db.read(db_path, """
            SELECT article,
                   delivery_date,
                   sum(quantity)               AS quantity,
                   count(*)                    AS lines,
                   count(*) FILTER (backorder) AS backorders
            FROM inventory_lines
            WHERE article IS NOT NULL AND delivery_date IS NOT NULL
            GROUP BY article, delivery_date
            ORDER BY article, delivery_date
            """)
    by_article = {}
    for article, day, quantity, lines, backorders in rows:
        entry = by_article.setdefault(article, {"days": [], "used": 0.0, "lines": 0,
                                                "backorders": 0, "first": day, "last": day})
        entry["days"].append((day, float(quantity or 0)))
        entry["used"] += float(quantity or 0)
        entry["lines"] += lines
        entry["backorders"] += backorders
        entry["first"] = min(entry["first"], day)
        entry["last"] = max(entry["last"], day)
    return by_article


def _on_hand(db_path):
    """A counted shelf beats a number noted on a purchase order."""
    noted = db.read(db_path, """
            SELECT line.article,
                   arg_max(line.stock, sheet.list_date) AS counted,
                   max(sheet.list_date)                 AS counted_on
            FROM purchase_order_lines line
            JOIN purchase_orders sheet USING (list_id)
            WHERE line.stock IS NOT NULL
            GROUP BY line.article
            """)
    on_hand = {article: (float(counted), counted_on) for article, counted, counted_on in noted}
    for article, quantity, counted_on, _ in db.read(db_path, """
            SELECT article, quantity, counted_on, counted_by FROM stock_counts
            """):
        on_hand[article] = (float(quantity), counted_on)
    return on_hand


def _on_order(db_path):
    rows = db.read(db_path, """
            SELECT article, sum(quantity) AS quantity
            FROM purchase_order_lines
            WHERE in_stock IS NOT TRUE AND quantity IS NOT NULL
            GROUP BY article
            """)
    return {article: float(quantity or 0) for article, quantity in rows}


def _suppliers(db_path):
    rows = db.read(db_path, """
            SELECT article,
                   arg_max(supplier, delivery_date)    AS supplier,
                   arg_max(designation, delivery_date) AS designation
            FROM inventory_lines
            WHERE article IS NOT NULL
            GROUP BY article
            """)
    return {article: (supplier, designation) for article, supplier, designation in rows}


def _references(db_path):
    rows = db.read(db_path, """
            SELECT article, arg_max(reference, seen) AS reference
            FROM catalog
            WHERE article IS NOT NULL AND reference IS NOT NULL
            GROUP BY article
            """)
    return {article: reference for article, reference in rows}


def _weekly(days, first, last):
    """Quantity per calendar week over the whole window, zeros included."""
    start = first - timedelta(days=first.weekday())
    buckets = {}
    week = start
    while week <= last:
        buckets[week] = 0.0
        week += timedelta(days=7)
    for day, quantity in days:
        week = day - timedelta(days=day.weekday())
        buckets[week] = buckets.get(week, 0.0) + quantity
    return [buckets[week] for week in sorted(buckets)]


def _spread(values, mean):
    if len(values) < 2:
        return 0.0
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _risk(cover_days, horizon, backorders, daily):
    if daily <= 0:
        return 0
    if cover_days is None:
        return 60 if backorders else 50
    reach = max(horizon, 1)
    risk = 100 * (1 - min(cover_days / reach, 1))
    if backorders:
        risk = min(risk + 15, 100)
    return int(round(risk))


def articles(db_path, lead=DEFAULT_LEAD, service=DEFAULT_SERVICE, horizon=DEFAULT_HORIZON):
    first, last, days = _window(db_path)
    usage = _usage(db_path)
    counted = _on_hand(db_path)
    ordered = _on_order(db_path)
    suppliers = _suppliers(db_path)
    references = _references(db_path)
    z = _service_z(service)

    out = []
    for article, entry in usage.items():
        weeks = _weekly(entry["days"], first, last)
        daily = entry["used"] / days
        weekly_mean = sum(weeks) / len(weeks) if weeks else 0.0
        weekly_spread = _spread(weeks, weekly_mean)
        daily_spread = weekly_spread / 7
        lead_demand = daily * lead
        safety = z * daily_spread * math.sqrt(lead)
        reorder_point = lead_demand + safety

        on_hand, counted_on = counted.get(article, (None, None))
        known = on_hand is not None
        have = on_hand if known else 0.0
        open_order = ordered.get(article, 0.0)
        cover_days = (have / daily) if daily > 0 and known else None
        want = reorder_point + daily * horizon - have - open_order
        supplier, designation = suppliers.get(article, (None, None))

        if not known:
            state = "uncounted"
        elif have < lead_demand:
            state = "short"
        elif have < reorder_point:
            state = "watch"
        else:
            state = "ok"

        out.append({
            "article": article,
            "designation": designation,
            "reference": references.get(article),
            "supplier": supplier,
            "used": round(entry["used"], 2),
            "lines": entry["lines"],
            "backorders": entry["backorders"],
            "first_seen": entry["first"].isoformat(),
            "last_seen": entry["last"].isoformat(),
            "weekly": [round(value, 2) for value in weeks],
            "per_day": round(daily, 3),
            "per_week": round(daily * 7, 2),
            "spread_per_week": round(weekly_spread, 2),
            "lead_demand": round(lead_demand, 2),
            "safety_stock": round(safety, 2),
            "reorder_point": round(reorder_point, 2),
            "on_hand": round(have, 2) if known else None,
            "counted_on": counted_on.isoformat() if counted_on else None,
            "on_order": round(open_order, 2),
            "cover_days": round(cover_days, 1) if cover_days is not None else None,
            "suggested": max(0, int(math.ceil(want))) if daily > 0 else 0,
            "state": state,
            "risk": _risk(cover_days, horizon, entry["backorders"], daily),
        })

    out.sort(key=lambda row: (-row["risk"], -row["used"], row["article"]))
    return out, (first, last, days)


def weeks_series(db_path):
    rows = db.read(db_path, """
            SELECT date_trunc('week', delivery_date)::DATE AS week,
                   sum(quantity)                      AS quantity,
                   count(*)                           AS lines,
                   count(DISTINCT article)            AS articles
            FROM inventory_lines
            WHERE delivery_date IS NOT NULL
            GROUP BY week
            ORDER BY week
            """)
    return [{"week": week.isoformat(), "quantity": round(float(quantity or 0), 2),
             "lines": lines, "articles": articles_count}
            for week, quantity, lines, articles_count in rows]


def sites_series(db_path, limit=8):
    rows = db.read(db_path, """
            SELECT coalesce(site, 'no site') AS site,
                   count(*)                  AS lines,
                   sum(quantity)             AS quantity
            FROM inventory_lines
            GROUP BY site
            ORDER BY lines DESC
            LIMIT ?
            """, (limit,))
    return [{"site": site, "lines": lines, "quantity": round(float(quantity or 0), 2)}
            for site, lines, quantity in rows]


def summary(db_path, lead=DEFAULT_LEAD, service=DEFAULT_SERVICE, horizon=DEFAULT_HORIZON):
    rows, (first, last, days) = articles(db_path, lead, service, horizon)
    counted = [row for row in rows if row["state"] != "uncounted"]
    short = [row for row in rows if row["state"] == "short"]
    watch = [row for row in rows if row["state"] == "watch"]
    to_order = [row for row in rows if row["suggested"] > 0 and row["state"] != "ok"]
    return {
        "settings": {"lead_days": lead, "service_level": service, "horizon_days": horizon},
        "window": {"first": first.isoformat(), "last": last.isoformat(), "days": days,
                   "weeks": round(days / 7, 1)},
        "counts": {"articles": len(rows), "counted": len(counted), "short": len(short),
                   "watch": len(watch), "to_order": len(to_order)},
        "weeks": weeks_series(db_path),
        "sites": sites_series(db_path),
        "articles": rows,
        "shortlist": to_order[:40],
        "thin": days < 56,
    }
