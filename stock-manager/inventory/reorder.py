"""What is ticked "to order" waits fifteen days, and then Plutus orders it.

A line on a stored purchase order that somebody has ticked "to order" is read into a
queue of its own, with the day it was read and the day it falls due. Nothing is ordered
the day it is read: a few of those lines turn out to be on a shelf somewhere, and a
fortnight is time enough for somebody to say so. Tick the line back to "in stock" and
it leaves the queue. Once the day has come, the lines due are written down as one
purchase order a site and work item, drafted by Plutus, which is as far as Plutus goes:
the order is a list, and what is bought from it is still decided at the gate.
"""

from datetime import date, timedelta

import duckdb

from . import audit, db

DELAY_DAYS = 15
DRAFTER = "Plutus"


def _day(today):
    return today or date.today()


def collect(db_path, today=None):
    """Read every line ticked "to order" into the queue, once each, and let go of the
    ones that are no longer ticked."""
    today = _day(today)
    due = today + timedelta(days=DELAY_DAYS)
    with duckdb.connect(str(db_path)) as con:
        con.begin()
        try:
            dropped = con.execute("""
                UPDATE order_queue SET state = 'dropped'
                WHERE state = 'waiting' AND NOT EXISTS (
                    SELECT 1 FROM purchase_order_lines l
                    WHERE l.list_id = order_queue.list_id AND l.line_no = order_queue.line_no
                      AND l.in_stock = FALSE)
                RETURNING queue_id""").fetchall()
            added = con.execute("""
                INSERT INTO order_queue (list_id, line_no, article, designation, reference,
                                         quantity, site, work_item, queued_on, due_on)
                SELECT l.list_id, l.line_no, l.article, l.designation, l.reference,
                       l.quantity, h.site, h.work_item, ?, ?
                FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
                WHERE l.in_stock = FALSE AND NOT EXISTS (
                    SELECT 1 FROM order_queue q
                    WHERE q.list_id = l.list_id AND q.line_no = l.line_no)
                RETURNING queue_id""", [today, due]).fetchall()
            con.commit()
        except Exception:
            con.rollback()
            raise
    return {"added": len(added), "dropped": len(dropped)}


def place_due(db_path, today=None):
    """Order what has waited long enough: one purchase order a site and work item."""
    today = _day(today)
    rows = db.read(db_path, """
            SELECT queue_id, article, designation, reference, quantity, site, work_item
            FROM order_queue WHERE state = 'waiting' AND due_on <= ?
            ORDER BY site, work_item, queue_id""", [today])
    groups = {}
    for queue_id, article, designation, reference, quantity, site, work_item in rows:
        groups.setdefault((site, work_item), []).append(
            (queue_id, article, designation, reference, quantity))

    placed = []
    for (site, work_item), lines in groups.items():
        # The same article from two lists is one line, so the supplier is asked once.
        merged = {}
        for _queue_id, article, designation, reference, quantity in lines:
            line = merged.setdefault(article, {"article": article, "designation": designation,
                                               "reference": reference, "quantity": 0.0})
            line["quantity"] += float(quantity or 0)
        list_id = db.save_purchase_order(db_path, {
            "list_date": today, "site": site, "work_item": work_item, "drafter": DRAFTER,
            "lines": list(merged.values())}, None, None, DRAFTER.upper())
        for queue_id, *_rest in lines:
            db.write(db_path, "UPDATE order_queue SET state = 'ordered', ordered_on = ?, "
                              "ordered_list = ? WHERE queue_id = ? RETURNING queue_id",
                     [today, list_id, queue_id])
        audit.record(db_path, DRAFTER.upper(), "order.queue", f"order #{list_id}",
                     f"{len(merged)} line(s) that waited {DELAY_DAYS} days")
        placed.append({"list_id": list_id, "site": site, "work_item": work_item,
                       "lines": len(merged)})
    return {"orders": placed, "lines": sum(len(lines) for lines in groups.values())}


def run(db_path, today=None):
    """What the server does by itself every hour: read, then order what is due."""
    return {**collect(db_path, today), **place_due(db_path, today)}


def listing(db_path, today=None, limit=200):
    """What the screen shows: what waits first, soonest due on top, then what was ordered."""
    today = _day(today)
    rows = db.read(db_path, """
            SELECT queue_id, list_id, line_no, article, designation, quantity, site, work_item,
                   queued_on, due_on, state, ordered_on, ordered_list
            FROM order_queue WHERE state IN ('waiting', 'ordered')
            ORDER BY (state = 'waiting') DESC,
                     CASE WHEN state = 'waiting' THEN due_on END ASC,
                     ordered_on DESC, queue_id DESC
            LIMIT ?""", [limit])
    names = ("queue_id", "list_id", "line_no", "article", "designation", "quantity", "site",
             "work_item", "queued_on", "due_on", "state", "ordered_on", "ordered_list")
    out = []
    for row in rows:
        item = dict(zip(names, row))
        item["days_left"] = ((item["due_on"] - today).days
                             if item["state"] == "waiting" and item["due_on"] else None)
        for key in ("queued_on", "due_on", "ordered_on"):
            item[key] = item[key].isoformat() if item[key] else None
        out.append(item)
    waiting = [row for row in out if row["state"] == "waiting"]
    return {"delay_days": DELAY_DAYS, "rows": out,
            "counts": {"waiting": len(waiting),
                       "due": sum(1 for row in waiting if row["days_left"] <= 0),
                       "ordered": len(out) - len(waiting)}}
