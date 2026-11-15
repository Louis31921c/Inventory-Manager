"""Quote requests: what to ask a supplier to price, when a piece count is not the measure.

A quote holds no money. It says what is needed, how much of it and in what unit (linear metre,
square metre, cubic metre, kilo, tonne, litre, piece) so the supplier can answer with a price.
"""
from . import db
from .models import normalize_name, parse_date

UNITS = {
    "piece": "piece",
    "ml": "linear metre",
    "m2": "square metre",
    "m3": "cubic metre",
    "kg": "kilo",
    "t": "tonne",
    "l": "litre",
    "h": "hour",
}

DEFAULT_UNIT = "piece"
STATES = ("open", "sent", "answered", "closed")

SPELLINGS = {
    "piece": "piece", "pieces": "piece", "pc": "piece", "pcs": "piece", "u": "piece",
    "unit": "piece", "units": "piece", "p": "piece",
    "ml": "ml", "lm": "ml", "m": "ml", "metre": "ml", "meter": "ml", "metres": "ml",
    "meters": "ml", "mlin": "ml",
    "m2": "m2", "m²": "m2", "sqm": "m2", "sm": "m2",
    "m3": "m3", "m³": "m3", "cbm": "m3", "cube": "m3", "cubes": "m3",
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
    "t": "t", "ton": "t", "tons": "t", "tonne": "t", "tonnes": "t",
    "l": "l", "lt": "l", "litre": "l", "litres": "l", "liter": "l", "liters": "l",
    "h": "h", "hr": "h", "hour": "h", "hours": "h",
}


def unit_of(text):
    return SPELLINGS.get(str(text or "").strip().lower().rstrip("."), None)


def units():
    """What the screen offers, in the order it offers it."""
    return [{"id": key, "label": label} for key, label in UNITS.items()]


def parse_request(text):
    """"120 ml skirting board; 3 m3 gravel; 12 anchors" -> three lines with their unit."""
    out = []
    for piece in str(text or "").replace("\n", ";").replace(",", ";").split(";"):
        words = piece.split()
        if not words:
            continue
        quantity, unit, start = None, DEFAULT_UNIT, 0
        first = words[0].replace(",", ".")
        try:
            quantity = float(first)
            start = 1
        except ValueError:
            quantity = None
        if start == 1 and len(words) > 1:
            named = unit_of(words[1])
            if named:
                unit, start = named, 2
        article = normalize_name(" ".join(words[start:]))
        if not article:
            continue
        out.append({"article": article, "quantity": quantity, "unit": unit})
    return out


def save(db_path, head, lines, user=None):
    if not lines:
        raise ValueError("A quote with no line asks for nothing.")
    quote_id = db.write(db_path, """
            INSERT INTO quotes (supplier, site, work_item, needed_by, state, note, asked_by)
            VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING quote_id
            """, (normalize_name(head.get("supplier")) or None,
                  normalize_name(head.get("site")) or None,
                  normalize_name(head.get("work_item")) or None,
                  parse_date(head.get("needed_by")),
                  "open", head.get("note") or None, user))[0]
    for line_no, line in enumerate(lines, 1):
        article = normalize_name(line.get("article"))
        if not article:
            continue
        quantity = line.get("quantity")
        db.write(db_path, """
                INSERT INTO quote_lines (quote_id, line_no, article, quantity, unit, designation)
                VALUES (?, ?, ?, ?, ?, ?)
                """, (quote_id, line_no, article,
                      float(quantity) if quantity not in (None, "") else None,
                      unit_of(line.get("unit")) or DEFAULT_UNIT,
                      (line.get("designation") or None)))
    return quote_id


def lines(db_path, quote_id):
    rows = db.read(db_path, """
            SELECT line_no, article, quantity, unit, designation
            FROM quote_lines WHERE quote_id = ? ORDER BY line_no
            """, [quote_id])
    return [{"line_no": line_no, "article": article, "quantity": quantity,
             "unit": unit, "unit_label": UNITS.get(unit, unit), "designation": designation}
            for line_no, article, quantity, unit, designation in rows]


