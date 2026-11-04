"""How much of a logistics manager's job the software does without a person: one number.

The job is split into eight parts, each worth a share of 100. For each part the app measures,
from what it has recorded, how much of the work got done with no human step. A part with
nothing to measure yet scores nothing and says so, and a part the software cannot do (it does
not place an order, it does not pay) is capped at what it can: sending a drafted mail still
takes a person pressing send. So the figure starts low and rises only as the software really
takes work over, and every line shows what it was worked out from.

  receiving     notes stored by the mailbox with no person, of every note stored (30 days)
  reading       share of what the model read that nobody had to correct (30 days)
  stock         counts that found the shelf as the books said, within one unit (90 days)
  planning      articles in use that are either fine or already covered by a planned order
  ordering      supplier mails: half credit for one sent unedited, a quarter if it was edited
  follow-up     orders confirmed by a filed reply, of orders sent (60 days)
  bills         bills that matched their deliveries, of bills received (60 days)
  deliveries    dispatches that reached a signature: recorded by a person, so a quarter credit
"""

from datetime import date

from . import dashboard, db, matching, stockctl

WEIGHTS = (("receiving", 15), ("reading", 10), ("stock", 15), ("planning", 20),
           ("ordering", 15), ("follow-up", 10), ("bills", 10), ("deliveries", 5))


def _ratio(done, total):
    return None if not total else max(0.0, min(1.0, done / total))


def _receiving(db_path):
    total, auto = db.read(db_path, """
            SELECT count(*), count(*) FILTER (WHERE confirmed_by = 'MAIL') FROM notes
            WHERE confirmed_at >= now() - INTERVAL 30 DAY""")[0]
    return _ratio(auto, total), total, "notes the mailbox stored with no person / all notes, 30 days"


def _reading(db_path):
    fields, wrong = db.read(db_path, """
            SELECT coalesce(sum(fields), 0), coalesce(sum(corrected), 0) FROM readings
            WHERE measured_at >= now() - INTERVAL 30 DAY""")[0]
    return _ratio(fields - wrong, fields), int(fields), "fields read that nobody corrected, 30 days"


def _stock(db_path):
    total, right = db.read(db_path, """
            SELECT count(*), count(*) FILTER (WHERE abs(quantity) <= 1) FROM stock_moves
            WHERE kind = 'count' AND created_at >= now() - INTERVAL 90 DAY""")[0]
    return _ratio(right, total), total, "counts within one unit of the books / counts, 90 days"


def _planning(db_path, today):
    from . import forecast

    plan, _span = forecast.articles(db_path)
    used = [row for row in plan if row["per_day"] > 0 and row["on_hand"] is not None]
    if not used:
        return None, 0, "articles in use that are fine or already covered"
    short = {r["article"]: r for r in dashboard.shortages(db_path, today)}
    covered = 0
    for row in used:
        item = short.get(row["article"])
        if item is None or item["cause"] in ("being handled", "waiting in the 15-day queue",
                                             "order drafted, not sent", "held for approval"):
            covered += 1
    return _ratio(covered, len(used)), len(used), "articles in use that are fine or already covered"


def _ordering(db_path):
    rows = db.read(db_path, """
            SELECT state, body IS NOT DISTINCT FROM orig_body FROM supplier_mail
            WHERE direction = 'out' AND created_at >= now() - INTERVAL 30 DAY""")
    credit = sum((0.5 if same else 0.25) for state, same in rows if state in ("sent", "confirmed", "closed"))
    return _ratio(credit, len(rows)), len(rows), "mails sent unedited count half, edited a quarter, unsent nothing"


def _follow_up(db_path):
    total, filed = db.read(db_path, """
            SELECT count(*), count(*) FILTER (WHERE confirmed_at IS NOT NULL) FROM supplier_mail
            WHERE direction = 'out' AND kind IN ('order', 'quote') AND state IN ('sent', 'confirmed', 'closed')
              AND sent_at >= now() - INTERVAL 60 DAY""")[0]
    return _ratio(filed, total), total, "orders and quotes answered by a reply the mailbox filed / sent, 60 days"


def _bills(db_path, today):
    rows = [b for b in matching.bills(db_path, today)["bills"] if b["bill_date"] >= (
        date.fromordinal(today.toordinal() - 60)).isoformat()]
    return (_ratio(sum(1 for b in rows if b["verdict"] == "matched"), len(rows)), len(rows),
            "bills that matched their deliveries / bills, 60 days")


def _deliveries(db_path):
    total, signed = db.read(db_path, """
            SELECT count(*), count(*) FILTER (WHERE state IN ('delivered', 'part delivered'))
            FROM dispatches WHERE created_at >= now() - INTERVAL 60 DAY""")[0]
    return _ratio(signed * 0.25, total), total, "deliveries with a signature, a person records them: quarter credit"


def measure(db_path, today=None):
    today = today or date.today()
    got = {"receiving": _receiving(db_path), "reading": _reading(db_path),
           "stock": _stock(db_path), "planning": _planning(db_path, today),
           "ordering": _ordering(db_path), "follow-up": _follow_up(db_path),
           "bills": _bills(db_path, today), "deliveries": _deliveries(db_path)}
    parts, score, measured = [], 0.0, 0
    for name, weight in WEIGHTS:
        rate, n, basis = got[name]
        points = weight * (rate or 0.0)
        score += points
        measured += weight if rate is not None else 0
        parts.append({"part": name, "weight": weight, "rate": None if rate is None else round(rate, 4),
                      "points": round(points, 2), "samples": int(n), "basis": basis})
    return {"score": round(score, 2), "measured_weight": measured, "parts": parts}
