"""How long each supplier really takes, learned from the delivery notes.

A note carries the day it was ordered and the day it arrived. The gap, by supplier and by
article, is what the planners should be using instead of one number typed into a box. The
middle gap says what to expect and the 80th percentile says what to plan around: one
delivery in five is slower than that. Few samples say little, so nothing is learned from
fewer than MIN_SAMPLES, and the caller falls back to the number it was given.
"""

from . import db

MIN_SAMPLES = 3


def _stats(group_by, extra=""):
    return f"""
            SELECT {group_by}, count(*), median(delivery_date - order_date),
                   quantile_cont(delivery_date - order_date, 0.8),
                   min(delivery_date - order_date), max(delivery_date - order_date)
            FROM inventory_lines
            WHERE order_date IS NOT NULL AND delivery_date IS NOT NULL
              AND delivery_date >= order_date AND NOT coalesce(backorder, FALSE) {extra}
            GROUP BY {group_by}"""


def _row(count, middle, slow, fastest, slowest):
    return {"n": int(count), "days": round(float(middle), 1), "plan_days": round(float(slow), 1),
            "fastest": int(fastest), "slowest": int(slowest)}


def per_supplier(db_path):
    return {supplier: _row(*rest) for supplier, *rest in
            db.read(db_path, _stats("supplier", "AND supplier IS NOT NULL"))}


def per_article(db_path):
    return {article: _row(*rest) for article, *rest in db.read(db_path, _stats("article"))}


def learned(db_path):
    """Article -> days to plan around, for the articles with enough deliveries to say."""
    return {article: row["plan_days"] for article, row in per_article(db_path).items()
            if row["n"] >= MIN_SAMPLES}


def for_supplier(db_path, supplier, default=None):
    """What to expect from a supplier, or `default` when it has too few deliveries."""
    row = per_supplier(db_path).get(supplier)
    return row["plan_days"] if row and row["n"] >= MIN_SAMPLES else default


def overview(db_path):
    suppliers = per_supplier(db_path)
    articles = per_article(db_path)
    return {
        "min_samples": MIN_SAMPLES,
        "suppliers": sorted(({"supplier": name, **row} for name, row in suppliers.items()),
                            key=lambda row: -row["plan_days"]),
        "articles": sorted(({"article": name, **row} for name, row in articles.items()
                            if row["n"] >= MIN_SAMPLES),
                           key=lambda row: (-row["plan_days"], row["article"]))[:100],
    }