def _head(values):
    quote_id, supplier, site, work_item, needed_by, state, note, asked_at, asked_by, count = values
    return {"quote_id": quote_id, "supplier": supplier, "site": site, "work_item": work_item,
            "needed_by": needed_by.isoformat() if needed_by else None, "state": state,
            "note": note, "asked_at": asked_at.isoformat(timespec="minutes") if asked_at else None,
            "asked_by": asked_by, "lines": count}


SELECT = """SELECT q.quote_id, q.supplier, q.site, q.work_item, q.needed_by, q.state, q.note,
                   q.asked_at, q.asked_by, count(l.line_no)
            FROM quotes q LEFT JOIN quote_lines l USING (quote_id)"""


def recent(db_path, limit=300):
    rows = db.read(db_path, SELECT + """
            GROUP BY q.quote_id, q.supplier, q.site, q.work_item, q.needed_by, q.state, q.note,
                     q.asked_at, q.asked_by
            ORDER BY q.quote_id DESC LIMIT ?
            """, [limit])
    return [_head(row) for row in rows]


def one(db_path, quote_id):
    rows = db.read(db_path, SELECT + """
            WHERE q.quote_id = ?
            GROUP BY q.quote_id, q.supplier, q.site, q.work_item, q.needed_by, q.state, q.note,
                     q.asked_at, q.asked_by
            """, [quote_id])
    if not rows:
        return None
    head = _head(rows[0])
    head["rows"] = lines(db_path, quote_id)
    return head


def update(db_path, quote_id, changes):
    allowed = ("supplier", "site", "work_item", "needed_by", "state", "note")
    sets, args = [], []
    for name, value in changes.items():
        if name not in allowed:
            continue
        if name == "needed_by":
            value = parse_date(value)
        elif name in ("supplier", "site", "work_item"):
            value = normalize_name(value) or None
        elif name == "state":
            value = str(value).lower()
            if value not in STATES:
                continue
        sets.append(f"{name} = ?")
        args.append(value)
    if not sets:
        return False
    db.write(db_path, f"UPDATE quotes SET {', '.join(sets)} WHERE quote_id = ?",
             args + [quote_id])
    return True


def drop(db_path, quote_id):
    if not db.read(db_path, "SELECT count(*) FROM quotes WHERE quote_id = ?", [quote_id])[0][0]:
        return False
    db.write(db_path, "DELETE FROM quote_lines WHERE quote_id = ?", [quote_id])
    db.write(db_path, "DELETE FROM quotes WHERE quote_id = ?", [quote_id])
    return True


def as_text(quote):
    """The request as it goes out to the supplier: no prices, just what is wanted."""
    head = [f"Quote request #{quote['quote_id']}"]
    if quote["supplier"]:
        head.append(f"Supplier: {quote['supplier']}")
    if quote["site"]:
        head.append(f"Site: {quote['site']}")
    if quote["work_item"]:
        head.append(f"Work item: {quote['work_item']}")
    if quote["needed_by"]:
        head.append(f"Needed by: {quote['needed_by']}")
    body = [f"- {show_quantity(row)} {row['article']}"
            + (f" ({row['designation']})" if row["designation"] else "")
            for row in quote.get("rows", [])]
    tail = [quote["note"]] if quote["note"] else []
    tail.append("Please quote a price for each line.")
    return "\n".join(head + [""] + body + [""] + tail)


def show_quantity(row):
    quantity = row.get("quantity")
    if quantity is None:
        return f"? {row.get('unit') or DEFAULT_UNIT}"
    shown = int(quantity) if float(quantity).is_integer() else quantity
    unit = row.get("unit") or DEFAULT_UNIT
    return f"{shown} {unit}"


def summary(db_path):
    rows = db.read(db_path, """
            SELECT state, count(*) FROM quotes GROUP BY state
            """)
    by_state = {state: count for state, count in rows}
    return {"quotes": sum(by_state.values()), "by_state": by_state,
            "open": by_state.get("open", 0) + by_state.get("sent", 0)}
