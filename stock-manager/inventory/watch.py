"""Things that need a person, found by the app instead of by looking.

Every hour the app looks over its own books for what has gone wrong or is about to, and keeps
a list of notices. A notice has a key that says what it is about, so the same trouble is one
notice for as long as it lasts, comes back to nothing when it is fixed, and is only pushed
out once: to Slack, by SMS, or by mail to yourself, whichever NOTIFY_VIA names. Nothing is
pushed unless it is set, and the list is always on the home screen. Once a day, at NOTIFY_HOUR,
a digest of everything still open goes the same way.

What it looks at:

  critical  stock at zero for an article that is used; an order unanswered for a long time
  high      stock that will run out before a delivery can come; an order past its day; a
            basket held for a day; a bill past its due date; a quote needed within 3 days
  normal    a reply to chase; delivery-note lines owed; a bill that does not match; a dispute
            with no credit note; files waiting in the queue; a price that jumped
  low       counts overdue; deliveries not billed
"""

import os
from datetime import date, datetime, timedelta

from . import db, disputes, forecast, gate, matching, outbox, stockctl

SEVERITIES = ("critical", "high", "normal", "low")
PRICE_JUMP = 0.25          # a new price this much above the usual one is a notice
GATE_HOURS = 24
QUOTE_DAYS = 3


