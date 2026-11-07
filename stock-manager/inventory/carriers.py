"""Carriers: a delivery the own vans cannot take is booked with a carrier, and followed to the signature.

A carrier is set up like a supplier webhook (channels.py): two addresses and a token.

  book    POST <book_url>     the delivery as JSON, signed (Idempotency-Key, X-Timestamp, X-Signature)
                              answered with {"tracking": "...", "label_url": "...", "eta": "YYYY-MM-DD"}
  track   GET  <track_url>    "{tracking}" in the address is replaced by the tracking number; the token
                              goes as a bearer token; answered with {"status": "...", "eta": "...",
                              "signed_by": "...", "detail": "..."}

Statuses are mapped to four states: booked, in transit, delivered, exception. A delivered shipment
records the proof of delivery on its dispatch by itself, with the name the carrier gives as the
signature. An exception, or an eta that has passed, is an alert for a person. Tracking is polled
(hourly): the app is not reachable from outside, so nothing is pushed in.
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from . import channels, db, outbound, routing
from .models import normalize_name, parse_date

TIMEOUT = 20
OPEN_STATES = ("booked", "in transit")

DELIVERED = {"delivered", "delivered_ok", "pod", "completed", "signed"}
MOVING = {"in_transit", "in transit", "picked_up", "picked up", "out_for_delivery", "out for delivery",
          "collected", "departed", "arrived_at_hub", "on_the_way"}
TROUBLE = {"exception", "failed", "failed_attempt", "returned", "return_to_sender", "lost", "damaged",
           "refused", "undeliverable"}


def listing(db_path):
    return [{"name": n, "book_url": b, "track_url": t, "has_token": bool(tok), "is_default": bool(d)}
            for n, b, t, tok, d in db.read(db_path, """
                SELECT name, book_url, track_url, token, is_default FROM carriers ORDER BY name""")]


def get(db_path, name):
    rows = db.read(db_path, "SELECT name, book_url, track_url, token, is_default FROM carriers "
                            "WHERE upper(name) = upper(?)", [name or ""])
    if not rows:
        return None
    n, b, t, tok, d = rows[0]
    return {"name": n, "book_url": b, "track_url": t, "token": tok or "", "is_default": bool(d)}


def default(db_path):
    rows = db.read(db_path, "SELECT name FROM carriers WHERE is_default AND book_url IS NOT NULL LIMIT 1")
    return rows[0][0] if rows else None


def save(db_path, name, book_url, track_url, token=None, is_default=False):
    name = normalize_name(name)
    if not name:
        raise ValueError("Name the carrier.")
    book_url = channels._check_url(book_url)
    track_url = (track_url or "").strip()
    if track_url:
        channels._check_url(track_url.replace("{tracking}", "x"))
        if "{tracking}" not in track_url:
            raise ValueError("The tracking address must hold {tracking} where the number goes.")
    keep = get(db_path, name)
    secret = (token or "").strip() or (keep["token"] if keep else "")
    if not secret:
        raise ValueError("A token is needed to sign and to ask.")
    if is_default:
        db.write(db_path, "UPDATE carriers SET is_default = false WHERE true RETURNING name")
    db.write(db_path, """
            INSERT INTO carriers (name, book_url, track_url, token, is_default, updated_at)
            VALUES (?, ?, ?, ?, ?, now())
            ON CONFLICT (name) DO UPDATE SET book_url = excluded.book_url, track_url = excluded.track_url,
                token = excluded.token, is_default = excluded.is_default, updated_at = now()
            RETURNING name""", [name, book_url, track_url or None, secret, bool(is_default)])
    return listing(db_path)


def remove(db_path, name):
    db.write(db_path, "DELETE FROM carriers WHERE upper(name) = upper(?) RETURNING name", [name or ""])
    return listing(db_path)


def _destination(db_path, list_id):
    """Who and where the order goes, from its contact in the contact book."""
    rows = db.read(db_path, """
            SELECT c.company, c.person, c.phone, c.email, c.address FROM purchase_orders h
            LEFT JOIN contacts c ON c.contact_id = h.contact_id WHERE h.list_id = ?""", [list_id])
    if not rows:
        return {}
    company, person, phone, email, address = rows[0]
    return {"company": company, "person": person, "phone": phone, "email": email, "address": address}


def payload(db_path, dispatch):
    depot = routing.depot(db_path)
    destination = _destination(db_path, dispatch["list_id"])
    ship = db.read(db_path, "SELECT ship_by, incoterm FROM purchase_orders WHERE list_id = ?",
                   [dispatch["list_id"]])
    ship_by, incoterm = ship[0] if ship else (None, None)
    return {"reference": f"dispatch-{dispatch['dispatch_id']}", "order_number": dispatch["list_id"],
            "pickup": {"label": depot["label"] if depot else None, "address": depot["address"] if depot else None},
            "delivery": {"site": dispatch["site"], "work_item": dispatch["work_item"], **destination},
            "ship_by": ship_by.isoformat() if ship_by else None, "incoterm": incoterm,
            "planned_on": dispatch["planned_on"],
            "parcels": [{"article": l["article"], "designation": l["designation"], "quantity": l["quantity"]}
                        for l in dispatch["lines"]]}


def _post(url, body, headers):
    return channels._post(url, body, headers)


def _get(url, headers):
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            return answer.status, answer.read(50000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", errors="replace")


def _json(text):
    try:
        found = json.loads(text) if (text or "").strip().startswith("{") else {}
    except ValueError:
        found = {}
    return found if isinstance(found, dict) else {}


def one(db_path, shipment_id):
    rows = db.read(db_path, _SELECT + " WHERE shipment_id = ?", [shipment_id])
    return _shape(rows[0]) if rows else None


_SELECT = """SELECT shipment_id, dispatch_id, carrier, tracking, label_url, state, booked_at, last_checked,
                    eta, signed_by, detail, error FROM shipments"""


def _shape(values):
    sid, did, carrier, tracking, label, state, booked, checked, eta, signed, detail, error = values
    return {"shipment_id": sid, "dispatch_id": did, "carrier": carrier, "tracking": tracking,
            "label_url": label, "state": state, "booked_at": booked.isoformat(timespec="minutes"),
            "last_checked": checked.isoformat(timespec="minutes") if checked else None,
            "eta": eta.isoformat() if eta else None, "signed_by": signed, "detail": detail, "error": error}


def shipments(db_path, open_only=False, limit=60):
    where = " WHERE state IN ('booked', 'in transit')" if open_only else ""
    return [_shape(r) for r in db.read(db_path, f"{_SELECT}{where} ORDER BY shipment_id DESC LIMIT ?", [limit])]


def for_dispatch(db_path, dispatch_id):
    rows = db.read(db_path, _SELECT + " WHERE dispatch_id = ? ORDER BY shipment_id DESC LIMIT 1", [dispatch_id])
    return _shape(rows[0]) if rows else None


def book(db_path, dispatch_id, carrier=None, user=None, post=_post):
    """Book a planned dispatch with a carrier. Never twice: a dispatch with a live shipment is refused."""
    dispatch = outbound.one(db_path, dispatch_id)
    if dispatch is None:
        raise ValueError(f"No dispatch #{dispatch_id}.")
    if dispatch["state"] not in ("planned", "loaded"):
        raise ValueError(f"Dispatch #{dispatch_id} is {dispatch['state']}.")
    live = for_dispatch(db_path, dispatch_id)
    if live and live["state"] in OPEN_STATES + ("delivered",):
        raise ValueError(f"Dispatch #{dispatch_id} is already booked with {live['carrier']}.")
    chosen = get(db_path, carrier) if carrier else get(db_path, default(db_path))
    if chosen is None or not chosen["book_url"]:
        raise ValueError("No carrier is set up to book with (System, Connections).")
    body = json.dumps(payload(db_path, dispatch), ensure_ascii=False).encode("utf-8")
    stamp = str(int(time.time()))
    headers = {"Content-Type": "application/json; charset=utf-8", "X-Timestamp": stamp,
               "Idempotency-Key": f"stock-manager-dispatch-{dispatch_id}",
               "X-Signature": channels._sign(chosen["token"], stamp, body), "User-Agent": "stock-manager"}
    try:
        status, text = post(chosen["book_url"], body, headers)
    except Exception as e:
        status, text = 0, str(e)[:300]
    if not 200 <= status < 300:
        raise ValueError(f"{chosen['name']} answered {status or 'nothing'}: {(text or '')[:160]}")
    answer = _json(text)
    tracking = str(answer.get("tracking") or "").strip() or None
    row = db.write(db_path, """
            INSERT INTO shipments (dispatch_id, carrier, tracking, label_url, eta, detail)
            VALUES (?, ?, ?, ?, ?, ?) RETURNING shipment_id""",
                   [dispatch_id, chosen["name"], tracking, answer.get("label_url"), parse_date(answer.get("eta")),
                    "booked"])
    db.write(db_path, "UPDATE dispatches SET note = ? WHERE dispatch_id = ? RETURNING dispatch_id",
             [f"carried by {chosen['name']}" + (f", tracking {tracking}" if tracking else ""), dispatch_id])
    return one(db_path, row[0])


def _state_of(word):
    word = (word or "").strip().lower().replace("-", "_")
    if word in DELIVERED:
        return "delivered"
    if word in TROUBLE:
        return "exception"
    if word in MOVING:
        return "in transit"
    return None


def track(db_path, get_=_get, now=None):
    """Ask each carrier where its open shipments are. Gives back what changed, in words."""
    changed = []
    for row in shipments(db_path, open_only=True):
        carrier = get(db_path, row["carrier"])
        if carrier is None or not carrier["track_url"] or not row["tracking"]:
            continue
        url = carrier["track_url"].replace("{tracking}", urllib.parse.quote(row["tracking"], safe=""))
        try:
            status, text = get_(url, {"Authorization": f"Bearer {carrier['token']}",
                                      "Accept": "application/json", "User-Agent": "stock-manager"})
        except Exception as e:
            status, text = 0, str(e)[:200]
        if not 200 <= status < 300:
            db.write(db_path, "UPDATE shipments SET last_checked = now(), error = ? WHERE shipment_id = ? "
                              "RETURNING shipment_id", [f"{carrier['name']} answered {status or 'nothing'}",
                                                        row["shipment_id"]])
            continue
        answer = _json(text)
        state = _state_of(answer.get("status")) or row["state"]
        eta = parse_date(answer.get("eta")) or (parse_date(row["eta"]) if row["eta"] else None)
        signed = (answer.get("signed_by") or "").strip() or None
        db.write(db_path, """
                UPDATE shipments SET state = ?, eta = ?, signed_by = coalesce(?, signed_by), detail = ?,
                       last_checked = now(), error = NULL
                WHERE shipment_id = ? RETURNING shipment_id""",
                 [state, eta, signed, (answer.get("detail") or answer.get("status") or "")[:200],
                  row["shipment_id"]])
        if state != row["state"]:
            changed.append(f"dispatch #{row['dispatch_id']}: {carrier['name']} says {state}")
        if state == "delivered":
            try:
                outbound.deliver(db_path, row["dispatch_id"], signed or carrier["name"],
                                 note=f"delivered, proof from {carrier['name']} ({row['tracking']})", now=now)
            except ValueError:
                pass                                 # a person already closed it
    return changed


def late(db_path, today=None):
    """Open shipments past their eta, and shipments in trouble: the ones a person must look at."""
    today = today or date.today()
    out = []
    for row in shipments(db_path, limit=200):
        if row["state"] == "exception":
            out.append({**row, "why": "the carrier reports a problem"})
        elif row["state"] in OPEN_STATES and row["eta"] and parse_date(row["eta"]) < today:
            out.append({**row, "why": f"past its eta, {row['eta']}"})
    return out


def test(db_path, name, post=_post):
    """A marked test booking: an empty document with test=true. Nothing is booked."""
    chosen = get(db_path, name)
    if chosen is None:
        raise ValueError("No such carrier.")
    body = json.dumps({"test": True, "reference": "test"}).encode("utf-8")
    stamp = str(int(time.time()))
    status, text = post(chosen["book_url"], body, {
        "Content-Type": "application/json", "X-Timestamp": stamp, "Idempotency-Key": "stock-manager-test",
        "X-Signature": channels._sign(chosen["token"], stamp, body), "User-Agent": "stock-manager"})
    if not 200 <= status < 300:
        raise ValueError(f"{chosen['name']} answered {status or 'nothing'}: {(text or '')[:160]}")
    return {"status": status}
