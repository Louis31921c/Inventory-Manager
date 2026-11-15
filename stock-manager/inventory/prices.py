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


# A day of waiting costs this share of the price: one supplier is picked over a cheaper one
# only when it saves more than that for every extra day its deliveries have really taken.
LEAD_DAY_COST = 0.005


def offers(db_path, article):
    """Every supplier's current price for the article: its latest sighting, or an active
    agreement, whichever is lower."""
    article = normalize_name(article)
    if not article:
        return []
    rows = db.read(db_path, """
            WITH latest AS (
                SELECT supplier, unit_price,
                       row_number() OVER (PARTITION BY supplier
                                          ORDER BY seen_on DESC, price_id DESC) AS n
                FROM article_prices WHERE article = ? AND supplier IS NOT NULL
            ), offers AS (
                SELECT supplier, unit_price FROM latest WHERE n = 1
                UNION ALL
                SELECT supplier, unit_price FROM agreements
                WHERE article = ? AND state = 'active'
                  AND valid_from <= current_date
                  AND (valid_to IS NULL OR valid_to >= current_date)
            )
            SELECT supplier, min(unit_price) FROM offers GROUP BY supplier
            """, (article, article))
    return [{"supplier": s, "unit_price": float(p)} for s, p in rows]


def cheapest(db_path, article):
    """The supplier to buy the article from: the lowest price once lead time is weighed in.

    Each offer is scored as price x (1 + LEAD_DAY_COST x days), the days being what that
    supplier's deliveries have taken (the 80th percentile in the delivery notes, see
    leadtimes). A supplier with too few deliveries to say is scored at the middle of the
    others', so having no history neither helps nor hurts it. With no history at all the
    plain lowest price wins. The answer says which cheaper supplier lost, and why.
    """
    from . import leadtimes

    found = offers(db_path, article)
    if not found:
        return None
    known = {name: row["plan_days"] for name, row in leadtimes.per_supplier(db_path).items()
             if row["n"] >= leadtimes.MIN_SAMPLES}
    days = [known[o["supplier"]] for o in found if o["supplier"] in known]
    middle = median(days) if days else 0.0
    for o in found:
        o["lead_days"] = known.get(o["supplier"])
        o["lead_known"] = o["supplier"] in known
        o["score"] = o["unit_price"] * (1 + LEAD_DAY_COST * (o["lead_days"] if o["lead_known"] else middle))
    found.sort(key=lambda o: (o["score"], o["unit_price"], o["supplier"]))
    best = found[0]
    lowest = min(found, key=lambda o: (o["unit_price"], o["supplier"]))
    answer = {"supplier": best["supplier"], "unit_price": best["unit_price"],
              "lead_days": best["lead_days"], "passed_over": None}
    if lowest["supplier"] != best["supplier"]:
        answer["passed_over"] = {
            "supplier": lowest["supplier"], "unit_price": lowest["unit_price"],
            "lead_days": lowest["lead_days"],
            "why": f"{lowest['supplier']} is {best['unit_price'] - lowest['unit_price']:.2f} cheaper per unit "
                   f"but its deliveries take {lowest['lead_days'] if lowest['lead_known'] else middle:g} days "
                   f"against {best['lead_days'] if best['lead_known'] else middle:g}"}
    return answer
