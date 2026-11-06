"""How an order reaches a supplier: by mail, which is the default, or by a webhook.

A supplier with a portal or an API takes the order as a JSON document posted to an address the
supplier gave. The same draft that would have been mailed is posted instead, so the whole round
(gate, Terminus, wallet, policy limits, dry run) is the same for both; only the last step differs.

What goes over the wire:

  POST <url>
    Content-Type: application/json
    Idempotency-Key: stock-manager-order-<mail id>     the same order posted twice is one order
    X-Timestamp: <unix seconds>
    X-Signature: sha256=<hex>                          HMAC-SHA256 of "<timestamp>.<body>" with the token

The supplier answers 2xx to accept. The body may be JSON with `confirmed`, `reference` and
`promised_date`; a promised date becomes the day to expect the goods. Any other status is a
refusal: the draft stays, marked failed, with the answer. The token is kept in the database and
is never given back to a screen.
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import date
from urllib.parse import urlparse

from . import db, outbox
from .models import parse_date

TIMEOUT = 20
LOCAL = ("localhost", "127.0.0.1", "::1")


def _check_url(url):
    parts = urlparse((url or "").strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("The address must start with https://.")
    if parts.scheme == "http" and parts.hostname not in LOCAL:
        raise ValueError("Only https:// is allowed, except for a test on this machine.")
    return parts.geturl()


def get(db_path, supplier):
    rows = db.read(db_path, "SELECT supplier, kind, url, token FROM supplier_channels "
                            "WHERE upper(supplier) = upper(?)", [supplier or ""])
    if not rows or rows[0][1] != "webhook" or not rows[0][2]:
        return None
    return {"supplier": rows[0][0], "kind": rows[0][1], "url": rows[0][2], "token": rows[0][3] or ""}


def has(db_path, supplier):
    return get(db_path, supplier) is not None


def listing(db_path):
    """Every supplier set up for a webhook. The token is only said to exist."""
    return [{"supplier": s, "kind": k, "url": u, "has_token": bool(t),
             "last": last} for s, k, u, t, last in db.read(db_path, """
                SELECT c.supplier, c.kind, c.url, c.token,
                       (SELECT CAST(max(sent_at) AS VARCHAR) FROM order_deliveries d
                        WHERE upper(d.supplier) = upper(c.supplier))
                FROM supplier_channels c ORDER BY c.supplier""")]


def save(db_path, supplier, url, token=None):
    supplier = (supplier or "").strip()
    if not supplier:
        raise ValueError("Name the supplier.")
    url = _check_url(url)
    keep = db.read(db_path, "SELECT token FROM supplier_channels WHERE upper(supplier) = upper(?)", [supplier])
    secret = (token or "").strip() or (keep[0][0] if keep else "")
    if not secret:
        raise ValueError("A token is needed to sign the orders.")
    db.write(db_path, """
            INSERT INTO supplier_channels (supplier, kind, url, token, updated_at)
            VALUES (?, 'webhook', ?, ?, now())
            ON CONFLICT (supplier) DO UPDATE SET url = excluded.url, token = excluded.token,
                kind = 'webhook', updated_at = now()
            RETURNING supplier""", [supplier, url, secret])
    return listing(db_path)


def remove(db_path, supplier):
    db.write(db_path, "DELETE FROM supplier_channels WHERE upper(supplier) = upper(?) RETURNING supplier",
             [supplier or ""])
    return listing(db_path)


def payload(db_path, row):
    """The order document for a draft: the lines this supplier is to send, in the app's own names."""
    list_id = row["ref_id"]
    site, work_item, groups, _left = outbox._order_groups(db_path, list_id)
    billing = outbox.bill_to(db_path, list_id)
    ship = db.read(db_path, "SELECT ship_by, incoterm FROM purchase_orders WHERE list_id = ?", [list_id])
    ship_by, incoterm = ship[0] if ship else (None, None)
    lines = []
    for article, designation, reference, quantity in groups.get(row["supplier"], []):
        text, _short, shown = outbox._line_text(db_path, row["supplier"], article, designation, reference, quantity)
        lines.append({"article": article, "designation": designation, "supplier_reference": reference,
                      "quantity": float(quantity), "as_written": text})
    return {"order_number": list_id, "supplier": row["supplier"], "site": site, "work_item": work_item,
            "ship_by": ship_by.isoformat() if ship_by else None, "incoterm": incoterm,
            "bill_to": billing, "lines": lines, "sent_on": date.today().isoformat()}


def _sign(token, stamp, body):
    return "sha256=" + hmac.new(token.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()


def _post(url, body, headers):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            return answer.status, answer.read(20000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", errors="replace")


def deliver(db_path, row, test=False, post=_post):
    """Post a draft order to its supplier. Gives back {status, reference, promised_date}; raises when refused."""
    via = get(db_path, row["supplier"])
    if via is None:
        raise ValueError(f"{row['supplier']} has no order webhook.")
    document = payload(db_path, row) if not test else {
        "order_number": 0, "supplier": row["supplier"], "lines": [], "test": True}
    document["test"] = bool(test)
    body = json.dumps(document, ensure_ascii=False).encode("utf-8")
    stamp = str(int(time.time()))
    headers = {"Content-Type": "application/json; charset=utf-8", "X-Timestamp": stamp,
               "Idempotency-Key": f"stock-manager-order-{row['mail_id']}",
               "X-Signature": _sign(via["token"], stamp, body), "User-Agent": "stock-manager"}
    try:
        status, text = post(via["url"], body, headers)
    except Exception as e:
        status, text = 0, str(e)[:300]
    ok = 200 <= status < 300
    db.write(db_path, """
            INSERT INTO order_deliveries (mail_id, supplier, url, status, ok, answer)
            VALUES (?, ?, ?, ?, ?, ?) RETURNING delivery_id""",
             [row["mail_id"] if not test else None, row["supplier"], via["url"], status, ok, (text or "")[:1000]])
    if not ok:
        raise outbox.SendError(f"{row['supplier']} answered {status or 'nothing'}: {(text or '')[:160]}")
    answer = {}
    try:
        answer = json.loads(text) if text.strip().startswith("{") else {}
    except ValueError:
        answer = {}
    return {"status": status, "reference": answer.get("reference"),
            "confirmed": bool(answer.get("confirmed")), "promised_date": parse_date(answer.get("promised_date"))}


def test(db_path, supplier, post=_post):
    """Ping a supplier's webhook with an empty, marked test document. Nothing is ordered."""
    row = {"supplier": supplier, "mail_id": 0}
    return deliver(db_path, row, test=True, post=post)
