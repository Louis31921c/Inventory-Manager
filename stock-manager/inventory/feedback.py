"""The loop that closes: what people correct, refuse and run out of changes what the app proposes.

Until now the numbers were worked out once and then left. A person who cuts Plutus's
quantity from 100 to 60 has said something, so has a basket turned down for a reason, an
article that ran out when the buffer said it would not, and a count that found less on the
shelf than the books. Each of those is written down here, and the first and last two move a
pair of factors kept per article:

  qty_factor     scales the quantity proposed to order. Cut a proposal and it comes back
                 smaller next time, raise it and it comes back larger.
  safety_factor  scales the safety stock in the buffers and the reorder point. An article that
                 ran out, or whose counts keep coming up short, gets more; one that has not
                 had a problem for two months drifts back toward 1.

A factor moves by half of what a correction says, and stays between fixed bounds, so one
odd afternoon cannot send an article to zero or to the moon. Nothing is hidden: the factors
and the reason for each are on the screen, and any of them can be forgotten.
"""

from datetime import date, timedelta

from . import db
from .models import normalize_name

STEP = 0.5                       # a factor moves this share of the way a correction points
QTY_BOUNDS = (0.4, 1.6)
SAFETY_BOUNDS = (0.6, 2.0)
STOCKOUT_LIFT = 1.15             # an article that ran out gets this much more safety stock
SHORT_COUNT_LIFT = 1.05          # a count that found at least SHORT_COUNT share less than the books
SHORT_COUNT = 0.05
DECAY = 0.97                     # a calm article drifts back toward 1 by this much a day, after
CALM_DAYS = 60                   # this many days without a stock-out
SAME_RUN = 7                     # one stock-out is counted once in this many days
COUNT_FRESH = 30                 # a count older than this says nothing about now
NOTICEABLE = 0.05                # an edit under this share of the proposal is not a correction


def _clamp(value, bounds):
    return max(bounds[0], min(bounds[1], value))


def _log(db_path, article, kind, proposed=None, final=None, detail=None, user=None):
    db.write(db_path, """
            INSERT INTO feedback (article, kind, proposed, final, detail, done_by)
            VALUES (?, ?, ?, ?, ?, ?) RETURNING feedback_id""",
             [article, kind, proposed, final, detail, user])


def _move(db_path, article, qty=None, safety=None, reason=None):
    """Move an article's factors, starting from where they are. None leaves one alone."""
    now = db.read(db_path, "SELECT qty_factor, safety_factor, samples FROM tuning WHERE article = ?",
                  [article])
    q, sf, n = now[0] if now else (1.0, 1.0, 0)
    q = _clamp(q * qty, QTY_BOUNDS) if qty is not None else q
    sf = _clamp(sf * safety, SAFETY_BOUNDS) if safety is not None else sf
    db.write(db_path, """
            INSERT INTO tuning (article, qty_factor, safety_factor, samples, last_reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (article) DO UPDATE SET qty_factor = excluded.qty_factor,
                safety_factor = excluded.safety_factor, samples = excluded.samples,
                last_reason = excluded.last_reason, updated_at = now()
            RETURNING article""", [article, round(q, 4), round(sf, 4), n + 1, reason])


def review_order(db_path, proposal, user=None):
    """Compare what was shown with what was saved, line by line. `proposal` is a list of
    {article, proposed, final}; a line the person removed has final 0. Gives back what moved."""
    moved = []
    for line in proposal or []:
        article = normalize_name(line.get("article"))
        try:
            proposed, final = float(line.get("proposed")), float(line.get("final") or 0)
        except (TypeError, ValueError):
            continue
        if not article or proposed <= 0:
            continue
        ratio = final / proposed
        if abs(ratio - 1) < NOTICEABLE:
            _log(db_path, article, "order_accepted", proposed, final, None, user)
            continue
        said = (f"removed from the order (proposed {proposed:g})" if final <= 0
                else f"changed {proposed:g} to {final:g}")
        _log(db_path, article, "order_edit", proposed, final, said, user)
        _move(db_path, article, qty=1 + STEP * (ratio - 1), reason=f"you {said}")
        moved.append({"article": article, "ratio": round(ratio, 3)})
    return moved


def refused(db_path, article, reason=None, user=None):
    """A basket with this article was turned down. Kept to be shown to the buyer."""
    article = normalize_name(article)
    if article:
        _log(db_path, article, "refused", None, None, reason, user)


