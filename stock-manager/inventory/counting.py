"""Counting, with a second count before the books are changed by much.

stockctl.count writes what somebody found on the shelf over what the paperwork says. That is right when the
count is right. A miscount, a count of the wrong article or of a pile somebody had just moved would put a
wrong number into the books, and every order Plutus writes rests on that number. So:

  * a gap inside the tolerance (a few percent, at least one unit) is posted at once
  * a bigger gap is not posted: the article is asked to be counted again, and the cause is looked for
    (a delivery noted twice, an owed line that arrived, a transfer nobody recorded, an order that went out)
  * when the second count agrees with the first it is posted, with the likely cause written beside it
  * when the two disagree, nothing is posted and a person settles it

Every day a few articles are put on the count list (the cycle of stockctl: the busiest monthly) and the list
is pushed to the alert channels. A count is blind: the screen never shows what the books expect.
"""

from datetime import date, datetime, timedelta

from . import charters, db, prices, stock, stockctl
from .models import normalize_name

PER_DAY_DEFAULT = 5
RECOUNT_HOURS = 48


def _tolerance(expected, pct):
    return max(1.0, abs(expected) * float(pct) / 100.0)


def _near(one, other):
    return abs(one - other) <= max(0.5, 0.05 * max(abs(one), abs(other)))


def explain(db_path, article, gap, today=None):
    """Likely causes of a gap (counted minus books), best first. Each is a guess with a confidence."""
    today = today or date.today()
    causes = []
    size = abs(gap)
    if gap < 0:
        for supplier, qty, first, second, one_note, other_note in db.read(db_path, """
                SELECT a.supplier, a.quantity, a.delivery_date, b.delivery_date, a.note_id, b.note_id
                FROM inventory_lines a JOIN inventory_lines b
                  ON a.article = b.article AND a.supplier IS NOT DISTINCT FROM b.supplier
                 AND a.quantity = b.quantity AND a.note_id < b.note_id
                 AND abs(date_diff('day', a.delivery_date, b.delivery_date)) <= 5
                WHERE a.article = ? AND a.quantity > 0 AND NOT coalesce(a.backorder, FALSE)
                  AND a.delivery_date >= ?""", [article, today - timedelta(days=90)]):
            if _near(float(qty), size):
                causes.append({"cause": "a delivery noted twice",
                               "detail": f"{qty:g} from {supplier} on {first} (note #{one_note}) and again on "
                                         f"{second} (note #{other_note})", "confidence": 0.8})
        for list_id, qty, day in db.read(db_path, f"""
                SELECT l.list_id, l.quantity, {stock.ORDER_DAY} FROM purchase_order_lines l
                JOIN purchase_orders h USING (list_id)
                WHERE l.article = ? AND l.in_stock IS NOT FALSE AND upper(coalesce(h.drafter, '')) <> 'PLUTUS'
                  AND {stock.ORDER_DAY} >= ?""", [article, today - timedelta(days=14)]):
            if _near(float(qty), size):
                causes.append({"cause": "an order went out from another place",
                               "detail": f"order #{list_id} of {qty:g} on {day} was counted here", "confidence": 0.4})
    else:
        for note_id, qty, day in db.read(db_path, """
                SELECT note_id, quantity, delivery_date FROM inventory_lines
                WHERE article = ? AND backorder AND settled_on IS NULL""", [article]):
            if _near(float(qty or 0), size):
                causes.append({"cause": "an owed line that arrived",
                               "detail": f"note #{note_id} of {day} still says {qty:g} owed", "confidence": 0.6})
        for row in stockctl.site_stock(db_path):
            if row["article"] == article and row["on_hand"] < 0 and _near(-row["on_hand"], size):
                causes.append({"cause": "a transfer nobody recorded",
                               "detail": f"{row['site'] or 'one site'} shows {row['on_hand']:g}", "confidence": 0.7})
    for move_id, qty, why, day, who in db.read(db_path, """
            SELECT move_id, quantity, reason, moved_on, created_by FROM stock_moves
            WHERE article = ? AND kind = 'adjust' AND moved_on >= ?""", [article, today - timedelta(days=30)]):
        if _near(-float(qty), gap) and float(qty) * gap < 0:
            causes.append({"cause": "an adjustment that should not have been made",
                           "detail": f"{qty:+g} on {day} by {who or '?'}: {why}", "confidence": 0.5})
    causes.sort(key=lambda c: -c["confidence"])
    return causes[:3]


def _words(causes):
    return "; ".join(f"{c['cause']} ({c['detail']})" for c in causes) or "no likely cause found"


def _open_check(db_path, article, now):
    rows = db.read(db_path, """
            SELECT check_id, first_count, expected, explanation FROM count_checks
            WHERE article = ? AND state = 'recount' AND asked_at >= ? ORDER BY check_id DESC LIMIT 1""",
                   [article, now - timedelta(hours=RECOUNT_HOURS)])
    return rows[0] if rows else None


def _close_tasks(db_path, article):
    db.write(db_path, "UPDATE count_tasks SET state = 'done' WHERE article = ? AND state = 'open' RETURNING article",
             [article])


