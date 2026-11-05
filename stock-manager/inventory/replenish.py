"""Plutus orders the way a logistics manager does: on a rhythm, for the whole cycle, with common sense.

A person who buys for a workshop does not place an order every time a shelf dips. They sit down
on the same day every fortnight, look at everything, and write one consolidated order, one mail
a supplier, for whatever will not last until the next sitting. Between sittings they place a
small urgent order only for what is about to run out with nothing on the way. That is what this
does, from the DDMRP buffers:

  The cycle order   every `cycle_days` (14) on `order_weekday` (Monday), at working hours, on a
                    working day. For each article that has been counted and is in use, the net flow
                    is projected to the next sitting; if it would be under the top of yellow by then
                    the article is ordered, up to the top of green plus the use of the cycle, so that
                    at the next sitting it is back at the top of green. Red first, then by days of
                    cover left. Grouped by supplier when the mails are drafted; whole packs.
  The urgent order  between cycles, only for an article already in its red zone that nothing is on
                    the way for, and only up to the buffer's own number. Small, capped, separate.
  What it leaves    an article already ordered, drafted, queued, owed or recently ordered by Plutus; one
  alone             nobody has counted (it asks for a count instead); one with no use in the last eight
                    weeks; and anything past the cover limit (no more than `max_cover_days` of use).

The order is a list, not a purchase. The charter's limits (lines, euros, refused articles) still
cut it, and the policies decide whether the supplier mails go without a person; a big cycle order
is over their single-action ceiling, so it goes to a person to read.
"""

from datetime import date, datetime, timedelta

from . import audit, charters, db, ddmrp, feedback, prices, stockctl, units, workdays

ORDER_HOUR = 8
DORMANT_WEEKS = 8
MIN_COVER_DAYS = 3            # how long an order of ours counts as on the way, at the least


# ---- the switch and the clock ----------------------------------------------

def _setting(db_path, key):
    rows = db.read(db_path, "SELECT value FROM policy_settings WHERE key = ?", [key])
    return rows[0][0] if rows else None


def _put(db_path, key, value):
    db.write(db_path, "INSERT INTO policy_settings (key, value) VALUES (?, ?) "
                      "ON CONFLICT (key) DO UPDATE SET value = excluded.value RETURNING key", [key, value])


def auto_enabled(db_path):
    """On unless somebody has switched it off."""
    return _setting(db_path, "plutus_auto") != "0"


def set_auto(db_path, on, user=None):
    _put(db_path, "plutus_auto", "1" if on else "0")
    audit.record(db_path, user or "PLUTUS", "replenish.auto", "plutus", "on" if on else "off")


def last_cycle(db_path):
    value = _setting(db_path, "plutus_last_cycle")
    return date.fromisoformat(value) if value else None


def next_cycle(db_path, today=None):
    """The next day the cycle order is written: the order weekday, `cycle_days` after the last one
    (or the next such weekday if there has been none), moved on past a day nobody works."""
    today = today or date.today()
    rules = charters.limits(db_path, "plutus")
    start = (last_cycle(db_path) + timedelta(days=int(rules["cycle_days"]))
             if last_cycle(db_path) else today)
    day = start + timedelta(days=(int(rules["order_weekday"]) - start.weekday()) % 7)
    return workdays.Calendar(db_path).next_working(day)


def cycle_due(db_path, now=None):
    now = now or datetime.now()
    return now.hour >= ORDER_HOUR and now.date() >= next_cycle(db_path, now.date()) \
        and workdays.Calendar(db_path).working(now.date())


# ---- what is already on its way ---------------------------------------------

def covered_by(db_path, article, lead_days, today):
    """Why this article need not be ordered now: something is already on its way. None if not."""
    window = max(MIN_COVER_DAYS, int(round(lead_days or 0)))
    since = today - timedelta(days=window)
    if db.read(db_path, "SELECT 1 FROM order_queue WHERE state = 'waiting' AND article = ?", [article]):
        return "it waits in the 15-day queue"
    if db.read(db_path, """
            SELECT 1 FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
            WHERE l.article = ? AND l.in_stock = FALSE AND upper(coalesce(h.drafter, '')) = 'PLUTUS'
              AND coalesce(h.list_date, CAST(h.created_at AS DATE)) >= ?""", [article, since]):
        return f"Plutus ordered it in the last {window} days"
    if db.read(db_path, """
            SELECT 1 FROM supplier_mail WHERE direction = 'out' AND kind = 'order'
              AND state IN ('draft', 'failed', 'sent', 'confirmed')
              AND CAST(created_at AS DATE) >= ? AND upper(body) LIKE ?""", [since, f"%{article}%"]):
        return "an order mail for it is drafted or sent"
    if db.read(db_path, "SELECT 1 FROM inventory_lines WHERE article = ? AND backorder "
                        "AND settled_on IS NULL", [article]):
        return "a delivery note still owes it"
    return None


