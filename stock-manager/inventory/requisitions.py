"""A request from a site, checked against stock first, with a status that comes back.

A person on a site asks for articles for a work item, by the day they are needed. Nobody has to
decide where they come from:

  1. what the site already has free is held back for the work item (a reservation)
  2. what is left goes through the purchase round as a basket: the gate reads it, Terminus
     reads the names, Plutus approves what is inside its limits and sends it from the wallet
  3. when the goods arrive on a delivery note for that site and work item, the line is done

`status` is always worked out from what really happened (the reservation, the gate request, the
delivery notes), never stored, so it cannot drift. `refresh` is the hourly pass: it holds back
stock that has arrived, closes finished requests, and says which ones are late.
"""

import asyncio
from datetime import date

from . import db, gate, outbox, prices, stockctl, verify
from .models import normalize_name, parse_date

WHO = "REQUEST"

LINE_WORDS = {
    "in stock": "held back from the shelf",
    "waiting": "ordering: waiting for approval",
    "approved": "ordering: approved, being placed",
    "ordered": "ordered from the supplier",
    "delivered": "delivered",
    "cancelled": "not ordered (cancelled)",
}


def _lines(db_path, req_id):
    return db.read(db_path, """
            SELECT line_no, article, quantity, reserved, reservation_id, to_order
            FROM requisition_lines WHERE req_id = ? ORDER BY line_no""", [req_id])


def _head(db_path, req_id):
    rows = db.read(db_path, """
            SELECT req_id, requester, site, work_item, needed_by, state, note, gate_request_id, created_at
            FROM requisitions WHERE req_id = ?""", [req_id])
    return rows[0] if rows else None


