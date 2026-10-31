"""The gate on what the chat bot buys, and the sealed log that remembers every turn of it.

A basket is free: anyone can fill one. Buying is not. Four things hold a basket back and
ask for a person:

  first-article   this article has never been bought before
  new-supplier    the article is known, this supplier is not
  over-ceiling    the basket comes to more than the ceiling
  dear-line       a line costs more than twice what that article usually costs
  no-price        a line carries no price at all, so neither money rule can be checked

Held is held: a request that is not approved stays waiting for as long as it takes. There
is no expiry, only a cancel. What there is, is a drift check — an approval remembers the
price it was given at, and if the price has moved since, the order is asked for again
rather than going through at a number nobody agreed to.

Every turn is written to gate_log, which is sealed: each entry carries the hash of the one
before it, so a line that is edited afterwards breaks the chain from there on and
`verify_chain` says where. Nothing is ever deleted — a cancel is an entry of its own.
"""

import hashlib
import json
from datetime import datetime

from . import db
from .models import normalize_name

# A basket worth more than this is held, whatever is in it.
CEILING = 500.0
# A line at more than this many times the usual price is held.
DEAR = 2.0
# How far a price may move between the approval and the order before it is asked again.
DRIFT = 0.10

GENESIS = "0" * 64

WAITING = "waiting"
CHECKED = "checked"
APPROVED = "approved"
CANCELLED = "cancelled"
ORDERED = "ordered"

OPEN_STATES = (WAITING, CHECKED)

REASON_WORDS = {
    "first-article": "first time this article is bought",
    "new-supplier": "a supplier this article has not come from before",
    "over-ceiling": f"the basket comes to more than {CEILING:.0f} EUR",
    "dear-line": f"a line costs more than {DEAR:g} times the usual price",
    "no-price": "a line carries no price, so the money rules cannot be checked",
}


# ---- the sealed log -------------------------------------------------------