# ---- what to order ----------------------------------------------------------

def _build(db_path, today, kind):
    """(lines to order, left alone with the reason, articles to count) for a cycle or urgent order."""
    rules = charters.limits(db_path, "plutus")
    cycle = int(rules["cycle_days"])
    rows, _chain, _span = ddmrp.buffers(db_path)
    held = {}
    for hold in stockctl.reservations(db_path, today):            # stock held back is not free to use
        held[hold["article"]] = held.get(hold["article"], 0.0) + hold["left"]
    asked, left, to_count = [], [], []
    for row in rows:
        article, adu = row["article"], float(row["adu"])
        if adu <= 0:
            continue
        if row["zone"] == "uncounted":
            to_count.append({"article": article, "why": "never counted, so the shelf is a guess"})
            continue
        if sum(row["weeks"][-DORMANT_WEEKS:]) <= 0:
            left.append({"article": article, "zone": row["zone"], "why": f"no use in the last {DORMANT_WEEKS} weeks"})
            continue
        flow = float(row["net_flow"]) - held.get(article, 0.0)
        # Books below zero are the paperwork being wrong (more shipped than ever delivered), not an
        # empty shelf. A person would go and count it. Until somebody does, it is ordered from zero,
        # one full buffer at most, and the count is asked for.
        unreliable = flow < 0
        if unreliable:
            to_count.append({"article": article, "why": f"the books say {flow:.0f}, below zero: count it"})
            flow = 0.0
        if kind == "urgent":
            if row["zone"] != "red" or row["to_order"] <= 0:
                continue
            quantity, projected = float(row["to_order"]), None
        else:
            projected = flow - adu * cycle
            if projected > row["top_of_yellow"]:
                continue                                        # it will last until the next sitting
            quantity = max(float(row["to_order"]), row["top_of_green"] - flow + adu * cycle)
        # Common sense: never buy more than the cover limit of use, whatever the buffer says,
        # and never more than one full buffer for an article whose books cannot be trusted.
        room = adu * rules["max_cover_days"] - flow
        if quantity > room:
            quantity = room
        if unreliable and quantity > row["top_of_green"]:
            quantity = row["top_of_green"]
        if quantity < 1:
            left.append({"article": article, "zone": row["zone"], "why": "it already holds the cover limit"})
            continue
        why = covered_by(db_path, article, row["lead_days"], today)
        if why:
            left.append({"article": article, "zone": row["zone"], "why": why})
            continue
        packed = units.to_packs(db_path, article, row["supplier"], quantity)
        quantity = float(packed["base"]) if packed else float(round(quantity))
        days_left = flow / adu if adu else None
        reason = (f"{row['zone']} zone, {days_left:.0f} days of cover"
                  + (" (books below zero, count it)" if unreliable else "") + "; "
                  + (f"urgent: nothing is on the way and the buffer asks {quantity:g}" if kind == "urgent"
                     else f"at the next sitting in {cycle} days it would be at {max(projected, 0):.0f}, under the "
                          f"top of yellow {row['top_of_yellow']:.0f}; order {quantity:g} to be back at the top of "
                          f"green {row['top_of_green']:.0f}"))
        asked.append({"article": article, "supplier": row["supplier"], "zone": row["zone"],
                      "quantity": quantity, "packs": packed["packs"] if packed else None,
                      "pack_label": packed["pack_label"] if packed else None, "reason": reason,
                      "days_of_cover": round(days_left, 1) if days_left is not None else None,
                      "lead_days": row["lead_days"], "net_flow": row["net_flow"],
                      "top_of_green": row["top_of_green"]})
    asked.sort(key=lambda r: (r["zone"] != "red", r["days_of_cover"] if r["days_of_cover"] is not None else 1e9,
                              r["article"]))
    return asked, left, to_count[:30]


