"""Stock control: where the stock stands site by site, what is spoken for, and how it is kept honest.

stock.py counts the shelf from the paperwork. This adds what the paperwork cannot say:

  transfers     goods moved from one site to another, so each site's pile is its own
  adjustments   a correction with a reason: breakage, a find, a miscount
  reservations  stock held back for a work item, which an order for that work item then uses up
  cycle counts  which articles to count next, the busy ones more often than the dead ones

Everything is a row in stock_moves or reservations, so every change has a day, a person and
a reason, and none of them rewrites the delivery notes.
"""

from datetime import date, timedelta

from . import db, feedback, stock
from .models import normalize_name, parse_date

# How often an article is counted: the ones that move most are checked most.
INTERVALS = {"A": 30, "B": 60, "C": 90}
# A is the articles that make up this share of what left in the last quarter, B the next.
A_SHARE, B_SHARE = 0.80, 0.95
WINDOW_DAYS = 90


def _site(value):
    return normalize_name(value) or ""


def _positive(quantity, what="quantity"):
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        raise ValueError(f"The {what} is not a number.") from None
    if quantity <= 0:
        raise ValueError(f"The {what} must be more than zero.")
    return quantity


def reservations(db_path, today=None):
    """Active reservations with what is left of each: an order for the same article at the
    same site and work item, written since, takes from it."""
    rows = db.read(db_path, f"""
            SELECT r.reservation_id, r.article, coalesce(r.site, ''), coalesce(r.work_item, ''),
                   r.quantity, r.needed_by, r.note, r.created_on, r.created_by,
                   coalesce((SELECT sum(l.quantity)
                             FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
                             WHERE l.article = r.article
                               AND coalesce(h.site, '') = coalesce(r.site, '')
                               AND coalesce(h.work_item, '') = coalesce(r.work_item, '')
                               AND {stock.ORDER_DAY} >= r.created_on), 0)
            FROM reservations r WHERE r.state = 'active'
            ORDER BY r.needed_by NULLS LAST, r.reservation_id""")
    out = []
    for rid, article, site, work_item, quantity, needed, note, created, who, taken in rows:
        left = max(0.0, float(quantity) - float(taken or 0))
        out.append({"reservation_id": rid, "article": article, "site": site,
                    "work_item": work_item, "quantity": float(quantity),
                    "taken": float(taken or 0), "left": left,
                    "needed_by": needed.isoformat() if needed else None,
                    "note": note, "created_on": created.isoformat(), "created_by": who,
                    "state": "used up" if left <= 0 else "held"})
    return out


def site_stock(db_path):
    """One line per article and site: on hand, held back, and free to promise."""
    came = db.read(db_path, """
            SELECT article, coalesce(site, ''), sum(quantity) FROM inventory_lines
            WHERE quantity IS NOT NULL GROUP BY article, coalesce(site, '')""")
    went = db.read(db_path, f"""
            SELECT l.article, coalesce(h.site, ''), sum(l.quantity)
            FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
            WHERE l.quantity IS NOT NULL
              AND ({stock.ORDER_DAY} IS NULL
                   OR {stock.ORDER_DAY} <= current_date - {stock.SHIPPING_DAYS})
            GROUP BY l.article, coalesce(h.site, '')""")
    moved = db.read(db_path, """
            SELECT article, coalesce(site, ''), sum(quantity) FROM stock_moves
            WHERE kind IN ('adjust', 'transfer_in', 'transfer_out')
            GROUP BY article, coalesce(site, '')""")
    held = {}
    for r in reservations(db_path):
        held[(r["article"], r["site"])] = held.get((r["article"], r["site"]), 0.0) + r["left"]

    table = {}
    for rows, sign in ((came, 1), (went, -1), (moved, 1)):
        for article, site, quantity in rows:
            table[(article, site)] = table.get((article, site), 0.0) + sign * float(quantity or 0)
    for key in held:
        table.setdefault(key, 0.0)
    out = [{"article": article, "site": site, "on_hand": round(on_hand, 2),
            "reserved": round(held.get((article, site), 0.0), 2),
            "available": round(on_hand - held.get((article, site), 0.0), 2)}
           for (article, site), on_hand in table.items()]
    out.sort(key=lambda row: (row["article"], row["site"]))
    return out


def _available(db_path, article, site):
    for row in site_stock(db_path):
        if row["article"] == article and row["site"] == site:
            return row["available"], row["on_hand"]
    return 0.0, 0.0


def _move(db_path, article, site, quantity, kind, reason, ref, user):
    db.write(db_path, """
            INSERT INTO stock_moves (article, site, quantity, kind, reason, ref, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING move_id""",
             (article, site or None, quantity, kind, reason, ref, user))


def transfer(db_path, article, from_site, to_site, quantity, user=None, note=None, force=False):
    """Move goods from one site to another. Refused when the first site does not hold them."""
    article, source, target = normalize_name(article), _site(from_site), _site(to_site)
    if not article:
        raise ValueError("No article named.")
    if source == target:
        raise ValueError("A transfer needs two different sites.")
    quantity = _positive(quantity)
    free, _on_hand = _available(db_path, article, source)
    if quantity > free + 1e-9 and not force:
        raise ValueError(f"{source or 'That site'} has {free:g} of {article} free, "
                         f"not {quantity:g}.")
    ref = f"{source or '-'} > {target or '-'}"
    _move(db_path, article, source, -quantity, "transfer_out", note, ref, user)
    _move(db_path, article, target, quantity, "transfer_in", note, ref, user)
    return {"article": article, "from": source, "to": target, "quantity": quantity}


