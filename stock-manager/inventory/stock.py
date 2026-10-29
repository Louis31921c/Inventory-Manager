"""What is on the shelf, counted from the paperwork instead of by hand.

In is a supplier delivery note (inventory_lines), out is a customer purchase order: an order is
picked and shipped within a day, so a line counts as gone once that day has passed. A hand count
in stock_counts always wins over the sum, because somebody has looked at the shelf.
"""
from . import db

SHIPPING_DAYS = 1

ORDER_DAY = "coalesce(h.list_date, CAST(h.created_at AS DATE))"


def received(db_path):
    rows = db.read(db_path, """
            SELECT article, sum(quantity), max(delivery_date), count(*)
            FROM inventory_lines
            WHERE quantity IS NOT NULL
            GROUP BY article
            """)
    return {article: {"quantity": float(quantity or 0), "last": last, "lines": lines}
            for article, quantity, last, lines in rows}


def shipped(db_path):
    """Everything ordered a day ago or more has left; the rest is still being picked."""
    rows = db.read(db_path, f"""
            SELECT l.article,
                   sum(CASE WHEN {ORDER_DAY} IS NULL
                                 OR {ORDER_DAY} <= current_date - {SHIPPING_DAYS}
                            THEN l.quantity ELSE 0 END),
                   sum(CASE WHEN {ORDER_DAY} > current_date - {SHIPPING_DAYS}
                            THEN l.quantity ELSE 0 END),
                   max({ORDER_DAY})
            FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
            WHERE l.quantity IS NOT NULL
            GROUP BY l.article
            """)
    return {article: {"out": float(out or 0), "picking": float(picking or 0), "last": last}
            for article, out, picking, last in rows}


def counted(db_path):
    return {row["article"]: row for row in db.stock_counts(db_path)}


def on_hand_map(db_path):
    """Article -> what the shelf holds. A hand count replaces the sum for that article."""
    came, went = received(db_path), shipped(db_path)
    answer = {article: entry["quantity"] - went.get(article, {}).get("out", 0.0)
              for article, entry in came.items()}
    for article, entry in went.items():
        answer.setdefault(article, -entry["out"])
    for article, row in counted(db_path).items():
        answer[article] = float(row["quantity"])
    return answer


def rows(db_path):
    """One line per article: in, out, still being picked, and what is left."""
    came, went, hand = received(db_path), shipped(db_path), counted(db_path)
    out = []
    for article in sorted(set(came) | set(went)):
        got = came.get(article, {})
        gone = went.get(article, {})
        running = float(got.get("quantity", 0.0)) - float(gone.get("out", 0.0))
        count = hand.get(article)
        out.append({
            "article": article,
            "received": round(float(got.get("quantity", 0.0)), 2),
            "shipped": round(float(gone.get("out", 0.0)), 2),
            "picking": round(float(gone.get("picking", 0.0)), 2),
            "on_hand": round(running, 2),
            "counted": round(float(count["quantity"]), 2) if count else None,
            "counted_on": count["counted_on"] if count else None,
            "gap": round(float(count["quantity"]) - running, 2) if count else None,
            "last_in": got["last"].isoformat() if got.get("last") else None,
            "last_out": gone["last"].isoformat() if gone.get("last") else None,
            "lines": got.get("lines", 0),
        })
    return out


def summary(db_path):
    lines = rows(db_path)
    return {
        "articles": len(lines),
        "received": round(sum(row["received"] for row in lines), 2),
        "shipped": round(sum(row["shipped"] for row in lines), 2),
        "picking": round(sum(row["picking"] for row in lines), 2),
        "on_hand": round(sum(row["on_hand"] for row in lines), 2),
        "negative": len([row for row in lines if row["on_hand"] < 0]),
        "counted": len([row for row in lines if row["counted"] is not None]),
        "disagreeing": len([row for row in lines if row["gap"] not in (None, 0)]),
        "shipping_days": SHIPPING_DAYS,
        "rows": lines,
    }
