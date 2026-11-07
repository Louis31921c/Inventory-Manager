"""Goods going out to the sites, with the paper that goes with them.

A stored list of what a site needs becomes a dispatch: a van, a day, and the lines to load.
Loading it and handing it over are two steps, and the second records who signed for it and
what really arrived, line by line. Short delivery marks the dispatch part delivered, which
leaves it open to finish. The paper is the delivery note to print for the driver, with a box
to sign, and one label per line to stick on the pallets.
"""

from datetime import datetime

from . import db
from .models import parse_date

STATES = ("planned", "loaded", "delivered", "part delivered", "failed")


def create_from_order(db_path, list_id, vehicle_id=None, planned_on=None, user=None, note=None):
    head = db.read(db_path, "SELECT site, work_item FROM purchase_orders WHERE list_id = ?", [list_id])
    if not head:
        raise ValueError(f"No order #{list_id} to deliver.")
    lines = db.read(db_path, """
            SELECT article, designation, quantity FROM purchase_order_lines
            WHERE list_id = ? AND quantity IS NOT NULL AND quantity > 0 ORDER BY line_no""",
                    [list_id])
    if not lines:
        raise ValueError(f"Order #{list_id} has no line with a quantity.")
    made = db.write(db_path, """
            INSERT INTO dispatches (list_id, site, work_item, vehicle_id, planned_on, note, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING dispatch_id""",
                    [list_id, head[0][0], head[0][1], vehicle_id, parse_date(planned_on),
                     note, user])
    dispatch_id = made[0]
    for number, (article, designation, quantity) in enumerate(lines, 1):
        db.write(db_path, "INSERT INTO dispatch_lines (dispatch_id, line_no, article, designation, "
                          "quantity) VALUES (?, ?, ?, ?, ?) RETURNING line_no",
                 [dispatch_id, number, article, designation, quantity])
    return one(db_path, dispatch_id)


def _lines(db_path, dispatch_id):
    return [{"line_no": n, "article": a, "designation": d, "quantity": float(q),
             "delivered": None if got is None else float(got)}
            for n, a, d, q, got in db.read(
                db_path, "SELECT line_no, article, designation, quantity, delivered "
                         "FROM dispatch_lines WHERE dispatch_id = ? ORDER BY line_no", [dispatch_id])]


def one(db_path, dispatch_id):
    rows = db.read(db_path, f"{_SELECT} WHERE d.dispatch_id = ?", [dispatch_id])
    if not rows:
        return None
    row = _shape(rows[0])
    row["lines"] = _lines(db_path, dispatch_id)
    return row


_SELECT = """SELECT d.dispatch_id, d.list_id, d.site, d.work_item, d.vehicle_id, v.name,
                    d.planned_on, d.state, d.loaded_at, d.delivered_at, d.received_by, d.note,
                    (SELECT count(*) FROM dispatch_lines l WHERE l.dispatch_id = d.dispatch_id)
             FROM dispatches d LEFT JOIN vehicles v USING (vehicle_id)"""


def _shape(values):
    (dispatch_id, list_id, site, work_item, vehicle_id, vehicle, planned, state, loaded, delivered,
     received_by, note, count) = values
    return {"dispatch_id": dispatch_id, "list_id": list_id, "site": site, "work_item": work_item,
            "vehicle_id": vehicle_id, "vehicle": vehicle,
            "planned_on": planned.isoformat() if planned else None, "state": state,
            "loaded_at": loaded.isoformat(timespec="minutes") if loaded else None,
            "delivered_at": delivered.isoformat(timespec="minutes") if delivered else None,
            "received_by": received_by, "note": note, "lines": count}


def listing(db_path, limit=100):
    rows = db.read(db_path, f"{_SELECT} ORDER BY (d.state IN ('delivered', 'failed')), d.dispatch_id DESC "
                            "LIMIT ?", [limit])
    return [_shape(r) for r in rows]


def load(db_path, dispatch_id):
    done = db.write(db_path, "UPDATE dispatches SET state = 'loaded', loaded_at = now() "
                             "WHERE dispatch_id = ? AND state = 'planned' RETURNING dispatch_id",
                    [dispatch_id])
    if done is None:
        raise ValueError("Only a planned dispatch can be marked loaded.")
    return one(db_path, dispatch_id)


def deliver(db_path, dispatch_id, received_by, delivered=None, note=None, now=None):
    """Hand the goods over. `delivered` maps a line number to what arrived; a line left out
    arrived in full. Who signed for it is required: that is the proof."""
    held = one(db_path, dispatch_id)
    if held is None:
        raise ValueError("No such dispatch.")
    if held["state"] in ("delivered", "failed"):
        raise ValueError("That dispatch is already closed.")
    who = (received_by or "").strip()
    if not who:
        raise ValueError("Say who signed for the goods.")
    delivered = {int(k): float(v) for k, v in (delivered or {}).items()}
    short = False
    for line in held["lines"]:
        got = delivered.get(line["line_no"], line["quantity"])
        if got < 0:
            raise ValueError("A delivered quantity cannot be below zero.")
        short = short or got + 1e-9 < line["quantity"]
        db.write(db_path, "UPDATE dispatch_lines SET delivered = ? WHERE dispatch_id = ? "
                          "AND line_no = ? RETURNING line_no", [got, dispatch_id, line["line_no"]])
    db.write(db_path, """
            UPDATE dispatches SET state = ?, delivered_at = ?, received_by = ?,
                   note = coalesce(?, note)
            WHERE dispatch_id = ? RETURNING dispatch_id""",
             ["part delivered" if short else "delivered", now or datetime.now(), who, note, dispatch_id])
    return one(db_path, dispatch_id)


def fail(db_path, dispatch_id, note):
    done = db.write(db_path, "UPDATE dispatches SET state = 'failed', note = ? WHERE dispatch_id = ? "
                             "AND state NOT IN ('delivered', 'failed') RETURNING dispatch_id",
                    [note, dispatch_id])
    if done is None:
        raise ValueError("That dispatch cannot be marked failed.")
    return one(db_path, dispatch_id)
