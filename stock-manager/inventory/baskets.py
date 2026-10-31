"""What a group chat asks for, held until someone turns it into a purchase order."""
import re
from datetime import date

from . import db
from .models import normalize_name

LEADING = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(?:x|\*)?\s+(.+)$", re.I)
TRAILING = re.compile(r"^(.+?)\s*(?:x|\*)\s*(\d+(?:[.,]\d+)?)$", re.I)
# "VIS TH 6X25 @ 0.11" — the price off the shop page, the only place one ever comes from.
PRICED = re.compile(r"^(.+?)\s*@\s*(\d+(?:[.,]\d+)?)\s*(?:EUR|€)?$", re.I)
# A comma between two digits is a decimal point, not the end of a line: "0,62" is one
# price, where "12 vis, 3 rondelle" is two articles.
SPLIT = re.compile(r"[\n;]+|,(?!\d)|(?<!\d),|\s+/\s+")
MAX_LINES = 60


def _number(text):
    try:
        return float(str(text).replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_items(text):
    """'12 vis th 6x25, rondelle m8 x100' -> two lines with their quantities."""
    out = []
    for piece in SPLIT.split(str(text or "")):
        piece = " ".join(piece.split())
        if not piece:
            continue
        quantity, name, price = None, piece, None
        hit = PRICED.match(piece)
        if hit:
            piece, name, price = hit.group(1).strip(), hit.group(1).strip(), _number(hit.group(2))
        hit = LEADING.match(piece)
        if hit:
            quantity, name = _number(hit.group(1)), hit.group(2)
        else:
            hit = TRAILING.match(piece)
            if hit:
                name, quantity = hit.group(1), _number(hit.group(2))
        article = normalize_name(name)
        if not article:
            continue
        out.append({"article": article, "quantity": quantity if quantity else 1.0,
                    "unit_price": price})
        if len(out) >= MAX_LINES:
            break
    return out


def add(db_path, channel, items, shop=None, site=None, work_item=None, who=None,
        supplier=None):
    for item in items:
        db.write(db_path, """
                INSERT INTO baskets (channel, shop, article, quantity, site, work_item,
                                     added_by, supplier, unit_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (str(channel), shop, item["article"], item.get("quantity"),
                      normalize_name(site), normalize_name(work_item), who,
                      normalize_name(item.get("supplier") or supplier),
                      item.get("unit_price")))
    return len(items)


def lines(db_path, channel):
    rows = db.read(db_path, """
            SELECT line_id, shop, article, quantity, site, work_item, added_by, added_at,
                   supplier, unit_price
            FROM baskets WHERE channel = ? ORDER BY line_id
            """, (str(channel),))
    return [{"line_id": line_id, "shop": shop, "article": article, "quantity": quantity,
             "site": site, "work_item": work_item, "added_by": who,
             "added_at": when.isoformat(timespec="minutes") if when else None,
             "supplier": supplier, "unit_price": price}
            for line_id, shop, article, quantity, site, work_item, who, when, supplier,
                price in rows]


def drop_line(db_path, channel, line_id):
    found = db.read(db_path, "SELECT count(*) FROM baskets WHERE channel = ? AND line_id = ?",
                    (str(channel), line_id))[0][0]
    if not found:
        return False
    db.write(db_path, "DELETE FROM baskets WHERE channel = ? AND line_id = ?",
             (str(channel), line_id))
    return True


def clear(db_path, channel):
    held = db.read(db_path, "SELECT count(*) FROM baskets WHERE channel = ?",
                   (str(channel),))[0][0]
    db.write(db_path, "DELETE FROM baskets WHERE channel = ?", (str(channel),))
    return held


def open_baskets(db_path):
    """Every channel with something waiting, newest first."""
    rows = db.read(db_path, """
            SELECT channel, count(*), max(added_at), any_value(shop), any_value(site),
                   max(added_by)
            FROM baskets GROUP BY channel ORDER BY max(added_at) DESC
            """)
    return [{"channel": channel, "lines": count,
             "last_at": when.isoformat(timespec="minutes") if when else None,
             "shop": shop, "site": site, "last_by": who,
             "items": lines(db_path, channel)}
            for channel, count, when, shop, site, who in rows]


def shops(db_path, channel):
    return sorted({row["shop"] for row in lines(db_path, channel) if row["shop"]})


def stock_of(db_path, article):
    """What the shelf says, for the chat to answer 'have we got any?'."""
    like = f"%{normalize_name(article) or ''}%"
    rows = db.read(db_path, """
            SELECT article, sum(quantity) FROM inventory_lines
            WHERE article LIKE ? GROUP BY article ORDER BY sum(quantity) DESC LIMIT 8
            """, (like,))
    return [{"article": name, "quantity": quantity} for name, quantity in rows]


def to_purchase_order(db_path, channel, who=None):
    """Turn the basket into a stored purchase order. Nothing is bought here."""
    held = lines(db_path, channel)
    if not held:
        return None
    site = next((row["site"] for row in held if row["site"]), None)
    work_item = next((row["work_item"] for row in held if row["work_item"]), None)
    shop = next((row["shop"] for row in held if row["shop"]), None)
    data = {
        "list_date": date.today(),
        "site": site,
        "work_item": work_item,
        "drafter": normalize_name(who),
        "lines": [{"article": row["article"], "quantity": row["quantity"],
                   "designation": f"asked in chat for {row['shop']}" if row["shop"] else
                                  "asked in chat",
                   "reference": None, "stock": None, "in_stock": None}
                  for row in held],
    }
    list_id = db.save_purchase_order(db_path, data, None, None, who)
    clear(db_path, channel)
    return {"list_id": list_id, "lines": held, "shop": shop, "site": site}
