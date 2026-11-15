"""What suppliers answered to a quote, which answer won, and the price that stays.

A quote request names what is wanted and holds no money. The answer comes back by mail or
phone and is typed in here, a price, a lead time and how long it stands, one per line. To
compare, the same request is copied to other suppliers, and the answers sit side by side
per article: price, lead time, and what the wanted quantity would come to. Awarding one
makes it the agreement for that article: it ends the one before, goes into the price
history (so the gate's "usual price" and the bill check know it), and from then on it is
the supplier the order drafts and Plutus use for that article.
"""

from datetime import date

from . import db, prices, quotes
from .models import normalize_name, parse_date


def set_offer(db_path, quote_id, line_no, unit_price, lead_days=None, valid_until=None,
              note=None, user=None):
    quote = quotes.one(db_path, quote_id)
    if quote is None:
        raise ValueError(f"No quote request #{quote_id}.")
    if not any(row["line_no"] == line_no for row in quote["rows"]):
        raise ValueError(f"Quote #{quote_id} has no line {line_no}.")
    try:
        unit_price = float(unit_price)
    except (TypeError, ValueError):
        raise ValueError("The price is not a number.") from None
    if unit_price <= 0:
        raise ValueError("A price is more than zero.")
    db.write(db_path, """
            INSERT INTO quote_offers (quote_id, line_no, supplier, unit_price, lead_days,
                                      valid_until, note, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (quote_id, line_no) DO UPDATE SET supplier = excluded.supplier,
                unit_price = excluded.unit_price, lead_days = excluded.lead_days,
                valid_until = excluded.valid_until, note = excluded.note,
                received_on = CAST(now() AS DATE)
            RETURNING offer_id""",
             [quote_id, line_no, quote["supplier"], unit_price,
              int(lead_days) if lead_days not in (None, "") else None,
              parse_date(valid_until), note or None, user])
    priced = db.read(db_path, "SELECT count(*) FROM quote_offers WHERE quote_id = ?", [quote_id])[0][0]
    if priced >= len(quote["rows"]) and quote["state"] in ("open", "sent"):
        quotes.update(db_path, quote_id, {"state": "answered"})
    return offers(db_path, quote_id)


def from_reply(db_path, quote_id, answers, user=None):
    """File the prices a supplier's reply gave, line by line, as offers on the quote request.
    A line with no usable price is skipped. Nothing is awarded: that stays a person's, or a policy's.
    Gives back how many were filed."""
    filed = 0
    for answer in answers:
        try:
            set_offer(db_path, quote_id, answer["line_no"], answer.get("unit_price"),
                      answer.get("lead_days"), answer.get("valid_until"),
                      "from the supplier's reply", user)
            filed += 1
        except (ValueError, KeyError):
            continue
    return filed


def offers(db_path, quote_id):
    quote = quotes.one(db_path, quote_id)
    if quote is None:
        return []
    got = {line_no: (price, lead, valid, note) for line_no, price, lead, valid, note in db.read(
        db_path, "SELECT line_no, unit_price, lead_days, valid_until, note FROM quote_offers "
                 "WHERE quote_id = ?", [quote_id])}
    out = []
    for row in quote["rows"]:
        price, lead, valid, note = got.get(row["line_no"], (None, None, None, None))
        out.append({"line_no": row["line_no"], "article": row["article"],
                    "quantity": row["quantity"], "unit": row["unit"],
                    "unit_price": price, "lead_days": lead,
                    "valid_until": valid.isoformat() if valid else None, "note": note})
    return out


def ask_others(db_path, quote_id, suppliers, user=None):
    """Copy a request to other suppliers so their answers can be put side by side."""
    quote = quotes.one(db_path, quote_id)
    if quote is None:
        raise ValueError(f"No quote request #{quote_id}.")
    made = []
    for name in suppliers:
        supplier = normalize_name(name)
        if not supplier or supplier == quote["supplier"]:
            continue
        head = {"supplier": supplier, "site": quote["site"], "work_item": quote["work_item"],
                "needed_by": quote["needed_by"], "note": quote["note"]}
        made.append(quotes.save(db_path, head, [
            {"article": r["article"], "quantity": r["quantity"], "unit": r["unit"]}
            for r in quote["rows"]], user))
    return made


