import math
from datetime import timedelta

from . import db, stock

DEFAULT_CYCLE = 7
DEFAULT_HORIZON = 4
FALLBACK_LEAD = 6
MIN_DAYS = 7

LEAD_CLASSES = ((7, "short", 0.7), (21, "medium", 0.5), (float("inf"), "long", 0.3))
VARIABILITY_CLASSES = ((0.5, "steady", 0.35), (1.0, "lumpy", 0.5),
                       (float("inf"), "erratic", 0.7))

STATES = ("quiet", "light", "usual", "spike")
SMOOTHING = 0.5


def _classify(value, table):
    for limit, name, factor in table:
        if value < limit:
            return name, factor
    return table[-1][1], table[-1][2]


def _window(db_path):
    rows = db.read(db_path, "SELECT min(delivery_date), max(delivery_date) FROM inventory_lines")
    first, last = rows[0] if rows else (None, None)
    if not first or not last:
        return None, None, MIN_DAYS
    return first, last, max((last - first).days + 1, MIN_DAYS)


def lead_times(db_path):
    """What the suppliers actually took, article first, then supplier, then everyone."""
    per_article = {article: float(days) for article, days in db.read(db_path, """
            SELECT article, median(delivery_date - order_date)
            FROM inventory_lines
            WHERE order_date IS NOT NULL AND delivery_date IS NOT NULL
            GROUP BY article
            HAVING count(*) >= 2
            """) if days is not None}
    per_supplier = {supplier: float(days) for supplier, days in db.read(db_path, """
            SELECT supplier, median(delivery_date - order_date)
            FROM inventory_lines
            WHERE order_date IS NOT NULL AND delivery_date IS NOT NULL AND supplier IS NOT NULL
            GROUP BY supplier
            """) if days is not None}
    everyone = db.read(db_path, """
            SELECT median(delivery_date - order_date)
            FROM inventory_lines
            WHERE order_date IS NOT NULL AND delivery_date IS NOT NULL
            """)
    overall = float(everyone[0][0]) if everyone and everyone[0][0] is not None else FALLBACK_LEAD
    return per_article, per_supplier, max(overall, 1.0)


def _usage(db_path):
    rows = db.read(db_path, """
            SELECT article,
                   date_trunc('week', delivery_date)::DATE AS week,
                   sum(quantity)                           AS quantity,
                   arg_max(supplier, delivery_date)        AS supplier
            FROM inventory_lines
            WHERE article IS NOT NULL AND delivery_date IS NOT NULL
            GROUP BY article, week
            ORDER BY article, week
            """)
    weeks = {}
    suppliers = {}
    for article, week, quantity, supplier in rows:
        weeks.setdefault(article, {})[week] = float(quantity or 0)
        suppliers[article] = supplier
    return weeks, suppliers


def _every_week(first, last):
    week = first - timedelta(days=first.weekday())
    end = last - timedelta(days=last.weekday())
    out = []
    while week <= end:
        out.append(week)
        week += timedelta(days=7)
    return out


def _spread(values, mean):
    if len(values) < 2:
        return 0.0
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _on_hand(db_path):
    """Counted from the notes in and the orders out, unless somebody counted the shelf."""
    return stock.on_hand_map(db_path)


def _open_demand(db_path):
    """What is still to leave the shelf.

    Only the orders that have not gone out yet: what has shipped was already taken off
    the on-hand count in stock.py, so counting it here as well would subtract it twice.
    """
    return {article: float(quantity or 0) for article, quantity in db.read(db_path, f"""
            SELECT l.article, sum(l.quantity)
            FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
            WHERE l.quantity IS NOT NULL
              AND {stock.ORDER_DAY} > current_date - {stock.SHIPPING_DAYS}
            GROUP BY l.article
            """)}


def state_of(quantity, typical):
    """Where a week sits against a week this article actually moved."""
    if quantity <= 0:
        return "quiet"
    if typical <= 0:
        return "usual"
    share = quantity / typical
    if share < 0.5:
        return "light"
    if share <= 1.75:
        return "usual"
    return "spike"


def typical_week(series):
    """The average of the weeks it moved at all: most articles sit still for weeks."""
    moving = [value for value in series if value > 0]
    return sum(moving) / len(moving) if moving else 0.0


def transition_matrix(sequences):
    """One matrix for the whole workshop: a single article never moves often enough."""
    counts = {state: {other: SMOOTHING for other in STATES} for state in STATES}
    seen = 0
    for sequence in sequences:
        for here, there in zip(sequence, sequence[1:]):
            counts[here][there] += 1
            seen += 1

    matrix = {}
    for state in STATES:
        total = sum(counts[state].values())
        matrix[state] = {other: counts[state][other] / total for other in STATES}
    return matrix, seen


def steady_state(matrix, rounds=200):
    share = {state: 1 / len(STATES) for state in STATES}
    for _ in range(rounds):
        share = {other: sum(share[state] * matrix[state][other] for state in STATES)
                 for other in STATES}
    return share


