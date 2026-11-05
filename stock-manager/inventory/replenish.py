"""Plutus orders from the DDMRP buffers, by itself.

The buffers already say what is short: an article whose net flow has fallen into its yellow or
red zone is to be replenished up to the top of its green. That number is the order, and this
turns it into one: every hour (and on demand) the articles in yellow or red that nothing is
already on the way for are written as one purchase order, drafted by PLUTUS, in whole packs
where a pack is known. It is a list, not a purchase. The order mails to the suppliers come from
it, and the policies decide whether those go without a person.

Nothing is invented. The quantity is the buffer's, never the model's, and the charter's limits
still apply to the order: the most lines, the most euros, no article whose baskets were refused
twice. An article that has an order, a draft, a queued line or a delivery still owed is left
alone, so a slow supplier is not ordered from every hour.
"""

from datetime import date, timedelta

from . import audit, charters, db, ddmrp, feedback, units
from .models import normalize_name

ZONES = ("red", "yellow")
MIN_COVER_DAYS = 3            # how long an order of ours counts as on the way, at the least


def auto_enabled(db_path):
    """On unless somebody has switched it off."""
    rows = db.read(db_path, "SELECT value FROM policy_settings WHERE key = 'plutus_auto'")
    return not rows or rows[0][0] != "0"


def set_auto(db_path, on, user=None):
    db.write(db_path, "INSERT INTO policy_settings (key, value) VALUES ('plutus_auto', ?) "
                      "ON CONFLICT (key) DO UPDATE SET value = excluded.value RETURNING key",
             ["1" if on else "0"])
    audit.record(db_path, user or "PLUTUS", "replenish.auto", "plutus", "on" if on else "off")


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


def proposals(db_path, today=None):
    """(to order, left alone with the reason) from the buffers, red before yellow."""
    today = today or date.today()
    rows, _chain, _span = ddmrp.buffers(db_path)
    order, left = [], []
    for row in rows:
        if row["zone"] not in ZONES or row["to_order"] <= 0:
            continue
        article = row["article"]
        why = covered_by(db_path, article, row["lead_days"], today)
        if why:
            left.append({"article": article, "zone": row["zone"], "why": why})
            continue
        packed = units.to_packs(db_path, article, row["supplier"], row["to_order"])
        quantity = float(packed["base"]) if packed else float(row["to_order"])
        order.append({
            "article": article, "supplier": row["supplier"], "zone": row["zone"],
            "quantity": quantity, "buffer_asks": float(row["to_order"]),
            "packs": packed["packs"] if packed else None,
            "pack_label": packed["pack_label"] if packed else None,
            "net_flow": row["net_flow"], "top_of_yellow": row["top_of_yellow"],
            "top_of_green": row["top_of_green"], "lead_days": row["lead_days"],
            "buffer_share": row["buffer_share"],
            "reason": (f"{row['zone']} zone: net flow {row['net_flow']:g} is under the top of yellow "
                       f"{row['top_of_yellow']:g}; the buffer asks {row['to_order']:g} to reach "
                       f"{row['top_of_green']:g}")})
    order.sort(key=lambda r: (r["zone"] != "red", r["buffer_share"], r["article"]))
    return order, left


def place(db_path, user="PLUTUS", today=None):
    """Write the order the buffers ask for. Gives back what was written and what was not."""
    today = today or date.today()
    asked, left = proposals(db_path, today)
    if not asked:
        return {"list_id": None, "lines": 0, "cuts": [], "left": left}
    # The charter has the last word on the draft, the same as on one the model wrote.
    rows = [{"article": r["article"], "suggested": r["quantity"], "supplier": r["supplier"]} for r in asked]
    lines = [{"article": r["article"], "quantity": r["quantity"], "supplier": r["supplier"],
              "reason": r["reason"]} for r in asked]
    kept, cuts, _changed = charters.enforce_plutus(db_path, lines, rows, (), feedback.refusals(db_path))
    if not kept:
        return {"list_id": None, "lines": 0, "cuts": cuts, "left": left}
    list_id = db.save_purchase_order(db_path, {
        "list_date": today, "site": None, "work_item": None, "drafter": "PLUTUS",
        "lines": [{"article": line["article"], "designation": line["reason"],
                   "quantity": line["quantity"], "in_stock": False} for line in kept]},
                                     None, None, "PLUTUS")
    audit.record(db_path, user, "replenish.order", f"order #{list_id}",
                 f"{len(kept)} line(s) from the DDMRP buffers")
    return {"list_id": list_id, "lines": len(kept), "cuts": cuts, "left": left}


def run(db_path, today=None):
    """What the server does every hour: order what the buffers ask for, unless it is switched off."""
    if not auto_enabled(db_path):
        return {"skipped": "Plutus's automatic ordering is off", "lines": 0}
    return place(db_path, today=today)


def overview(db_path, today=None):
    asked, left = proposals(db_path, today)
    return {"auto": auto_enabled(db_path), "to_order": asked, "left_alone": left}