def conditions(db_path, today=None):
    today = today or date.today()
    found = []

    def add(key, kind, severity, title, detail=""):
        found.append((key, kind, severity, title, detail))

    rows, _span = forecast.articles(db_path)
    for row in rows:
        if row["per_day"] <= 0 or row["on_hand"] is None:
            continue
        if row["on_hand"] <= 0:
            add(f"stockout:{row['article']}", "stock", "critical",
                f"{row['article']} is at zero",
                f"used {row['per_week']:g} a week, lead time {row['lead_days']:g} days")
        elif row["state"] == "short":
            days = row["cover_days"]
            add(f"short:{row['article']}", "stock", "high",
                f"{row['article']} will run out before a delivery can arrive",
                f"{row['on_hand']:g} on hand, {days:g} days of cover, lead time {row['lead_days']:g} days")

    for order in matching.orders(db_path, today)["orders"]:
        if order["state"] == "late":
            add(f"order-late:{order['list_id']}", "order", "high",
                f"Order #{order['list_id']} has nothing delivered after {order['age_days']} days",
                f"{order['site'] or ''} {order['work_item'] or ''}".strip())

    for thread in outbox.threads(db_path, today):
        if thread["needs_chase"]:
            add(f"chase:{thread['mail_id']}", "supplier",
                "critical" if thread["days_waiting"] >= 10 else "normal",
                f"{thread['supplier']}: {thread['why']} ({thread['kind']} #{thread['ref_id']})",
                f"waiting {thread['days_waiting']} working day(s), expected {thread['expected_on']}")
    for owed in outbox.owing(db_path, today):
        add(f"owing:{owed['supplier']}", "supplier", "normal",
            f"{owed['supplier']} still owes {len(owed['lines'])} line(s) from delivery notes",
            ", ".join(line["article"] for line in owed["lines"][:5]))

    for held in gate.waiting(db_path):
        asked = datetime.fromisoformat(held["asked_at"]) if held.get("asked_at") else None
        if asked and datetime.combine(today, datetime.max.time()) - asked >= timedelta(hours=GATE_HOURS):
            add(f"gate:{held['request_id']}", "approval", "high",
                f"Basket #{held['request_id']} has waited for approval over {GATE_HOURS} hours",
                f"{held.get('shop') or ''} {held.get('site') or ''}".strip())

    for bill in matching.bills(db_path, today)["bills"]:
        if bill["state"] == "disputed":
            continue
        if bill["verdict"] in ("over", "nothing delivered"):
            add(f"bill:{bill['bill_id']}", "bill", "normal",
                f"{bill['supplier']} bill {bill['number'] or bill['bill_id']}: {bill['verdict']}",
                f"billed {bill['billed']}, deliveries come to {bill['expected']}")
    for bill_id, supplier, number, due, total in db.read(db_path, """
            SELECT bill_id, supplier, number, due_date, total FROM bills
            WHERE state = 'to pay' AND due_date IS NOT NULL AND due_date < ?""", [today]):
        add(f"due:{bill_id}", "bill", "high", f"{supplier} bill {number or bill_id} is past its due date",
            f"due {due}, {total}")
    for dispute in disputes.listing(db_path, today):
        if dispute["overdue"]:
            add(f"dispute:{dispute['dispute_id']}", "bill", "normal",
                f"No credit note yet from {dispute['supplier']} ({dispute['age_days']} days)",
                dispute["reason"] or "")

    for quote_id, supplier, needed, state in db.read(db_path, """
            SELECT quote_id, supplier, needed_by, state FROM quotes
            WHERE state IN ('open', 'sent') AND needed_by IS NOT NULL AND needed_by <= ?""",
                                                    [today + timedelta(days=QUOTE_DAYS)]):
        add(f"quote:{quote_id}", "quote", "high", f"Quote #{quote_id} ({supplier or '?'}) is needed by {needed}",
            f"still {state}")

    from . import purchasing

    for stop in purchasing.blocked(db_path):
        what = {"send_order": "send an order", "approve": "approve a basket", "place_basket": "place a basket",
                "pay_bill": "pay a bill", "open_gate": "open an order", "coherence": "pass an order"}.get(
                    stop["action"], stop["action"])
        who = "Plutus" if stop["who"] == "PLUTUS" else "Terminus"
        dry = " (dry run)" if stop["mode"] == "dry_run" else ""
        add(f"blocked:{stop['who']}:{stop['action']}:{stop['subject']}", "blocked", "high",
            f"{who} could not {what}{dry}: {stop['subject'].replace(':', ' ')}"
            + (f", {stop['supplier']}" if stop.get("supplier") else ""), stop.get("reasons") or "")

    waiting = db.read(db_path, "SELECT count(*) FROM intake WHERE state = 'waiting' "
                               "AND arrived_at < now() - INTERVAL 1 DAY")[0][0]
    if waiting:
        add("intake", "intake", "normal", f"{waiting} file(s) have waited in the queue over a day")

    for article, price, usual in db.read(db_path, """
            WITH latest AS (
                SELECT article, unit_price, row_number() OVER (PARTITION BY article
                       ORDER BY seen_on DESC, price_id DESC) AS n FROM article_prices),
            usual AS (SELECT article, median(unit_price) AS m FROM latest WHERE n BETWEEN 2 AND 13
                      GROUP BY article HAVING count(*) >= 3)
            SELECT l.article, l.unit_price, u.m FROM latest l JOIN usual u USING (article)
            WHERE l.n = 1 AND l.unit_price > u.m * ?""", [1 + PRICE_JUMP]):
        add(f"price:{article}", "price", "normal", f"{article} price jumped",
            f"{price:.2f} against a usual {usual:.2f}")

    due = stockctl.cycle_due(db_path, today)["total_due"]
    if due >= 20:
        add("counts", "stock", "low", f"{due} articles are due for a count")
    for unbilled in matching.bills(db_path, today)["unbilled"]:
        add(f"unbilled:{unbilled['supplier']}", "bill", "low",
            f"{unbilled['supplier']}: {unbilled['deliveries']} delivery(ies) not billed in {unbilled['days']} days",
            f"worth about {unbilled['worth']}")
    return found


def scan(db_path, today=None):
    """Bring the notice list up to date. Gives back the notices that are new."""
    seen = conditions(db_path, today)
    keys = {key for key, *_rest in seen}
    new = []
    for key, kind, severity, title, detail in seen:
        row = db.read(db_path, "SELECT notice_id, resolved_at FROM notices WHERE key = ?", [key])
        if row:
            db.write(db_path, "UPDATE notices SET kind = ?, severity = ?, title = ?, detail = ?, "
                              "last_seen = now(), resolved_at = NULL WHERE key = ? RETURNING notice_id",
                     [kind, severity, title, detail, key])
            if row[0][1] is not None:                 # it had been fixed, and it is back
                db.write(db_path, "UPDATE notices SET first_seen = now(), pushed_at = NULL "
                                  "WHERE key = ? RETURNING notice_id", [key])
                new.append(key)
        else:
            db.write(db_path, "INSERT INTO notices (key, kind, severity, title, detail) "
                              "VALUES (?, ?, ?, ?, ?) RETURNING notice_id",
                     [key, kind, severity, title, detail])
            new.append(key)
    for (key,) in db.read(db_path, "SELECT key FROM notices WHERE resolved_at IS NULL "
                                   "AND kind <> 'digest'"):
        if key not in keys:
            db.write(db_path, "UPDATE notices SET resolved_at = now() WHERE key = ? RETURNING notice_id",
                     [key])
    return new