def _bundle(db_path, kept, asked, minimum):
    """A supplier whose whole order is not worth placing waits for the next sitting, unless one of
    its lines is already in red. A price nobody knows counts as worth placing."""
    zone = {r["article"]: r["zone"] for r in asked}
    by_supplier = {}
    for line in kept:
        by_supplier.setdefault(line.get("supplier") or "", []).append(line)
    out, cuts = [], []
    for supplier, lines in by_supplier.items():
        value, unknown = 0.0, False
        for line in lines:
            seen = prices.sightings(db_path, line["article"].upper(), 1)
            if seen:
                value += seen[0] * line["quantity"]
            else:
                unknown = True
        if (supplier and not unknown and value < minimum
                and not any(zone.get(l["article"]) == "red" for l in lines)):
            cuts += [{"article": l["article"].upper(), "why": f"{supplier}'s whole order is {value:.0f} EUR, "
                                                              f"under {minimum:g}: it waits for the next sitting"}
                     for l in lines]
            continue
        out += lines
    kept_order = [l for l in kept if l in out]
    return kept_order, cuts


def proposals(db_path, today=None, kind="cycle"):
    today = today or date.today()
    asked, left, _count = _build(db_path, today, kind)
    return asked, left


def place(db_path, user="PLUTUS", today=None, kind="cycle"):
    """Write one order: the cycle order, or the small urgent one. Gives back what was written."""
    today = today or date.today()
    asked, left, to_count = _build(db_path, today, kind)
    base = {"kind": kind, "left": left, "to_count": to_count}
    if not asked:
        return dict(base, list_id=None, lines=0, cuts=[])
    rules = charters.limits(db_path, "plutus")
    caps = ({"max_lines": rules["urgent_max_lines"], "max_order_value": rules["urgent_max_value"]}
            if kind == "urgent" else None)
    rows = [{"article": r["article"], "suggested": r["quantity"], "supplier": r["supplier"]} for r in asked]
    lines = [{"article": r["article"], "quantity": r["quantity"], "supplier": r["supplier"],
              "reason": r["reason"]} for r in asked]
    kept, cuts, _changed = charters.enforce_plutus(db_path, lines, rows, (), feedback.refusals(db_path), caps)
    if kind == "cycle" and kept:
        kept, small = _bundle(db_path, kept, asked, rules["min_supplier_order"])
        cuts += small
    if not kept:
        return dict(base, list_id=None, lines=0, cuts=cuts)
    list_id = db.save_purchase_order(db_path, {
        "list_date": today, "site": None, "work_item": "URGENT" if kind == "urgent" else None,
        "drafter": "PLUTUS",
        "lines": [{"article": line["article"], "designation": line["reason"],
                   "quantity": line["quantity"], "in_stock": False} for line in kept]}, None, None, "PLUTUS")
    if kind == "cycle":
        _put(db_path, "plutus_last_cycle", today.isoformat())
    audit.record(db_path, user, f"replenish.{kind}", f"order #{list_id}", f"{len(kept)} line(s) from the DDMRP buffers")
    return dict(base, list_id=list_id, lines=len(kept), cuts=cuts)


def run(db_path, today=None, now=None):
    """What the server does every hour: an urgent order if something is about to run out, and the
    cycle order when the cycle comes round. Mostly it does nothing, as a person would not."""
    now = now or datetime.now()
    today = today or now.date()
    if not auto_enabled(db_path):
        return {"skipped": "Plutus's automatic ordering is off", "lines": 0}
    out = {"lines": 0, "orders": []}
    if cycle_due(db_path, now):
        # The sitting: everything goes into the one order, the urgent ones included.
        cycle = place(db_path, today=today, kind="cycle")
        if cycle["list_id"]:
            out["orders"].append(("cycle", cycle["list_id"], cycle["lines"]))
        else:
            _put(db_path, "plutus_last_cycle", today.isoformat())     # nothing needed: the next sitting is a cycle on
    elif (workdays.Calendar(db_path).working(today) and now.hour >= ORDER_HOUR
          and _setting(db_path, "plutus_last_urgent") != today.isoformat()):
        urgent = place(db_path, today=today, kind="urgent")           # between sittings, only the urgent
        if urgent["list_id"]:                                          # and never twice in a day
            _put(db_path, "plutus_last_urgent", today.isoformat())
            out["orders"].append(("urgent", urgent["list_id"], urgent["lines"]))
    out["lines"] = sum(n for _k, _i, n in out["orders"])
    return out


def overview(db_path, today=None):
    today = today or date.today()
    asked, left, to_count = _build(db_path, today, "cycle")
    urgent, _l, _c = _build(db_path, today, "urgent")
    rules = charters.limits(db_path, "plutus")
    return {"auto": auto_enabled(db_path), "cycle_days": int(rules["cycle_days"]),
            "last_cycle": last_cycle(db_path).isoformat() if last_cycle(db_path) else None,
            "next_cycle": next_cycle(db_path, today).isoformat(),
            "to_order": asked, "urgent": urgent, "left_alone": left, "to_count": to_count}