def _canon(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _digest(prev_seal, request_id, event, who, made_at, body):
    stem = "|".join([prev_seal, str(request_id or ""), event, str(who or ""),
                     made_at, _canon(body)])
    return hashlib.sha256(stem.encode("utf-8")).hexdigest()


def last_seal(db_path):
    row = db.read(db_path, "SELECT seal FROM gate_log ORDER BY entry_id DESC LIMIT 1")
    return row[0][0] if row else GENESIS


def seal(db_path, request_id, event, body, who=None):
    """Append one entry, sealed against the one before it."""
    prev = last_seal(db_path)
    made_at = datetime.now().isoformat(timespec="seconds")
    mark = _digest(prev, request_id, event, who, made_at, body)
    db.write(db_path, """
            INSERT INTO gate_log (request_id, event, body, who, made_at, prev_seal, seal)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (request_id, event, _canon(body), who, made_at, prev, mark))
    return mark


def chain(db_path, request_id=None):
    where = "WHERE request_id = ?" if request_id is not None else ""
    args = (request_id,) if request_id is not None else ()
    rows = db.read(db_path, f"""
            SELECT entry_id, request_id, event, body, who, made_at, prev_seal, seal
            FROM gate_log {where} ORDER BY entry_id
            """, args)
    return [{"entry_id": entry, "request_id": request, "event": event,
             "body": json.loads(body), "who": who,
             "made_at": made_at.isoformat(timespec="seconds") if made_at else None,
             "prev_seal": prev, "seal": mark}
            for entry, request, event, body, who, made_at, prev, mark in rows]


def verify_chain(db_path):
    """(the chain holds, the first entry that does not, how many were read)."""
    rows = db.read(db_path, """
            SELECT entry_id, request_id, event, body, who, made_at, prev_seal, seal
            FROM gate_log ORDER BY entry_id
            """)
    prev = GENESIS
    for entry, request, event, body, who, made_at, stored_prev, mark in rows:
        stamp = made_at.isoformat(timespec="seconds") if made_at else ""
        if stored_prev != prev:
            return False, entry, len(rows)
        if _digest(prev, request, event, who, stamp, json.loads(body)) != mark:
            return False, entry, len(rows)
        prev = mark
    return True, None, len(rows)


# ---- what fires the gate --------------------------------------------------

def bought_before(db_path):
    """Every article the firm has already bought, by any road."""
    rows = db.read(db_path, """
            SELECT DISTINCT article FROM inventory_lines WHERE article IS NOT NULL
            UNION
            SELECT DISTINCT article FROM purchase_order_lines WHERE article IS NOT NULL
            UNION
            SELECT DISTINCT article FROM article_prices WHERE source <> 'basket'
            """)
    return {row[0] for row in rows if row[0]}


def look(db_path, lines, supplier=None):
    """Read a basket and say what, if anything, holds it back.

    Gives back the lines with what was found against each of them, the basket total, and
    the reasons in the order they were met. An empty reasons list means it may go through.
    """
    from . import prices

    known = bought_before(db_path)
    read = []
    reasons = []

    for line in lines:
        article = normalize_name(line.get("article"))
        price = line.get("unit_price")
        at = normalize_name(line.get("supplier") or supplier)
        found = []

        if article and article not in known:
            found.append("first-article")
        elif article and at and at not in prices.suppliers(db_path, article):
            found.append("new-supplier")

        usual = prices.usual(db_path, article) if article else None
        if price is None:
            found.append("no-price")
        elif usual and float(price) > usual * DEAR:
            found.append("dear-line")

        read.append({**line, "article": article, "supplier": at, "usual": usual,
                     "found": found})
        for one in found:
            if one not in reasons:
                reasons.append(one)

    total, unpriced = prices.basket_total(read)
    if total > CEILING:
        if "over-ceiling" not in reasons:
            reasons.append("over-ceiling")

    return {"lines": read, "total": round(total, 2), "unpriced": unpriced,
            "reasons": reasons, "held": bool(reasons)}


def said(reasons):
    return [REASON_WORDS.get(one, one) for one in reasons]


# ---- the requests ---------------------------------------------------------

def ask(db_path, reading, channel=None, shop=None, asked_by=None, site=None, work_item=None):
    """Hold a basket and write down why. Gives back the request id."""
    body = {"lines": reading["lines"], "total": reading["total"],
            "reasons": reading["reasons"], "shop": shop, "site": site,
            "work_item": work_item, "channel": str(channel) if channel else None}
    row = db.write(db_path, """
            INSERT INTO gate_requests
                (channel, shop, asked_by, site, work_item, reasons, basket, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING request_id
            """, (str(channel) if channel else None, shop, asked_by, normalize_name(site),
                  normalize_name(work_item), ",".join(reading["reasons"]),
                  _canon(reading["lines"]), reading["total"]))
    request_id = row[0]
    seal(db_path, request_id, "requested", body, asked_by)
    return request_id


def one(db_path, request_id):
    rows = db.read(db_path, """
            SELECT request_id, channel, shop, asked_by, site, work_item, reasons, basket,
                   total, state, agent_verdict, agent_said, agent_at, decided_by, decided_at,
                   asked_at
            FROM gate_requests WHERE request_id = ?
            """, (request_id,))
    return _row(rows[0]) if rows else None


def _row(row):
    (request_id, channel, shop, asked_by, site, work_item, reasons, basket, total, state,
     verdict, agent_said, agent_at, decided_by, decided_at, asked_at) = row
    return {
        "request_id": request_id, "channel": channel, "shop": shop, "asked_by": asked_by,
        "site": site, "work_item": work_item,
        "reasons": [one for one in (reasons or "").split(",") if one],
        "lines": json.loads(basket), "total": total, "state": state,
        "agent_verdict": verdict, "agent_said": agent_said,
        "agent_at": agent_at.isoformat(timespec="seconds") if agent_at else None,
        "decided_by": decided_by,
        "decided_at": decided_at.isoformat(timespec="seconds") if decided_at else None,
        "asked_at": asked_at.isoformat(timespec="seconds") if asked_at else None,
    }


def waiting(db_path):
    """Everything still asking for an answer, oldest first."""
    rows = db.read(db_path, f"""
            SELECT request_id, channel, shop, asked_by, site, work_item, reasons, basket,
                   total, state, agent_verdict, agent_said, agent_at, decided_by, decided_at,
                   asked_at
            FROM gate_requests
            WHERE state IN ('{WAITING}', '{CHECKED}') ORDER BY request_id
            """)
    return [_row(row) for row in rows]


def history(db_path, limit=40):
    rows = db.read(db_path, """
            SELECT request_id, channel, shop, asked_by, site, work_item, reasons, basket,
                   total, state, agent_verdict, agent_said, agent_at, decided_by, decided_at,
                   asked_at
            FROM gate_requests ORDER BY request_id DESC LIMIT ?
            """, (limit,))
    return [_row(row) for row in rows]


def record_check(db_path, request_id, verdict, agent_said, matches=()):
    """What the verification agent made of it. It settles the name, never the purchase."""
    db.write(db_path, """
            UPDATE gate_requests
            SET agent_verdict = ?, agent_said = ?, agent_at = current_timestamp,
                state = CASE WHEN state = ? THEN ? ELSE state END
            WHERE request_id = ?
            """, (verdict, agent_said, WAITING, CHECKED, request_id))
    seal(db_path, request_id, "checked",
         {"verdict": verdict, "said": agent_said, "matches": list(matches)}, "agent")
    return one(db_path, request_id)


def approve(db_path, request_id, who):
    """The second factor, and the only one that can let money out."""
    now = one(db_path, request_id)
    if now is None:
        raise ValueError(f"No request #{request_id}.")
    if now["state"] not in OPEN_STATES:
        raise ValueError(f"Request #{request_id} is already {now['state']}.")
    db.write(db_path, """
            UPDATE gate_requests SET state = ?, decided_by = ?, decided_at = current_timestamp
            WHERE request_id = ?
            """, (APPROVED, who, request_id))
    seal(db_path, request_id, "approved",
         {"total": now["total"], "lines": now["lines"]}, who)
    return one(db_path, request_id)


def cancel(db_path, request_id, who, why=None):
    """Takes an approval back, or drops a request that was never answered."""
    now = one(db_path, request_id)
    if now is None:
        raise ValueError(f"No request #{request_id}.")
    if now["state"] == ORDERED:
        raise ValueError(f"Request #{request_id} has been ordered; it cannot be cancelled.")
    db.write(db_path, """
            UPDATE gate_requests SET state = ?, decided_by = ?, decided_at = current_timestamp
            WHERE request_id = ?
            """, (CANCELLED, who, request_id))
    seal(db_path, request_id, "cancelled", {"was": now["state"], "why": why}, who)
    return one(db_path, request_id)


def may_order(db_path, request_id, lines_now=None):
    """(the bot may place it, why not).

    Approved is not enough on its own: the prices have to be the ones that were approved,
    within the drift. An approval never expires, so this is what stops a yes from three
    months ago paying today's price without anybody looking.
    """
    now = one(db_path, request_id)
    if now is None:
        return False, "there is no such request"
    if now["state"] != APPROVED:
        return False, f"the request is {now['state']}, not approved"

    if lines_now is None:
        return True, ""

    agreed = {line.get("article"): line.get("unit_price") for line in now["lines"]}
    for line in lines_now:
        article = normalize_name(line.get("article"))
        was, is_now = agreed.get(article), line.get("unit_price")
        if was is None or is_now is None:
            continue
        if float(was) <= 0:
            continue
        if abs(float(is_now) - float(was)) / float(was) > DRIFT:
            return False, (f"{article} was approved at {float(was):.2f} and is now "
                           f"{float(is_now):.2f}; it has to be approved again")
    return True, ""


def mark_ordered(db_path, request_id, who, reference=None):
    """The order has been placed. What it cost becomes the history the next gate reads."""
    from . import prices

    now = one(db_path, request_id)
    if now is None:
        raise ValueError(f"No request #{request_id}.")
    allowed, why = may_order(db_path, request_id)
    if not allowed:
        raise ValueError(f"Request #{request_id} may not be ordered: {why}.")

    db.write(db_path, "UPDATE gate_requests SET state = ? WHERE request_id = ?",
             (ORDERED, request_id))
    for line in now["lines"]:
        prices.remember(db_path, line.get("article"), line.get("unit_price"),
                        supplier=line.get("supplier"), source="order")
    seal(db_path, request_id, "ordered", {"reference": reference, "total": now["total"]}, who)
    return one(db_path, request_id)
