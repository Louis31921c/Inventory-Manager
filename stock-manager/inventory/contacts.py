"""Who an outbound order goes to: the company, the person to ring, and the terms."""
import csv
import os
from pathlib import Path

from . import config, db
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


# ---- the contact file on the disk -----------------------------------------

FILE_NAME = "supplier-contacts.csv"
COLUMNS = {"company": "company", "societe": "company", "name": "company",
           "person": "person", "contact": "person", "nom": "person",
           "phone": "phone", "tel": "phone", "telephone": "phone",
           "email": "email", "mail": "email", "e-mail": "email",
           "address": "address", "adresse": "address",
           "incoterm": "incoterm", "terms": "incoterm",
           "site": "site", "chantier": "site",
           "note": "note", "remarque": "note"}


def candidates():
    """Where a supplier contact file is looked for, best place first."""
    told = os.environ.get("STOCK_CONTACTS_FILE")
    here = Path(__file__).resolve().parent.parent.parent
    places = [Path(told)] if told else []
    places += [here / FILE_NAME, here.parent / FILE_NAME,
               config.default_data_dir() / FILE_NAME]
    home = Path.home()
    for folder in ("Documents", "Desktop", "Downloads"):
        places.append(home / folder / FILE_NAME)
    # Installed in a venv the package sits far from the file, so look a little way down
    # the usual folders too rather than make somebody type a path.
    for folder in ("Documents", "Desktop"):
        for depth in ("*", "*/*"):
            places += sorted((home / folder).glob(f"{depth}/{FILE_NAME}"))
    return places


def find_file():
    for place in candidates():
        if place.is_file():
            return place
    return None


def read_file(path=None):
    """The file as rows this module understands. Unknown columns are left alone."""
    path = Path(path) if path else find_file()
    if path is None:
        raise FileNotFoundError(f"No {FILE_NAME} found. Looked in: "
                                + ", ".join(str(one.parent) for one in candidates()))
    with open(path, newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = []
        for raw in csv.DictReader(handle, dialect=dialect):
            row = {}
            for key, value in raw.items():
                named = COLUMNS.get(str(key or "").strip().lower())
                if named and value:
                    row[named] = value.strip()
            if row.get("company"):
                rows.append(row)
    return path, rows


def load_file(db_path, path=None, user=None):
    """Put the file in the contact book: new companies added, known ones updated."""
    where, rows = read_file(path)
    known = {(one["company"], one["person"] or ""): one for one in all_of(db_path)}
    added = changed = same = 0
    for row in rows:
        shaped = _shape(row)
        key = (shaped["company"], shaped["person"] or "")
        held = known.get(key)
        if held is None:
            contact_id = save(db_path, row, user=user)
            known[key] = one(db_path, contact_id)
            added += 1
        elif all(shaped[name] == held.get(name) for name in row if name in FIELDS):
            same += 1                            # nothing on the line has moved
        else:
            update(db_path, held["contact_id"], row)
            known[key] = one(db_path, held["contact_id"])
            changed += 1
    return {"file": str(where), "read": len(rows), "added": added, "updated": changed,
            "same": same}


def look_up(name=None, company=None, path=None, limit=50):
    """Ask the file itself, the way a query asks a table: two words, the lines that match."""
    where, rows = read_file(path)
    name = str(name or "").strip().lower()
    company = str(company or "").strip().lower()
    found = [row for row in rows
             if (not name or name in (row.get("person") or "").lower())
             and (not company or company in (row.get("company") or "").lower())]
    return {"file": str(where), "columns": list(FIELDS), "found": len(found),
            "rows": [{field: row.get(field) for field in FIELDS} for row in found[:limit]],
            "limit": limit}
