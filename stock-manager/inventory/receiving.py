"""Receiving: every line that arrived is checked against what was ordered and given a place.

A delivery note is stored by the mailbox, a scan or a person. This module goes over what has come in
(`sync`, hourly and whenever the screen opens; it is safe to run again) and for each new line:

  * says whether it matches an order: as ordered, more than ordered, or on no order at all
  * suggests where it goes: the place that article was last put away at that site, else RECEIVING
  * waits as "to put away" until somebody confirms the place; the confirmation is learned, so the
    next delivery of that article is suggested to the same place

A line on no order, or more than was ordered, is an alert: either the paperwork is wrong or somebody
is about to be billed for goods nobody asked for.
"""

from datetime import date, timedelta

from . import db
from .models import normalize_name

WINDOW_DAYS = 120
LOOK_BACK_DAYS = 7
DEFAULT_PLACE = "RECEIVING"


def _place(text):
    place = " ".join(str(text or "").upper().split())
    if not place or len(place) > 40:
        raise ValueError("A place is a short name: a rack, a bay, a shelf (up to 40 characters).")
    return place


def home(db_path, article, site):
    rows = db.read(db_path, "SELECT location FROM article_homes WHERE article = ? AND site = ?",
                   [article, site or ""])
    return rows[0][0] if rows else None


def _check(db_path, article, site, supplier, quantity, day, note_id):
    """('as ordered' | 'more than ordered' | 'not on any order', words)."""
    since = day - timedelta(days=WINDOW_DAYS)
    ordered = float(db.read(db_path, """
            SELECT coalesce(sum(l.quantity), 0) FROM purchase_order_lines l JOIN purchase_orders h USING (list_id)
            WHERE l.article = ? AND l.in_stock IS FALSE AND l.quantity > 0
              AND coalesce(h.site, '') = ? AND coalesce(h.list_date, CAST(h.created_at AS DATE)) BETWEEN ? AND ?""",
                                [article, site or "", since, day])[0][0])
    before = float(db.read(db_path, """
            SELECT coalesce(sum(quantity), 0) FROM inventory_lines
            WHERE article = ? AND coalesce(site, '') = ? AND quantity > 0 AND NOT coalesce(backorder, FALSE)
              AND delivery_date BETWEEN ? AND ? AND note_id < ?""", [article, site or "", since, day, note_id])[0][0])
    if ordered <= 0:
        return "not on any order", f"no order for {article} at {site or 'this site'} in the last {WINDOW_DAYS} days"
    left = ordered - before
    if quantity <= left + 1e-9:
        return "as ordered", f"{quantity:g} of {left:g} still expected"
    return "more than ordered", f"{quantity:g} came, {max(left, 0):g} was still expected"


def sync(db_path, today=None):
    """Make a put-away line of every new delivery line. Gives back how many were added."""
    today = today or date.today()
    new = db.read(db_path, """
            SELECT l.note_id, l.line_no, l.article, coalesce(l.site, ''), l.supplier, l.quantity, l.delivery_date
            FROM inventory_lines l
            WHERE l.quantity > 0 AND NOT coalesce(l.backorder, FALSE) AND l.delivery_date >= ?
              AND NOT EXISTS (SELECT 1 FROM putaways p WHERE p.note_id = l.note_id AND p.line_no = l.line_no)
            ORDER BY l.note_id, l.line_no""", [today - timedelta(days=LOOK_BACK_DAYS)])
    for note_id, line_no, article, site, supplier, quantity, day in new:
        word, detail = _check(db_path, article, site, supplier, float(quantity), day, note_id)
        db.write(db_path, """
                INSERT INTO putaways (note_id, line_no, article, site, supplier, quantity, suggested, check_word, check_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING putaway_id""",
                 [note_id, line_no, article, site, supplier, quantity,
                  home(db_path, article, site) or DEFAULT_PLACE, word, detail])
    return len(new)


_COLUMNS = ("putaway_id, note_id, line_no, article, site, supplier, quantity, suggested, location, state, "
            "check_word, check_detail, created_at, done_at, done_by")


def _shape(values):
    keys = [k.strip() for k in _COLUMNS.split(",")]
    row = dict(zip(keys, values))
    for key in ("created_at", "done_at"):
        row[key] = row[key].isoformat(timespec="minutes") if row[key] else None
    return row


def listing(db_path, state="to put away", limit=100):
    return [_shape(r) for r in db.read(
        db_path, f"SELECT {_COLUMNS} FROM putaways WHERE state = ? ORDER BY putaway_id DESC LIMIT ?",
        [state, limit])]


def put_away(db_path, putaway_id, location=None, user=None):
    """Confirm where a pile went. With no place given, the suggested one is confirmed."""
    rows = db.read(db_path, "SELECT article, site, suggested, state FROM putaways WHERE putaway_id = ?",
                   [putaway_id])
    if not rows:
        raise ValueError("No such put-away line.")
    article, site, suggested, state = rows[0]
    if state != "to put away":
        raise ValueError("That line is already put away.")
    place = _place(location or suggested)
    db.write(db_path, """
            UPDATE putaways SET location = ?, state = 'put away', done_at = now(), done_by = ?
            WHERE putaway_id = ? RETURNING putaway_id""", [place, normalize_name(user) or None, putaway_id])
    if place != DEFAULT_PLACE:
        db.write(db_path, """
                INSERT INTO article_homes (article, site, location, last_used) VALUES (?, ?, ?, now())
                ON CONFLICT (article, site) DO UPDATE SET location = excluded.location, last_used = now()
                RETURNING article""", [article, site or "", place])
    return True


def confirm_suggested(db_path, user=None):
    """Confirm every line whose suggested place is a learned one (not the receiving bay)."""
    done = 0
    for row in listing(db_path, limit=500):
        if row["suggested"] and row["suggested"] != DEFAULT_PLACE:
            put_away(db_path, row["putaway_id"], None, user)
            done += 1
    return done


def where(db_path, article):
    """Where an article lives: the homes learned for it, by site."""
    return [{"site": s, "location": loc} for s, loc in db.read(
        db_path, "SELECT site, location FROM article_homes WHERE article = ? ORDER BY site",
        [normalize_name(article)])]


def problems(db_path, today=None, days=14):
    """Lines that arrived on no order or above what was ordered, recently: the alert for a person."""
    since = (today or date.today()) - timedelta(days=days)
    return [_shape(r) for r in db.read(db_path, f"""
            SELECT {_COLUMNS} FROM putaways
            WHERE check_word <> 'as ordered' AND created_at >= ? ORDER BY putaway_id DESC""", [since])]


def overview(db_path, today=None):
    sync(db_path, today)
    waiting = listing(db_path)
    return {"to_put_away": waiting,
            "summary": {"waiting": len(waiting),
                        "unexpected": sum(1 for r in waiting if r["check_word"] != "as ordered"),
                        "suggested": sum(1 for r in waiting if r["suggested"] != DEFAULT_PLACE)}}
