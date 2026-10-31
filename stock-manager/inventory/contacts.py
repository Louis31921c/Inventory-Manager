"""Who an outbound order goes to: the company, the person to ring, and the terms."""
from . import db
from .models import normalize_name

INCOTERMS = {
    "EXW": "Ex works, they collect",
    "FCA": "Free carrier",
    "CPT": "Carriage paid to",
    "CIP": "Carriage and insurance paid to",
    "DAP": "Delivered at place",
    "DPU": "Delivered at place unloaded",
    "DDP": "Delivered duty paid",
}

FIELDS = ("company", "person", "phone", "email", "address", "incoterm", "site", "note")


def terms():
    return [{"id": key, "label": f"{key} — {what}"} for key, what in INCOTERMS.items()]


def _shape(body):
    incoterm = str(body.get("incoterm") or "").strip().upper()
    return {
        "company": normalize_name(body.get("company")) or "",
        "person": (body.get("person") or "").strip() or None,
        "phone": (body.get("phone") or "").strip() or None,
        "email": (body.get("email") or "").strip() or None,
        "address": (body.get("address") or "").strip() or None,
        "incoterm": incoterm if incoterm in INCOTERMS else None,
        "site": normalize_name(body.get("site")) or None,
        "note": (body.get("note") or "").strip() or None,
    }


def save(db_path, body, user=None):
    row = _shape(body)
    if not row["company"]:
        raise ValueError("A contact needs a company name.")
    names = ", ".join(FIELDS)
    marks = ", ".join("?" for _ in FIELDS)
    answer = db.write(db_path, f"""
            INSERT INTO contacts ({names}, added_by) VALUES ({marks}, ?) RETURNING contact_id
            """, [row[name] for name in FIELDS] + [user])
    return answer[0]


def _row(values):
    (contact_id, company, person, phone, email, address, incoterm, site, note,
     added_at, added_by) = values
    return {"contact_id": contact_id, "company": company, "person": person, "phone": phone,
            "email": email, "address": address, "incoterm": incoterm, "site": site, "note": note,
            "added_at": added_at.isoformat(timespec="minutes") if added_at else None,
            "added_by": added_by}


SELECT = """SELECT contact_id, company, person, phone, email, address, incoterm, site, note,
                   added_at, added_by
            FROM contacts"""


def all_of(db_path):
    return [_row(row) for row in db.read(db_path, SELECT + " ORDER BY company, person")]


def one(db_path, contact_id):
    rows = db.read(db_path, SELECT + " WHERE contact_id = ?", [contact_id])
    return _row(rows[0]) if rows else None


def for_site(db_path, site):
    """The contact a site usually ships to, when there is one."""
    site = normalize_name(site)
    if not site:
        return None
    rows = db.read(db_path, SELECT + " WHERE site = ? ORDER BY contact_id LIMIT 1", [site])
    return _row(rows[0]) if rows else None


def update(db_path, contact_id, body):
    row = _shape({**(one(db_path, contact_id) or {}), **body})
    if not row["company"]:
        raise ValueError("A contact needs a company name.")
    db.write(db_path, f"UPDATE contacts SET {', '.join(f'{name} = ?' for name in FIELDS)} "
                      f"WHERE contact_id = ?", [row[name] for name in FIELDS] + [contact_id])
    return True


def drop(db_path, contact_id):
    if not db.read(db_path, "SELECT count(*) FROM contacts WHERE contact_id = ?",
                   [contact_id])[0][0]:
        return False
    db.write(db_path, "UPDATE purchase_orders SET contact_id = NULL WHERE contact_id = ?",
             [contact_id])
    db.write(db_path, "DELETE FROM contacts WHERE contact_id = ?", [contact_id])
    return True


def put_on_order(db_path, list_id, contact_id=None, incoterm=None, ship_by=None):
    """Hang a contact and the terms on an order that is already stored."""
    from .models import parse_date

    sets, args = [], []
    if contact_id is not None:
        sets.append("contact_id = ?")
        args.append(int(contact_id) or None)
    if incoterm is not None:
        word = str(incoterm).strip().upper()
        sets.append("incoterm = ?")
        args.append(word if word in INCOTERMS else None)
    if ship_by is not None:
        sets.append("ship_by = ?")
        args.append(parse_date(ship_by))
    if not sets:
        return False
    db.write(db_path, f"UPDATE purchase_orders SET {', '.join(sets)} WHERE list_id = ?",
             args + [list_id])
    return True
