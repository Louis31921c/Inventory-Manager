"""Faults found by statistics, not by a fixed limit.

A rule like "a price 25% above the usual one" misses what is odd for one article and
normal for another. Here every article, supplier and bill is held against its own history,
with robust statistics (the median and the median absolute deviation, which one wild value
cannot drag the way it drags a mean). A finding is only raised when the history is long
enough to say what is normal, and each carries an impact, so the Inbox can put the costly
ones first.

  consumption   an article's last week of use against its own weeks: a spike or a collapse
  lead time     a supplier whose recent deliveries take longer than its own habit
  price         a price far from that article's history, either way (a rise of 25% or more
                is the price notice's business already)
  duplicate     the same bill twice: same number, or same amount on the same day

Nothing here changes anything. watch.py turns each finding into a notice.
"""

from datetime import date, timedelta
from statistics import median

from . import db, stock

# How far from normal, in robust standard deviations, counts as odd.
Z_LIMIT = 3.5
MIN_WEEKS = 8            # weeks of history before a consumption can be called odd
MIN_DELIVERIES = 8       # a supplier's earlier deliveries, and
RECENT_DELIVERIES = 4    # how many of its latest are compared with them
MIN_PRICES = 5
PRICE_UP_ELSEWHERE = 0.25
MIN_PRICE_GAP = 0.10       # EUR: a few cents on a cheap article is rounding, not a fault
HISTORY_WEEKS = 26
SPIKE_OVER_PEAK = 1.5      # a spike is also this much above the biggest earlier week
MIN_USUAL_FOR_COLLAPSE = 5
COLLAPSE_BELOW = 0.25      # and a collapse is under this share of the usual week


def _scale(values, floor):
    """The spread of `values` the robust way, never below `floor`."""
    mid = median(values)
    mad = median(abs(v - mid) for v in values)
    return max(1.4826 * mad, floor)


def consumption(db_path, today=None):
    """An article whose last 7 days of use is far from its usual week."""
    today = today or date.today()
    since = today - timedelta(weeks=HISTORY_WEEKS)
    weeks = {}
    for article, day, quantity in db.read(db_path, f"""
            SELECT l.article, {stock.ORDER_DAY}, sum(l.quantity)
            FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
            WHERE l.quantity IS NOT NULL AND upper(coalesce(h.drafter, '')) <> 'PLUTUS'
              AND {stock.ORDER_DAY} > ? AND {stock.ORDER_DAY} <= ?
            GROUP BY l.article, {stock.ORDER_DAY}""", [since, today]):
        index = (today - day).days // 7
        weeks.setdefault(article, {}).setdefault(index, 0.0)
        weeks[article][index] += float(quantity)
    prices = dict(db.read(db_path, """
            SELECT article, arg_max(unit_price, seen_on) FROM article_prices GROUP BY article"""))
    found = []
    for article, by_week in weeks.items():
        # Week 0 is the one being judged; the rest, zeros included, say what is usual.
        # From the first week the article was used: before that it did not exist for us.
        oldest = max(by_week)
        past = [by_week.get(i, 0.0) for i in range(1, oldest + 1)]
        if sum(1 for i in range(1, oldest + 1) if i in by_week) < MIN_WEEKS // 2:
            continue
        mid = median(past)
        spread = _scale(past, max(0.5, 0.5 * mid ** 0.5))
        now = by_week.get(0, 0.0)
        z = (now - mid) / spread
        if abs(z) < Z_LIMIT or abs(now - mid) < 3:
            continue
        # Lumpy demand (a pallet some weeks, nothing others) is not a fault. A spike has to beat
        # the biggest week seen before as well, and a collapse only counts for an article that
        # really moves every week and has all but stopped.
        if z > 0 and now < SPIKE_OVER_PEAK * max(past):
            continue
        if z < 0 and (mid < MIN_USUAL_FOR_COLLAPSE or now > COLLAPSE_BELOW * mid):
            continue
        price = float(prices.get(article) or 0)
        impact = abs(now - mid) * price
        word = "spiked" if z > 0 else "collapsed"
        found.append({
            "key": f"anomaly-use:{article}", "kind": "anomaly",
            "severity": "high" if z > 0 and impact >= 500 else "normal",
            "title": f"{article}: use {word} this week",
            "detail": f"{now:g} out in 7 days against a usual {mid:g} (z {z:+.1f})"
                      + (f", about {impact:,.0f} EUR off the usual" if impact else ""),
            "impact": impact})
    return found