def risk_of_spike(matrix, start, weeks):
    """The chance of at least one busy week ahead, spikes absorbing the walk."""
    here = {state: 1.0 if state == start else 0.0 for state in STATES}
    caught = here.pop("spike")
    for _ in range(max(weeks, 0)):
        moved = {state: 0.0 for state in STATES if state != "spike"}
        for state, weight in here.items():
            for other in STATES:
                if other == "spike":
                    caught += weight * matrix[state][other]
                else:
                    moved[other] += weight * matrix[state][other]
        here = moved
    return caught


def buffers(db_path, cycle=DEFAULT_CYCLE, horizon=DEFAULT_HORIZON):
    first, last, days = _window(db_path)
    weeks_by_article, suppliers = _usage(db_path)
    if not weeks_by_article:
        return [], {"matrix": {}, "steady": {}, "transitions": 0}, (first, last, days)

    calendar = _every_week(first, last)
    per_article, per_supplier, overall = lead_times(db_path)
    on_hand = _on_hand(db_path)
    open_demand = _open_demand(db_path)

    sequences = []
    rows = []
    for article, by_week in weeks_by_article.items():
        series = [by_week.get(week, 0.0) for week in calendar]
        used = sum(series)
        adu = used / days
        adu_week = adu * 7
        spread = _spread(series, used / len(series) if series else 0.0)
        cv = spread / (used / len(series)) if used else 0.0

        supplier = suppliers.get(article)
        lead = per_article.get(article) or per_supplier.get(supplier) or overall
        lead = max(float(lead), 1.0)
        lead_class, lead_factor = _classify(lead, LEAD_CLASSES)
        variability, variability_factor = _classify(cv, VARIABILITY_CLASSES)

        red_base = adu * lead * lead_factor
        red_safety = red_base * variability_factor
        red = red_base + red_safety
        yellow = adu * lead
        green = max(adu * lead * lead_factor, adu * cycle)

        top_of_red = red
        top_of_yellow = red + yellow
        top_of_green = red + yellow + green

        have = on_hand.get(article)
        asked = open_demand.get(article, 0.0)
        flow = (have if have is not None else 0.0) - asked
        share = flow / top_of_green if top_of_green > 0 else 0.0

        if have is None:
            zone = "uncounted"
        elif flow <= top_of_red:
            zone = "red"
        elif flow <= top_of_yellow:
            zone = "yellow"
        elif flow <= top_of_green:
            zone = "green"
        else:
            zone = "over"

        states = [state_of(value, typical_week(series)) for value in series]
        sequences.append(states)

        rows.append({
            "article": article,
            "supplier": supplier,
            "adu": round(adu, 3),
            "per_week": round(adu_week, 2),
            "lead_days": round(lead, 1),
            "lead_class": lead_class,
            "lead_factor": lead_factor,
            "variability": variability,
            "variability_factor": variability_factor,
            "cv": round(cv, 2),
            "red": round(red, 1),
            "red_base": round(red_base, 1),
            "red_safety": round(red_safety, 1),
            "yellow": round(yellow, 1),
            "green": round(green, 1),
            "top_of_red": round(top_of_red, 1),
            "top_of_yellow": round(top_of_yellow, 1),
            "top_of_green": round(top_of_green, 1),
            "on_hand": round(have, 1) if have is not None else None,
            "open_demand": round(asked, 1),
            "net_flow": round(flow, 1),
            "buffer_share": round(100 * share, 1),
            "zone": zone,
            "to_order": max(0, int(math.ceil(top_of_green - flow))) if adu > 0 else 0,
            "state": states[-1] if states else "quiet",
            "weeks": [round(value, 2) for value in series],
        })

    matrix, moves = transition_matrix(sequences)
    for row in rows:
        row["spike_risk"] = round(100 * risk_of_spike(matrix, row["state"], horizon), 1)

    order = {"red": 0, "yellow": 1, "uncounted": 2, "green": 3, "over": 4}
    rows.sort(key=lambda row: (order.get(row["zone"], 9), -row["spike_risk"], -row["per_week"]))
    chain = {"matrix": matrix, "steady": steady_state(matrix), "transitions": moves}
    return rows, chain, (first, last, days)


def summary(db_path, cycle=DEFAULT_CYCLE, horizon=DEFAULT_HORIZON):
    rows, chain, (first, last, days) = buffers(db_path, cycle, horizon)
    counts = {zone: len([row for row in rows if row["zone"] == zone])
              for zone in ("red", "yellow", "green", "over", "uncounted")}
    return {
        "settings": {"cycle_days": cycle, "horizon_weeks": horizon},
        "window": {"first": first.isoformat() if first else None,
                   "last": last.isoformat() if last else None,
                   "days": days, "weeks": round(days / 7, 1)},
        "counts": dict(counts, articles=len(rows)),
        "chain": {
            "states": list(STATES),
            "matrix": chain["matrix"],
            "steady": chain["steady"],
            "transitions": chain["transitions"],
        },
        "articles": rows,
        "thin": days < 56 or chain["transitions"] < 60,
    }