def refusals(db_path, days=90, today=None):
    """article -> how many times a basket with it was refused in the last `days`."""
    since = (today or date.today()) - timedelta(days=days)
    return {a: int(n) for a, n in db.read(db_path, """
            SELECT article, count(*) FROM feedback WHERE kind = 'refused'
              AND CAST(happened_at AS DATE) >= ? GROUP BY article""", [since])}


def short_count(db_path, article, counted, expected, user=None):
    """A hand count came in below the books: the shelf loses more than the paperwork says."""
    article = normalize_name(article)
    if not article or expected is None or expected <= 0:
        return False
    if (expected - counted) / expected < SHORT_COUNT:
        return False
    _log(db_path, article, "count_short", expected, counted, "the count found less than the books", user)
    _move(db_path, article, safety=SHORT_COUNT_LIFT, reason="counts come up short")
    return True


def learn(db_path, today=None):
    """Once an hour: note the articles that ran out, and let calm ones relax toward 1."""
    from . import forecast

    today = today or date.today()
    ran_out = 0
    rows, _span = forecast.articles(db_path, use_tuning=False)
    for row in rows:
        # Only a hand count that found it empty says it ran out: the paperwork alone goes
        # negative for plenty of articles that are on the shelf.
        counted = date.fromisoformat(row["counted_on"]) if row.get("counted_on") else None
        if (row["per_day"] <= 0 or row["on_hand"] is None or row["on_hand"] > 0
                or counted is None or (today - counted).days > COUNT_FRESH):
            continue
        recent = db.read(db_path, """
                SELECT count(*) FROM feedback WHERE article = ? AND kind = 'stockout'
                  AND CAST(happened_at AS DATE) > ?""", [row["article"], today - timedelta(days=SAME_RUN)])
        if recent[0][0]:
            continue
        _log(db_path, row["article"], "stockout", None, row["on_hand"], "ran out")
        _move(db_path, row["article"], safety=STOCKOUT_LIFT, reason="it ran out")
        ran_out += 1

    relaxed = 0
    for (article,) in db.read(db_path, "SELECT article FROM tuning WHERE safety_factor <> 1.0"):
        last = db.read(db_path, """
                SELECT max(CAST(happened_at AS DATE)) FROM feedback
                WHERE article = ? AND kind IN ('stockout', 'count_short')""", [article])[0][0]
        if last is None or (today - last).days >= CALM_DAYS:
            now = db.read(db_path, "SELECT safety_factor, updated_at FROM tuning WHERE article = ?",
                          [article])[0]
            if now[1].date() >= today:                       # already moved today
                continue
            target = 1.0 + (now[0] - 1.0) * DECAY
            if abs(target - 1.0) < 0.01:
                target = 1.0
            db.write(db_path, "UPDATE tuning SET safety_factor = ?, updated_at = now(), "
                              "last_reason = 'calm for a while, easing back' WHERE article = ? "
                              "RETURNING article", [round(target, 4), article])
            relaxed += 1
    return {"ran_out": ran_out, "relaxed": relaxed}


def factors(db_path):
    """article -> (qty_factor, safety_factor), for the articles that have been moved."""
    return {a: (float(q), float(s)) for a, q, s in db.read(
        db_path, "SELECT article, qty_factor, safety_factor FROM tuning")}


def listing(db_path):
    return [{"article": a, "qty_factor": float(q), "safety_factor": float(s), "samples": int(n),
             "last_reason": r, "updated": u.isoformat(timespec="minutes")}
            for a, q, s, n, r, u in db.read(db_path, """
                SELECT article, qty_factor, safety_factor, samples, last_reason, updated_at
                FROM tuning WHERE qty_factor <> 1.0 OR safety_factor <> 1.0 OR samples > 0
                ORDER BY updated_at DESC""")]


def forget(db_path, article):
    done = db.write(db_path, "DELETE FROM tuning WHERE article = ? RETURNING article",
                    [normalize_name(article)])
    return done is not None


def recent(db_path, limit=40):
    return [{"article": a, "kind": k, "proposed": p, "final": f, "detail": d,
             "at": t.isoformat(timespec="minutes"), "by": b}
            for a, k, p, f, d, t, b in db.read(db_path, """
                SELECT article, kind, proposed, final, detail, happened_at, done_by FROM feedback
                ORDER BY feedback_id DESC LIMIT ?""", [limit])]
