"""What an article costs, and what it usually costs.

Nothing in the paperwork carries a price per article: a bill gives a total for a month
and a delivery note gives none at all. So the price comes in with the basket — the shop
page Dots is reading has it — and every price that comes in is kept here. That history is
the only thing the gate can hold a new price up against.
"""

from statistics import median

from . import db
from .models import normalize_name

# How many sightings back the usual price looks. Older than that and the trade has moved.
RECENT = 12


def remember(db_path, article, unit_price, supplier=None, source="basket", seen_on=None):
    """Keep one price sighting. Prices are never overwritten, only added to."""
    article = normalize_name(article)
    if not article or unit_price is None:
        return False
    try:
        price = float(unit_price)
    except (TypeError, ValueError):
        return False
    if price <= 0:
        return False

    db.write(db_path, """
            INSERT INTO article_prices (article, supplier, unit_price, source, seen_on)
            VALUES (?, ?, ?, ?, coalesce(?, current_date))
            """, (article, normalize_name(supplier), price, source, seen_on))
    return True


def sightings(db_path, article, limit=RECENT):
    article = normalize_name(article)
    if not article:
        return []
    return [float(price) for (price,) in db.read(db_path, """
            SELECT unit_price FROM article_prices
            WHERE article = ? ORDER BY seen_on DESC, price_id DESC LIMIT ?
            """, (article, limit))]


def usual(db_path, article):
    """The middle of the recent prices, or None when the article has no history.

    The middle and not the mean: one typo of a price pulls a mean a long way, and the
    whole point of the number is to say whether a new price is out of the ordinary.
    """
    seen = sightings(db_path, article)
    return median(seen) if seen else None


def suppliers(db_path, article):
    """Every supplier this article has ever been bought from, prices and paperwork alike."""
    article = normalize_name(article)
    if not article:
        return set()
    rows = db.read(db_path, """
            SELECT DISTINCT supplier FROM article_prices WHERE article = ? AND supplier IS NOT NULL
            UNION
            SELECT DISTINCT l.supplier FROM inventory_lines l
            WHERE l.article = ? AND l.supplier IS NOT NULL
            """, (article, article))
    return {row[0] for row in rows if row[0]}


def line_total(line):
    """What one basket line comes to, or None when it carries no price."""
    price = line.get("unit_price")
    if price is None:
        return None
    return float(price) * float(line.get("quantity") or 0)


def basket_total(lines):
    """(what the basket comes to, how many lines could not be priced)."""
    total = 0.0
    missing = 0
    for line in lines:
        amount = line_total(line)
        if amount is None:
            missing += 1
        else:
            total += amount
    return total, missing
