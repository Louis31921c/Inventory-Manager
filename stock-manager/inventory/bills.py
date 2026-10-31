"""Supplier bills: what each delivery ends up costing, and what is still owed."""
from . import db
from .models import normalize_name, parse_date

STATES = ("to pay", "paid", "disputed")

FIELDS = ("supplier", "number", "bill_date", "due_date", "currency", "net", "tax", "total",
          "site", "order_ref", "state", "paid_on", "source", "sender", "image_sha256",
          "image_path", "added_by", "note")


def _money(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace(" ", "")
    text = text.replace("€", "").replace("EUR", "").replace("eur", "")
    if "," in text and "." in text:
        text = text.replace(".", "") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def shape(bill, source="file", user=None, sender=None, sha256=None, path=None):
    """Turn what the model read into a row, filling in what it can work out itself."""
    net, tax, total = _money(bill.get("net")), _money(bill.get("tax")), _money(bill.get("total"))
    if total is None and net is not None:
        total = round(net + (tax or 0), 2)
    if net is None and total is not None and tax is not None:
        net = round(total - tax, 2)
    state = str(bill.get("state") or "to pay").lower()
    return {
        "supplier": normalize_name(bill.get("supplier")) or "?",
        "number": (str(bill.get("number")).strip() if bill.get("number") else None),
        "bill_date": parse_date(bill.get("bill_date")),
        "due_date": parse_date(bill.get("due_date")),
        "currency": (str(bill.get("currency") or "EUR").strip().upper() or "EUR")[:8],
        "net": net, "tax": tax, "total": total,
        "site": normalize_name(bill.get("site")),
        "order_ref": (str(bill.get("order_ref")).strip() if bill.get("order_ref") else None),
        "state": state if state in STATES else "to pay",
        "paid_on": parse_date(bill.get("paid_on")),
        "source": source, "sender": sender, "image_sha256": sha256, "image_path": path,
        "added_by": user, "note": bill.get("note") or None,
    }


def problems(row):
    out = []
    if not row.get("supplier") or row["supplier"] == "?":
        out.append("supplier missing")
    if row.get("bill_date") is None:
        out.append("bill date missing or unreadable")
    if row.get("total") is None:
        out.append("total missing")
    return out


def find_by_image(db_path, sha256):
    rows = db.read(db_path, "SELECT bill_id FROM bills WHERE image_sha256 = ?", [sha256])
    return rows[0][0] if rows else None


def find_same(db_path, supplier, number):
    """The same invoice number from the same supplier is the same bill."""
    if not number:
        return None
    rows = db.read(db_path, "SELECT bill_id FROM bills WHERE supplier = ? AND number = ?",
                   [supplier, number])
    return rows[0][0] if rows else None


def save(db_path, row):
    names = ", ".join(FIELDS)
    marks = ", ".join("?" for _ in FIELDS)
    answer = db.write(db_path, f"INSERT INTO bills ({names}) VALUES ({marks}) RETURNING bill_id",
                      [row.get(name) for name in FIELDS])
    return answer[0]


def _row(values):
    (bill_id, supplier, number, bill_date, month, due, currency, net, tax, total, site,
     order_ref, state, paid_on, source, sender, has_image, added_at, added_by, note) = values
    return {"bill_id": bill_id, "supplier": supplier, "number": number,
            "bill_date": bill_date.isoformat() if bill_date else None, "month": month,
            "due_date": due.isoformat() if due else None, "currency": currency,
            "net": net, "tax": tax, "total": total, "site": site, "order_ref": order_ref,
            "state": state, "paid_on": paid_on.isoformat() if paid_on else None,
            "source": source, "sender": sender, "photo": bool(has_image),
            "added_at": added_at.isoformat(timespec="minutes") if added_at else None,
            "added_by": added_by, "note": note}


SELECT = """SELECT bill_id, supplier, number, bill_date, month, due_date, currency, net, tax,
                   total, site, order_ref, state, paid_on, source, sender, image_path,
                   added_at, added_by, note
            FROM bills"""


def recent(db_path, limit=500):
    rows = db.read(db_path, SELECT + " ORDER BY bill_date DESC, bill_id DESC LIMIT ?", [limit])
    return [_row(row) for row in rows]


def one(db_path, bill_id):
    rows = db.read(db_path, SELECT + " WHERE bill_id = ?", [bill_id])
    return _row(rows[0]) if rows else None


def update(db_path, bill_id, changes):
    """Only the fields a person edits on the screen, never the whole row."""
    allowed = ("supplier", "number", "bill_date", "due_date", "net", "tax", "total", "site",
               "order_ref", "state", "paid_on", "note")
    sets, args = [], []
    for name, value in changes.items():
        if name not in allowed:
            continue
        if name in ("bill_date", "due_date", "paid_on"):
            value = parse_date(value)
        elif name in ("net", "tax", "total"):
            value = _money(value)
        elif name in ("supplier", "site"):
            value = normalize_name(value)
        elif name == "state":
            value = str(value).lower()
            if value not in STATES:
                continue
        sets.append(f"{name} = ?")
        args.append(value)
    if not sets:
        return False
    if "state = ?" in sets and changes.get("state") == "paid" and "paid_on" not in changes:
        sets.append("paid_on = coalesce(paid_on, current_date)")
    db.write(db_path, f"UPDATE bills SET {', '.join(sets)} WHERE bill_id = ?", args + [bill_id])
    return True


def drop(db_path, bill_id):
    before = db.read(db_path, "SELECT count(*) FROM bills WHERE bill_id = ?", [bill_id])[0][0]
    if not before:
        return False
    db.write(db_path, "DELETE FROM bills WHERE bill_id = ?", [bill_id])
    return True


def by_month(db_path, months=12):
    rows = db.read(db_path, """
            SELECT month, count(*), sum(coalesce(total, 0)), sum(coalesce(net, 0)),
                   sum(CASE WHEN state = 'to pay' THEN coalesce(total, 0) ELSE 0 END)
            FROM bills
            GROUP BY month
            ORDER BY month DESC
            LIMIT ?
            """, [months])
    return [{"month": month, "bills": count, "total": total, "net": net, "owed": owed}
            for month, count, total, net, owed in rows]


def summary(db_path):
    row = db.read(db_path, """
            SELECT count(*), sum(coalesce(total, 0)),
                   sum(CASE WHEN state = 'to pay' THEN coalesce(total, 0) ELSE 0 END),
                   sum(CASE WHEN state = 'to pay' AND due_date IS NOT NULL
                                 AND due_date < current_date THEN coalesce(total, 0)
                            ELSE 0 END),
                   count(DISTINCT supplier)
            FROM bills
            """)[0]
    count, total, owed, late, suppliers = row
    months = by_month(db_path, months=12)
    paid_months = [m for m in months if m["total"]]
    average = round(sum(m["total"] for m in paid_months) / len(paid_months), 2) if paid_months else 0
    return {"bills": count or 0, "total": total or 0, "owed": owed or 0, "late": late or 0,
            "suppliers": suppliers or 0, "per_month": average,
            "this_month": months[0] if months else None}