def submit(db_path, article, quantity, user=None, now=None):
    """One hand count. Gives back {state: posted | recount | confirmed | disputed, ...}."""
    now = now or datetime.now()
    article = normalize_name(article)
    if not article:
        raise ValueError("No article named.")
    try:
        counted = float(quantity)
    except (TypeError, ValueError):
        raise ValueError("The count is not a number.") from None
    if counted < 0:
        raise ValueError("A count cannot be below zero.")
    expected = float(stock.on_hand_map(db_path).get(article, 0.0))
    pct = charters.limits(db_path, "plutus")["count_tolerance_pct"]
    tolerance = _tolerance(expected, pct)
    gap = round(counted - expected, 2)
    base = {"article": article, "counted": counted, "tolerance": round(tolerance, 2)}

    pending = _open_check(db_path, article, now)
    if pending:
        check_id, first, was, words = pending
        if abs(counted - float(first)) <= tolerance:
            posted = stockctl.count(db_path, article, counted, user)
            db.write(db_path, """
                    UPDATE count_checks SET state = 'confirmed', second_count = ?, closed_at = now()
                    WHERE check_id = ? RETURNING check_id""", [counted, check_id])
            _close_tasks(db_path, article)
            return {**base, "state": "confirmed", "expected": posted["expected"], "variance": posted["variance"],
                    "causes_said": words}
        db.write(db_path, """
                UPDATE count_checks SET state = 'disputed', second_count = ? WHERE check_id = ? RETURNING check_id""",
                 [counted, check_id])
        return {**base, "state": "disputed", "first_count": float(first),
                "causes_said": "the two counts disagree: a person settles it"}

    if abs(gap) <= tolerance:
        posted = stockctl.count(db_path, article, counted, user)
        _close_tasks(db_path, article)
        return {**base, "state": "posted", "expected": posted["expected"], "variance": posted["variance"]}

    said = _words(explain(db_path, article, gap, now.date()))
    db.write(db_path, """
            INSERT INTO count_checks (article, expected, first_count, counted_by, explanation)
            VALUES (?, ?, ?, ?, ?) RETURNING check_id""", [article, expected, counted, normalize_name(user) or None, said])
    return {**base, "state": "recount", "causes_said": said}


def disputed(db_path):
    return [{"check_id": i, "article": a, "first_count": f, "second_count": s, "explanation": e}
            for i, a, f, s, e in db.read(db_path, """
                SELECT check_id, article, first_count, second_count, explanation FROM count_checks
                WHERE state = 'disputed' ORDER BY check_id""")]


def recounts(db_path, now=None):
    """Articles waiting for their second count (the first number is not shown)."""
    now = now or datetime.now()
    return [{"check_id": i, "article": a, "asked_at": at.isoformat(timespec="minutes")}
            for i, a, at in db.read(db_path, """
                SELECT check_id, article, asked_at FROM count_checks
                WHERE state = 'recount' AND asked_at >= ? ORDER BY check_id""", [now - timedelta(hours=RECOUNT_HOURS)])]


def settle(db_path, check_id, quantity, user=None):
    """A person decides a disputed count: this number goes into the books."""
    rows = db.read(db_path, "SELECT article FROM count_checks WHERE check_id = ? AND state = 'disputed'", [check_id])
    if not rows:
        raise ValueError("That count is not waiting for a person.")
    posted = stockctl.count(db_path, rows[0][0], quantity, user)
    db.write(db_path, "UPDATE count_checks SET state = 'settled', closed_at = now() WHERE check_id = ? RETURNING check_id",
             [check_id])
    _close_tasks(db_path, rows[0][0])
    return posted


def per_day(db_path):
    rows = db.read(db_path, "SELECT value FROM policy_settings WHERE key = 'count_per_day'")
    return int(rows[0][0]) if rows else PER_DAY_DEFAULT


def assign(db_path, today=None):
    """Put today's articles on the count list, once a day. Gives back the articles added."""
    today = today or date.today()
    if db.read(db_path, "SELECT 1 FROM policy_settings WHERE key = 'count_assigned_on' AND value = ?",
               [today.isoformat()]):
        return []
    added = []
    open_now = {a for (a,) in db.read(db_path, "SELECT article FROM count_tasks WHERE state = 'open'")}
    for row in stockctl.cycle_due(db_path, today, limit=per_day(db_path))["due"]:
        if row["article"] in open_now:
            continue
        db.write(db_path, "INSERT INTO count_tasks (article, due_on) VALUES (?, ?) ON CONFLICT DO NOTHING RETURNING article",
                 [row["article"], today])
        added.append(row["article"])
    db.write(db_path, "INSERT INTO policy_settings (key, value) VALUES ('count_assigned_on', ?) "
                      "ON CONFLICT (key) DO UPDATE SET value = excluded.value RETURNING key", [today.isoformat()])
    return added


def tasks(db_path):
    """The count list: articles with where they live. Never what the books expect."""
    from . import receiving

    return [{"article": a, "due_on": d.isoformat(),
             "where": ", ".join(f"{h['location']}" for h in receiving.where(db_path, a)) or None}
            for a, d in db.read(db_path, "SELECT article, due_on FROM count_tasks WHERE state = 'open' "
                                         "ORDER BY due_on, article")]


def overview(db_path, today=None):
    return {"tasks": tasks(db_path), "recounts": recounts(db_path), "disputed": disputed(db_path)}