def adjust(db_path, article, site, quantity, reason, user=None):
    """Correct the stock of one article at one site by a signed amount, with a reason."""
    article = normalize_name(article)
    if not article:
        raise ValueError("No article named.")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("An adjustment needs a reason.")
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        raise ValueError("The quantity is not a number.") from None
    if quantity == 0:
        raise ValueError("An adjustment of nothing changes nothing.")
    _move(db_path, article, _site(site), quantity, "adjust", reason, None, user)
    return {"article": article, "site": _site(site), "quantity": quantity}


def reserve(db_path, article, site, work_item, quantity, needed_by=None, user=None, note=None,
            force=False):
    """Hold stock back for a work item. Refused when less than that is free at the site."""
    article, site = normalize_name(article), _site(site)
    if not article:
        raise ValueError("No article named.")
    quantity = _positive(quantity)
    free, _on_hand = _available(db_path, article, site)
    if quantity > free + 1e-9 and not force:
        raise ValueError(f"Only {free:g} of {article} is free at {site or 'that site'}; "
                         f"{quantity:g} cannot be held back.")
    row = db.write(db_path, """
            INSERT INTO reservations (article, site, work_item, quantity, needed_by, note,
                                      created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING reservation_id""",
                   (article, site or None, normalize_name(work_item) or None, quantity,
                    parse_date(needed_by), note or None, user))
    return {"reservation_id": row[0], "article": article, "site": site, "quantity": quantity}


def release(db_path, reservation_id):
    row = db.write(db_path, "UPDATE reservations SET state = 'released' "
                            "WHERE reservation_id = ? AND state = 'active' "
                            "RETURNING reservation_id", (reservation_id,))
    return row is not None


def moves(db_path, limit=60):
    rows = db.read(db_path, """
            SELECT move_id, article, coalesce(site, ''), quantity, kind, reason, ref, moved_on,
                   created_by
            FROM stock_moves ORDER BY move_id DESC LIMIT ?""", (limit,))
    return [{"move_id": mid, "article": article, "site": site, "quantity": quantity,
             "kind": kind, "reason": reason, "ref": ref, "moved_on": day.isoformat(),
             "by": who} for mid, article, site, quantity, kind, reason, ref, day, who in rows]


def cycle_due(db_path, today=None, limit=10):
    """The articles to count next. The busiest quarter is counted monthly, the next every two
    months, the rest every three; the one furthest past its day comes first."""
    today = today or date.today()
    since = today - timedelta(days=WINDOW_DAYS)
    volume = {article: float(total or 0) for article, total in db.read(db_path, f"""
            SELECT l.article, sum(l.quantity)
            FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
            WHERE l.quantity IS NOT NULL AND {stock.ORDER_DAY} >= ?
            GROUP BY l.article""", (since,))}
    articles = {row["article"] for row in stock.rows(db_path)}
    ranked = sorted(articles, key=lambda a: (-volume.get(a, 0.0), a))
    total = sum(volume.values()) or 1.0
    kind, running = {}, 0.0
    for article in ranked:
        share = running / total
        kind[article] = "A" if share < A_SHARE else ("B" if share < B_SHARE else "C")
        running += volume.get(article, 0.0)
    last = {row["article"]: parse_date(row["counted_on"]) for row in db.stock_counts(db_path)}

    due = []
    for article in ranked:
        counted = last.get(article)
        every = INTERVALS[kind[article]]
        age = (today - counted).days if counted else None
        ratio = float("inf") if age is None else age / every
        if ratio >= 1:
            due.append({"article": article, "class": kind[article], "every_days": every,
                        "last_counted": counted.isoformat() if counted else None,
                        "days_since": age,
                        "late_by": None if age is None else age - every,
                        "_ratio": min(ratio, 99.0), "_volume": volume.get(article, 0.0)})
    # Furthest past its day first; never-counted articles tie, and the busier goes first.
    due.sort(key=lambda row: (-row["_ratio"], -row["_volume"], row["article"]))
    for row in due:
        del row["_ratio"], row["_volume"]
    return {"due": due[:limit], "total_due": len(due),
            "classes": {k: sum(1 for v in kind.values() if v == k) for k in "ABC"}}


def count(db_path, article, quantity, user=None):
    """A hand count of one article. Says how far the paperwork was from the shelf."""
    article = normalize_name(article)
    if not article:
        raise ValueError("No article named.")
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        raise ValueError("The count is not a number.") from None
    if quantity < 0:
        raise ValueError("A count cannot be below zero.")
    expected = float(stock.on_hand_map(db_path).get(article, 0.0))
    db.save_stock_count(db_path, article, quantity, user)
    variance = round(quantity - expected, 2)
    _move(db_path, article, None, variance, "count", "cycle count", None, user)
    feedback.short_count(db_path, article, quantity, expected, user)
    return {"article": article, "counted": quantity, "expected": round(expected, 2),
            "variance": variance}


def overview(db_path):
    sites = site_stock(db_path)
    held = reservations(db_path)
    return {"sites": sites, "reservations": held, "moves": moves(db_path),
            "cycle": cycle_due(db_path),
            "summary": {"lines": len(sites),
                        "reserved": round(sum(r["left"] for r in held), 2),
                        "negative": sum(1 for r in sites if r["on_hand"] < 0)}}