def lead_times(db_path, today=None):
    """A supplier whose latest deliveries are slower than its own habit."""
    today = today or date.today()
    found = []
    for supplier, in db.read(db_path, """
            SELECT supplier FROM inventory_lines
            WHERE supplier IS NOT NULL AND order_date IS NOT NULL AND delivery_date IS NOT NULL
              AND delivery_date >= order_date AND NOT coalesce(backorder, FALSE)
            GROUP BY supplier HAVING count(*) >= ?""", [MIN_DELIVERIES + RECENT_DELIVERIES]):
        rows = db.read(db_path, """
                SELECT delivery_date - order_date FROM inventory_lines
                WHERE supplier = ? AND order_date IS NOT NULL AND delivery_date IS NOT NULL
                  AND delivery_date >= order_date AND delivery_date <= ?
                  AND NOT coalesce(backorder, FALSE)
                ORDER BY delivery_date DESC, note_id DESC""", [supplier, today])
        days = [float(r[0]) for r in rows]
        recent, base = days[:RECENT_DELIVERIES], days[RECENT_DELIVERIES:]
        if len(base) < MIN_DELIVERIES:
            continue
        slower = median(recent) - median(base)
        spread = _scale(base, 1.0)
        if slower >= max(3.0, Z_LIMIT * spread / (len(recent) ** 0.5)):
            found.append({
                "key": f"anomaly-lead:{supplier}", "kind": "anomaly", "severity": "high",
                "title": f"{supplier} is getting slower",
                "detail": f"its last {len(recent)} deliveries took {median(recent):g} days, "
                          f"it usually takes {median(base):g}",
                "impact": slower})
    return found


def prices(db_path, today=None):
    """A latest price far from the same supplier's own history for that article, falling or rising a little."""
    found = []
    history = {}
    for article, supplier, price in db.read(db_path, """
            SELECT article, coalesce(supplier, ''), unit_price FROM article_prices
            WHERE (article, coalesce(supplier, '')) IN (
                SELECT article, coalesce(supplier, '') FROM article_prices
                GROUP BY article, coalesce(supplier, '') HAVING count(*) >= ?)
            ORDER BY article, supplier, seen_on DESC, price_id DESC""", [MIN_PRICES]):
        history.setdefault((article, supplier), []).append(float(price))
    for (article, supplier), seen in history.items():
        latest, before = seen[0], seen[1:]
        mid = median(before)
        if mid <= 0:
            continue
        change = latest / mid - 1
        spread = _scale(before, 0.02 * mid)
        z = (latest - mid) / spread
        if (abs(z) < Z_LIMIT or abs(change) < 0.10 or change >= PRICE_UP_ELSEWHERE
                or abs(latest - mid) < MIN_PRICE_GAP):
            continue
        found.append({
            "key": f"anomaly-price:{article}:{supplier}", "kind": "anomaly", "severity": "normal",
            "title": f"{article}: price {'fell' if change < 0 else 'rose'} {abs(change):.0%} out of line",
            "detail": (f"{supplier}: " if supplier else "") + f"{latest:.2f} against a usual {mid:.2f}"
                      + (": a typo, or a price to check before the next order" if change < 0 else ""),
            "impact": abs(latest - mid)})
    return found


def duplicate_bills(db_path, today=None):
    """The same bill entered twice."""
    found = []
    seen = {}
    for bill_id, supplier, number, day, total in db.read(db_path, """
            SELECT bill_id, supplier, number, bill_date, coalesce(total, net) FROM bills
            WHERE state <> 'disputed' ORDER BY bill_id"""):
        keys = []
        if number and str(number).strip():
            keys.append(("number", supplier.upper(), str(number).strip().upper()))
        if total:
            keys.append(("amount", supplier.upper(), day, round(float(total), 2)))
        for key in keys:
            if key in seen:
                first = seen[key]
                found.append({
                    "key": f"anomaly-duplicate:{bill_id}", "kind": "bill", "severity": "high",
                    "title": f"{supplier} bill {number or bill_id} looks like a duplicate of #{first}",
                    "detail": ("same number" if key[0] == "number" else "same amount on the same day")
                              + f", {float(total or 0):,.2f} to pay twice",
                    "impact": float(total or 0)})
                break
            seen[key] = bill_id
    return found


DETECTORS = (consumption, lead_times, prices, duplicate_bills)


def find(db_path, today=None):
    """Every finding, the costliest first."""
    out = []
    for detector in DETECTORS:
        out += detector(db_path, today)
    out.sort(key=lambda row: -row["impact"])
    return out