def submit(db_path, requester, site, work_item, needed_by, items, note=None, check_names=True):
    """Open a request. `items` is a list of {article, quantity}. Gives back its status."""
    site, work_item = normalize_name(site), normalize_name(work_item)
    if not site or not work_item:
        raise ValueError("A request names the site and the work item.")
    lines = []
    for item in items or []:
        article = normalize_name(item.get("article"))
        try:
            quantity = float(item.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if article and quantity > 0:
            lines.append((article, quantity))
    if not lines:
        raise ValueError("A request needs at least one article with a quantity.")

    day = parse_date(needed_by)
    req_id = db.write(db_path, """
            INSERT INTO requisitions (requester, site, work_item, needed_by, note)
            VALUES (?, ?, ?, ?, ?) RETURNING req_id""",
                      [normalize_name(requester) or None, site, work_item, day, note or None])[0]

    basket = []
    for number, (article, quantity) in enumerate(lines, 1):
        free, _on_hand = stockctl._available(db_path, article, site)
        held = min(max(free, 0.0), quantity)
        reservation_id = None
        if held > 0:
            reservation_id = stockctl.reserve(db_path, article, site, work_item, held,
                                              day.isoformat() if day else None, WHO,
                                              f"request #{req_id}")["reservation_id"]
        to_order = round(quantity - held, 6)
        db.write(db_path, """
                INSERT INTO requisition_lines (req_id, line_no, article, quantity, reserved, reservation_id, to_order)
                VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING line_no""",
                 [req_id, number, article, quantity, held, reservation_id, to_order])
        if to_order > 0:
            known = prices.sightings(db_path, article, 1)
            basket.append({"article": article, "quantity": to_order,
                           "supplier": outbox.supplier_of(db_path, article),
                           "unit_price": known[0] if known else None})

    if basket:
        reading = gate.look(db_path, basket)
        request_id = gate.ask(db_path, reading, channel=f"req:{req_id}", shop=f"REQUEST #{req_id}",
                              asked_by=requester or WHO, site=site, work_item=work_item)
        db.write(db_path, "UPDATE requisitions SET gate_request_id = ? WHERE req_id = ? RETURNING req_id",
                 [request_id, req_id])
        if check_names:
            try:
                answer = asyncio.run(verify.check(db_path, reading["lines"]))
                gate.record_check(db_path, request_id, answer["verdict"], answer["note"], answer["lines"])
            except Exception:
                pass                                     # unread: it waits for Terminus or a person
    else:
        db.write(db_path, "UPDATE requisitions SET state = 'done', closed_at = now() WHERE req_id = ? RETURNING req_id",
                 [req_id])
    return status(db_path, req_id)


def _delivered(db_path, article, site, work_item, since):
    return float(db.read(db_path, """
            SELECT coalesce(sum(quantity), 0) FROM inventory_lines
            WHERE article = ? AND coalesce(site, '') = ? AND coalesce(work_item, '') = ?
              AND NOT coalesce(backorder, FALSE) AND delivery_date >= ?""",
                         [article, site or "", work_item or "", since])[0][0])


def _gate_state(db_path, request_id):
    if not request_id:
        return None
    request = gate.one(db_path, request_id)
    return request["state"] if request else None


def status(db_path, req_id, today=None):
    """The request with each line's state, in words. None when there is no such request."""
    head = _head(db_path, req_id)
    if head is None:
        return None
    _id, requester, site, work_item, needed_by, state, note, request_id, created = head
    today = today or date.today()
    since = created.date()
    gate_state = _gate_state(db_path, request_id)
    out = []
    for line_no, article, quantity, reserved, reservation_id, to_order in _lines(db_path, req_id):
        if to_order <= 0:
            word = "in stock"
        elif _delivered(db_path, article, site, work_item, since) + 1e-9 >= to_order:
            word = "delivered"
        elif state == "cancelled" or gate_state == "cancelled":
            word = "cancelled"
        elif gate_state == "ordered":
            word = "ordered"
        elif gate_state == "approved":
            word = "approved"
        else:
            word = "waiting"
        out.append({"line_no": line_no, "article": article, "quantity": quantity, "reserved": reserved,
                    "to_order": to_order, "state": word, "says": LINE_WORDS[word]})
    finished = bool(out) and all(row["state"] in ("in stock", "delivered") for row in out)
    shown = "cancelled" if state == "cancelled" else "done" if finished else "open"
    late = bool(needed_by and shown == "open" and needed_by < today)
    return {"req_id": req_id, "requester": requester, "site": site, "work_item": work_item,
            "needed_by": needed_by.isoformat() if needed_by else None, "state": shown, "late": late,
            "note": note, "created_at": created.isoformat(timespec="minutes"),
            "gate_request_id": request_id, "lines": out,
            "summary": _summary(out, shown, late)}


def _summary(lines, state, late):
    if state in ("done", "cancelled"):
        return state
    counts = {}
    for row in lines:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    parts = [f"{n} {name}" for name, n in counts.items()]
    return ("LATE: " if late else "") + ", ".join(parts)


def listing(db_path, include_done=False, limit=60, today=None):
    ids = [r[0] for r in db.read(db_path, "SELECT req_id FROM requisitions ORDER BY req_id DESC LIMIT ?", [limit])]
    rows = [status(db_path, i, today) for i in ids]
    return [r for r in rows if include_done or r["state"] == "open"]


def cancel(db_path, req_id, who=None):
    """Drop a request: its reservations are released and its basket is cancelled if nobody has acted on it."""
    head = _head(db_path, req_id)
    if head is None:
        raise ValueError(f"No request #{req_id}.")
    for _no, _article, _qty, _reserved, reservation_id, _to in _lines(db_path, req_id):
        if reservation_id:
            stockctl.release(db_path, reservation_id)
    request_id = head[7]
    if request_id and _gate_state(db_path, request_id) in ("waiting", "checked"):
        gate.cancel(db_path, request_id, who or WHO, f"request #{req_id} cancelled")
    db.write(db_path, "UPDATE requisitions SET state = 'cancelled', closed_at = now() WHERE req_id = ? RETURNING req_id",
             [req_id])
    return status(db_path, req_id)


def refresh(db_path, today=None):
    """The hourly pass. Holds back stock that has arrived for a line, closes finished requests, and
    gives back the requests that have just finished, so the requester can be told."""
    just_done = []
    for req_id, state in db.read(db_path, "SELECT req_id, state FROM requisitions WHERE state = 'open'"):
        now = status(db_path, req_id, today)
        if now is None:
            continue
        head = _head(db_path, req_id)
        for row in now["lines"]:
            if row["state"] == "delivered" and row["reserved"] < row["quantity"] - 1e-9:
                try:
                    taken = stockctl.reserve(db_path, row["article"], head[2], head[3],
                                             row["quantity"] - row["reserved"],
                                             head[4].isoformat() if head[4] else None, WHO, f"request #{req_id}")
                    db.write(db_path, "UPDATE requisition_lines SET reserved = quantity, reservation_id = ? "
                                      "WHERE req_id = ? AND line_no = ? RETURNING line_no",
                             [taken["reservation_id"], req_id, row["line_no"]])
                except ValueError:
                    pass                                 # not free (used by something else): left as delivered
        if now["state"] == "done":
            db.write(db_path, "UPDATE requisitions SET state = 'done', closed_at = now() WHERE req_id = ? "
                              "RETURNING req_id", [req_id])
            just_done.append(now)
    return just_done


def late(db_path, today=None):
    return [r for r in listing(db_path, today=today) if r["late"]]


def text(row):
    """The request in a few lines, for a chat or a push."""
    where = f"{row['site']} / {row['work_item']}"
    need = f", needed {row['needed_by']}" if row["needed_by"] else ""
    body = "\n".join(f"· {int(l['quantity']) if float(l['quantity']).is_integer() else l['quantity']} × "
                     f"{l['article']}: {l['says']}" for l in row["lines"][:25])
    return f"**Request #{row['req_id']}** for {where}{need}: {row['summary']}\n{body}"
