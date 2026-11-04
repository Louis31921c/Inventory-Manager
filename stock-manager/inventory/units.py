"""How an article is bought against how it is counted.

A supplier sells washers by the box of 500 and a cable by the drum of 100 m, and wants a
whole number of boxes, at least so many. The shelf counts washers. Without the link, an
order of 1,200 washers goes out as 1,200 boxes. This keeps the pack size per article (and
per supplier when two of them sell it differently), and turns a wanted quantity into the
packs to order.
"""

import math

from . import db
from .models import normalize_name


def set_pack(db_path, article, pack_qty, supplier="", base_unit="piece", pack_label=None,
             moq_packs=1):
    article = normalize_name(article)
    if not article:
        raise ValueError("No article named.")
    try:
        pack_qty = float(pack_qty)
    except (TypeError, ValueError):
        raise ValueError("The pack size is not a number.") from None
    if pack_qty <= 0:
        raise ValueError("A pack holds more than zero.")
    moq = max(1, int(moq_packs or 1))
    db.write(db_path, """
            INSERT INTO article_units (article, supplier, base_unit, pack_qty, pack_label, moq_packs)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (article, supplier) DO UPDATE SET base_unit = excluded.base_unit,
                pack_qty = excluded.pack_qty, pack_label = excluded.pack_label,
                moq_packs = excluded.moq_packs
            RETURNING article""",
             [article, normalize_name(supplier) or "", (base_unit or "piece").strip(), pack_qty,
              (pack_label or "").strip() or None, moq])
    return article


def remove(db_path, article, supplier=""):
    return db.write(db_path, "DELETE FROM article_units WHERE article = ? AND supplier = ? "
                             "RETURNING article", [normalize_name(article),
                                                   normalize_name(supplier) or ""]) is not None


def listing(db_path):
    return [{"article": a, "supplier": s, "base_unit": u, "pack_qty": q, "pack_label": l,
             "moq_packs": m} for a, s, u, q, l, m in db.read(
                 db_path, "SELECT article, supplier, base_unit, pack_qty, pack_label, moq_packs "
                          "FROM article_units ORDER BY article, supplier")]


def pack_for(db_path, article, supplier=None):
    """The pack this supplier sells the article in, else the one anybody does, else None."""
    rows = db.read(db_path, """
            SELECT supplier, base_unit, pack_qty, pack_label, moq_packs FROM article_units
            WHERE article = ? AND supplier IN (?, '')
            ORDER BY (supplier = '') LIMIT 1""", [normalize_name(article),
                                                 normalize_name(supplier) or ""])
    if not rows:
        return None
    _s, unit, qty, label, moq = rows[0]
    return {"base_unit": unit, "pack_qty": float(qty), "pack_label": label or "pack",
            "moq_packs": int(moq)}


def to_packs(db_path, article, supplier, wanted):
    """What to order to get `wanted` of the article: whole packs, at least the minimum.
    None when nothing is known about its packs, so the caller orders the quantity as it is."""
    pack = pack_for(db_path, article, supplier)
    if pack is None:
        return None
    packs = max(pack["moq_packs"], math.ceil(float(wanted) / pack["pack_qty"] - 1e-9))
    return {**pack, "packs": packs, "base": packs * pack["pack_qty"], "wanted": float(wanted),
            "extra": round(packs * pack["pack_qty"] - float(wanted), 6)}


def price_per_base(pack_price, pack_qty):
    """A price quoted per pack, as the price of one counted unit."""
    return float(pack_price) / float(pack_qty)