def compare(db_path, today=None):
    """Per article, every answer received, the cheapest and the fastest marked."""
    today = today or date.today()
    rows = db.read(db_path, """
            SELECT l.article, o.quote_id, o.line_no, q.supplier, o.unit_price, o.lead_days,
                   o.valid_until, l.quantity, l.unit, q.state
            FROM quote_offers o
            JOIN quote_lines l ON l.quote_id = o.quote_id AND l.line_no = o.line_no
            JOIN quotes q ON q.quote_id = o.quote_id
            ORDER BY l.article, o.unit_price""")
    by_article = {}
    for article, qid, line_no, supplier, price, lead, valid, qty, unit, state in rows:
        by_article.setdefault(article, []).append({
            "quote_id": qid, "line_no": line_no, "supplier": supplier, "unit_price": float(price),
            "lead_days": lead, "valid_until": valid.isoformat() if valid else None,
            "expired": bool(valid and valid < today), "quantity": qty, "unit": unit,
            "total": round(float(price) * float(qty or 0), 2), "state": state})
    out = []
    for article, offered in by_article.items():
        live = [o for o in offered if not o["expired"]]
        cheapest = min((o["unit_price"] for o in live), default=None)
        leads = [o["lead_days"] for o in live if o["lead_days"] is not None]
        fastest = min(leads) if leads else None
        for o in offered:
            o["cheapest"] = cheapest is not None and not o["expired"] and o["unit_price"] == cheapest
            o["fastest"] = fastest is not None and o["lead_days"] == fastest and not o["expired"]
        out.append({"article": article, "offers": offered,
                    "agreement": agreement_for(db_path, article, today)})
    return {"articles": out, "agreements": agreements(db_path)}


def award(db_path, quote_id, line_no, valid_to=None, user=None):
    """Make this answer the agreed price for its article."""
    quote = quotes.one(db_path, quote_id)
    offer = next((o for o in offers(db_path, quote_id)
                  if o["line_no"] == line_no and o["unit_price"] is not None), None)
    if quote is None or offer is None:
        raise ValueError("That line has no price to award.")
    supplier = quote["supplier"]
    if not supplier:
        raise ValueError("The quote request names no supplier.")
    article = normalize_name(offer["article"])
    until = parse_date(valid_to) or parse_date(offer["valid_until"])
    db.write(db_path, "UPDATE agreements SET state = 'ended', valid_to = coalesce(valid_to, CAST(now() AS DATE)) "
                      "WHERE article = ? AND state = 'active' RETURNING agreement_id", [article])
    row = db.write(db_path, """
            INSERT INTO agreements (article, supplier, unit_price, lead_days, valid_to, quote_id,
                                    created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING agreement_id""",
                   [article, supplier, offer["unit_price"], offer["lead_days"], until, quote_id, user])
    prices.remember(db_path, article, offer["unit_price"], supplier=supplier, source="quote")
    left = [o for o in offers(db_path, quote_id) if o["unit_price"] is None]
    if not left:
        awarded = db.read(db_path, "SELECT count(DISTINCT article) FROM agreements WHERE quote_id = ?",
                          [quote_id])[0][0]
        if awarded >= len(quote["rows"]):
            quotes.update(db_path, quote_id, {"state": "closed"})
    return {"agreement_id": row[0], "article": article, "supplier": supplier,
            "unit_price": offer["unit_price"]}


def agreements(db_path, active_only=True):
    where = "WHERE state = 'active'" if active_only else ""
    return [{"agreement_id": i, "article": a, "supplier": s, "unit_price": float(p),
             "lead_days": l, "valid_from": f.isoformat(),
             "valid_to": t.isoformat() if t else None, "quote_id": q, "state": st}
            for i, a, s, p, l, f, t, q, st in db.read(db_path, f"""
                SELECT agreement_id, article, supplier, unit_price, lead_days, valid_from, valid_to,
                       quote_id, state FROM agreements {where} ORDER BY article""")]


def agreement_for(db_path, article, on=None):
    """The price agreed for an article on a day, or None."""
    on = on or date.today()
    rows = db.read(db_path, """
            SELECT supplier, unit_price, lead_days, valid_to, agreement_id FROM agreements
            WHERE article = ? AND valid_from <= ?
              AND (valid_to IS NULL OR valid_to >= ?)
            ORDER BY valid_from DESC, agreement_id DESC LIMIT 1""",
                   [normalize_name(article), on, on])
    if not rows:
        return None
    supplier, price, lead, valid_to, agreement_id = rows[0]
    return {"agreement_id": agreement_id, "supplier": supplier, "unit_price": float(price),
            "lead_days": lead, "valid_to": valid_to.isoformat() if valid_to else None}


def end(db_path, agreement_id):
    return db.write(db_path, "UPDATE agreements SET state = 'ended', valid_to = CAST(now() AS DATE) "
                             "WHERE agreement_id = ? AND state = 'active' RETURNING agreement_id",
                    [agreement_id]) is not None
