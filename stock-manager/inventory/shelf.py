"""The In stock table: per material, what is on the shelf, what it is worth, how long it lasts.

Built from figures the rest of the program already works out, so the screens agree:
the shelf from stock.py (a hand count wins), the daily use from the DDMRP buffers, what is
on its way from the orders sent and confirmed on Orders to come, and the price from the
last price seen for the material.
"""

from datetime import date

from . import dashboard, db, ddmrp, stock

# How far back "weekly movements" looks: eight weeks, so one odd week does not decide it.
MOVEMENT_WEEKS = 8


def _last_prices(db_path):
    return {article: float(price) for article, price in db.read(db_path, """
            SELECT article, arg_max(unit_price, seen_on)
            FROM article_prices
            WHERE unit_price IS NOT NULL
            GROUP BY article""") if price is not None}


def _weekly(db_path):
    """Material -> (in per week, out per week), averaged over the last MOVEMENT_WEEKS weeks."""
    days = 7 * MOVEMENT_WEEKS
    came = {article: float(total or 0) for article, total in db.read(db_path, f"""
            SELECT article, sum(quantity) FROM inventory_lines
            WHERE quantity IS NOT NULL AND delivery_date > current_date - {days}
            GROUP BY article""")}
    went = {article: float(total or 0) for article, total in db.read(db_path, f"""
            SELECT l.article, sum(l.quantity)
            FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
            WHERE l.quantity IS NOT NULL AND upper(coalesce(h.drafter, '')) <> 'PLUTUS'
              AND {stock.ORDER_DAY} > current_date - {days}
            GROUP BY l.article""")}
    return {article: (came.get(article, 0.0) / MOVEMENT_WEEKS, went.get(article, 0.0) / MOVEMENT_WEEKS)
            for article in set(came) | set(went)}


def _on_its_way(db_path, today=None):
    """Material -> (quantity sent or confirmed and not yet in, the earliest day expected)."""
    out = {}
    for row in dashboard.planned_orders(db_path, today)["rows"]:
        if row["status"] not in ("sent", "confirmed"):
            continue
        quantity, first = out.get(row["article"], (0.0, None))
        expected = row.get("expected_on") or None
        first = min(filter(None, (first, expected)), default=None)
        out[row["article"]] = (quantity + float(row["quantity"] or 0), first)
    return out


def summary(db_path, today=None):
    """stock.summary, with each row carrying the In stock table's figures as well."""
    base = stock.summary(db_path)
    hand = stock.on_hand_map(db_path)
    daily = {row["article"]: row["adu"] for row in ddmrp.buffers(db_path)[0]}
    prices = _last_prices(db_path)
    weekly = _weekly(db_path)
    coming = _on_its_way(db_path, today)

    total_value = 0.0
    for row in base["rows"]:
        article = row["article"]
        sku = round(float(hand.get(article, row["on_hand"])), 2)
        price = prices.get(article)
        value = round(max(sku, 0.0) * price, 2) if price is not None else None
        total_value += value or 0.0
        use = daily.get(article) or 0.0
        if use > 0:
            lasts = round(max(sku, 0.0) / use, 1)
        else:
            lasts = None
        way_qty, way_on = coming.get(article, (0.0, None))
        week_in, week_out = weekly.get(article, (0.0, 0.0))
        row.update({
            "sku": sku,
            "unit_price": round(price, 2) if price is not None else None,
            "value": value,
            "lasts_days": lasts,
            "on_way": round(way_qty, 2),
            "on_way_on": way_on if isinstance(way_on, str) or way_on is None else way_on.isoformat(),
            "week_in": round(week_in, 1),
            "week_out": round(week_out, 1),
        })
    # What runs out first comes first; a material nobody uses goes to the bottom.
    base["rows"].sort(key=lambda row: (row["lasts_days"] is None,
                                       row["lasts_days"] if row["lasts_days"] is not None else 0,
                                       row["article"]))
    base["total_value"] = round(total_value, 2)
    base["priced"] = sum(1 for row in base["rows"] if row["unit_price"] is not None)
    base["as_of"] = (today or date.today()).isoformat()
    return base