def open_notices(db_path, limit=100):
    rows = db.read(db_path, """
            SELECT notice_id, key, kind, severity, title, detail, first_seen
            FROM notices WHERE resolved_at IS NULL AND kind <> 'digest'
            ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                   WHEN 'normal' THEN 2 ELSE 3 END, first_seen
            LIMIT ?""", [limit])
    return [{"notice_id": i, "key": k, "kind": kind, "severity": sev, "title": t, "detail": d,
             "since": f.isoformat(timespec="minutes")} for i, k, kind, sev, t, d, f in rows]


# ---- pushing out ----------------------------------------------------------

def channels():
    return [c.strip().lower() for c in os.environ.get("NOTIFY_VIA", "").split(",") if c.strip()]


def _slack(text):
    from . import slack_bot

    kept = slack_bot.settings()
    if not kept["bot_token"] or not kept["channels"]:
        return False
    from slack_sdk import WebClient

    WebClient(token=kept["bot_token"]).chat_postMessage(channel=kept["channels"][0], text=text)
    return True


def _sms(config, text):
    from . import alerts

    return alerts.send(config.sms, text) == "sent"


def _mail(config, text, subject):
    import smtplib
    from email.message import EmailMessage

    mail = config.mail
    ready = outbox.readiness(mail)
    if not ready["ready"]:
        return False
    message = EmailMessage()
    message["From"] = message["To"] = mail.user
    message["Subject"] = subject
    message.set_content(text)
    link = outbox._connect(mail, outbox.smtp_for(mail))
    try:
        link.send_message(message)
    finally:
        link.quit()
    return True


def deliver(config, text, subject="Logistics Manager", send=None):
    """Send `text` by every channel NOTIFY_VIA names. `send` replaces the real senders in tests."""
    sent = []
    for channel in channels():
        try:
            if send is not None:
                ok = send(channel, text, subject)
            elif channel == "slack":
                ok = _slack(text)
            elif channel == "sms":
                ok = _sms(config, text)
            elif channel == "mail":
                ok = _mail(config, text, subject)
            else:
                ok = False
        except Exception:
            ok = False
        if ok:
            sent.append(channel)
    return sent


def push_new(db_path, config, send=None):
    """Push the critical and high notices not yet pushed, in one message."""
    rows = db.read(db_path, """
            SELECT notice_id, severity, title FROM notices
            WHERE resolved_at IS NULL AND pushed_at IS NULL AND severity IN ('critical', 'high')
              AND kind <> 'digest' ORDER BY severity, first_seen""")
    if not rows or not channels():
        return []
    text = "Needs you:\n" + "\n".join(f"- [{sev}] {title}" for _id, sev, title in rows)
    sent = deliver(config, text, "Needs you", send)
    if sent:
        for notice_id, _sev, _title in rows:
            db.write(db_path, "UPDATE notices SET pushed_at = now() WHERE notice_id = ? "
                              "RETURNING notice_id", [notice_id])
    return sent


def digest(db_path, config, now=None, send=None):
    """Once a day, at NOTIFY_HOUR, everything still open."""
    now = now or datetime.now()
    try:
        hour = int(os.environ.get("NOTIFY_HOUR", "8"))
    except ValueError:
        hour = 8
    if now.hour < hour or not channels():
        return []
    key = f"digest:{now.date().isoformat()}"
    if db.read(db_path, "SELECT 1 FROM notices WHERE key = ?", [key]):
        return []
    items = open_notices(db_path)
    db.write(db_path, "INSERT INTO notices (key, kind, severity, title, resolved_at) "
                      "VALUES (?, 'digest', 'low', 'daily digest', now()) RETURNING notice_id", [key])
    if not items:
        return []
    text = f"{len(items)} thing(s) need you:\n" + "\n".join(
        f"- [{n['severity']}] {n['title']}" for n in items[:40])
    return deliver(config, text, "Daily digest", send)


def run(db_path, config, today=None, now=None, send=None):
    new = scan(db_path, today)
    pushed = push_new(db_path, config, send)
    sent = digest(db_path, config, now, send)
    return {"new": len(new), "open": len(open_notices(db_path)), "pushed": pushed, "digest": sent}
