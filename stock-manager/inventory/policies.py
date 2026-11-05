"""Rules a person sets so that routine work goes through without them, and nothing else does.

A policy says: for this action, with this supplier, up to this much, inside these hours and
budgets, the software may go ahead by itself. There are three actions:

  send_order   mail a drafted order to its supplier
  send_chase   mail the reminder for an order nobody has answered
  approve      approve a basket the gate is holding

The freedom is bounded on every side:

  * A kill switch, off until somebody turns it on, stops everything at once.
  * A policy starts in dry-run, which writes down what it would have done and does nothing. It
    can only be set live after three days of that, so there is a record to read first.
  * Absolute limits that no policy can raise: a single action never passes HARD_CEILING euros,
    nothing is sent outside working days and the policy's hours, and an order for a price that
    cannot be checked, from a supplier with too little history, or to no address, is never sent.
  * Approving a basket is only ever for the reasons the policy lists, never without Terminus
    having read the names as the same, and a basket held because of a new article or a new
    supplier is held unless the policy names that reason.
  * Every decision, allowed or refused, live or dry, is a row in the log with its reasons, and
    everything done is signed POLICY in the audit, with the policy's name in the detail.
"""

from datetime import date, datetime, timedelta

from . import audit, db, gate, outbox, pricing, prices, workdays
from .models import normalize_name

ACTIONS = ("send_order", "send_chase", "approve")
HARD_CEILING = 5000.0
MIN_DRY_DAYS = 3
REASONS = ("first-article", "new-supplier", "over-ceiling", "dear-line", "no-price")

_FIELDS = ("name", "action", "active", "mode", "supplier", "max_total", "per_day", "per_month",
           "price_band", "min_deliveries", "allowed_reasons", "terminus_same", "hours_from",
           "hours_to", "per_supplier_day", "max_chases")


# ---- the kill switch and the rules ------------------------------------------

def enabled(db_path):
    rows = db.read(db_path, "SELECT value FROM policy_settings WHERE key = 'enabled'")
    return bool(rows) and rows[0][0] == "1"


def set_enabled(db_path, on, user=None):
    db.write(db_path, "INSERT INTO policy_settings (key, value) VALUES ('enabled', ?) "
                      "ON CONFLICT (key) DO UPDATE SET value = excluded.value RETURNING key",
             ["1" if on else "0"])
    audit.record(db_path, user or "POLICY", "policy.switch", "kill switch", "on" if on else "off")


def _shape(row):
    out = dict(zip(("policy_id",) + _FIELDS + ("created_by", "created_at"), row))
    out["created_at"] = out["created_at"].isoformat(timespec="minutes")
    out["dry_days"] = (date.today() - date.fromisoformat(out["created_at"][:10])).days
    out["may_go_live"] = out["dry_days"] >= MIN_DRY_DAYS
    return out


def listing(db_path):
    rows = db.read(db_path, f"SELECT policy_id, {', '.join(_FIELDS)}, created_by, created_at "
                            "FROM policies ORDER BY policy_id")
    return [_shape(r) for r in rows]


def one(db_path, policy_id):
    rows = db.read(db_path, f"SELECT policy_id, {', '.join(_FIELDS)}, created_by, created_at "
                            "FROM policies WHERE policy_id = ?", [policy_id])
    return _shape(rows[0]) if rows else None


def _number(value, name, low=0.0, high=None, whole=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} is a number.") from None
    if number < low or (high is not None and number > high):
        raise ValueError(f"{name} must be {'between ' + str(low) + ' and ' + str(high) if high is not None else 'at least ' + str(low)}.")
    return int(number) if whole else number


def create(db_path, body, user=None):
    """A new policy: inactive and in dry-run, whatever the body says."""
    action = body.get("action")
    if action not in ACTIONS:
        raise ValueError(f"The action is one of {', '.join(ACTIONS)}.")
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("A policy needs a name.")
    reasons = [r.strip() for r in str(body.get("allowed_reasons") or "").split(",") if r.strip()]
    for reason in reasons:
        if reason not in REASONS:
            raise ValueError(f"Unknown reason {reason}; the gate's are {', '.join(REASONS)}.")
    row = db.write(db_path, """
            INSERT INTO policies (name, action, supplier, max_total, per_day, per_month, price_band,
                                  min_deliveries, allowed_reasons, terminus_same, hours_from,
                                  hours_to, per_supplier_day, max_chases, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING policy_id""",
                   [name, action, (normalize_name(body.get("supplier")) or "*"),
                    _number(body.get("max_total"), "The total", 0.01, HARD_CEILING),
                    _number(body["per_day"], "The daily budget", 0.01) if body.get("per_day") else None,
                    _number(body["per_month"], "The monthly budget", 0.01) if body.get("per_month") else None,
                    _number(body.get("price_band", 0.10), "The price band", 0.0, 1.0),
                    _number(body.get("min_deliveries", 3), "The history", 0, whole=True),
                    ",".join(reasons), True if action == "approve" else bool(body.get("terminus_same", True)),
                    _number(body.get("hours_from", 8), "The first hour", 0, 23, True),
                    _number(body.get("hours_to", 17), "The last hour", 1, 24, True),
                    _number(body.get("per_supplier_day", 2), "The mails a supplier a day", 1, whole=True),
                    _number(body.get("max_chases", 2), "The reminders", 0, whole=True), user])
    audit.record(db_path, user or "POLICY", "policy.create", name, action)
    return one(db_path, row[0])


def update(db_path, policy_id, changes, user=None):
    held = one(db_path, policy_id)
    if held is None:
        raise ValueError("No such policy.")
    if "active" in changes:
        db.write(db_path, "UPDATE policies SET active = ? WHERE policy_id = ? RETURNING policy_id",
                 [bool(changes["active"]), policy_id])
    if "mode" in changes:
        mode = changes["mode"]
        if mode not in ("dry_run", "live"):
            raise ValueError("The mode is dry_run or live.")
        if mode == "live" and not held["may_go_live"]:
            raise ValueError(f"A policy runs {MIN_DRY_DAYS} days in dry-run before it can go live "
                             f"({held['dry_days']} so far).")
        db.write(db_path, "UPDATE policies SET mode = ? WHERE policy_id = ? RETURNING policy_id",
                 [mode, policy_id])
    for field, low, high in (("max_total", 0.01, HARD_CEILING), ("price_band", 0.0, 1.0),
                             ("per_day", 0.01, None), ("per_month", 0.01, None)):
        if field in changes and changes[field] not in (None, ""):
            db.write(db_path, f"UPDATE policies SET {field} = ? WHERE policy_id = ? RETURNING policy_id",
                     [_number(changes[field], field, low, high), policy_id])
    audit.record(db_path, user or "POLICY", "policy.update", held["name"], ", ".join(sorted(changes)))
    return one(db_path, policy_id)


def remove(db_path, policy_id):
    return db.write(db_path, "DELETE FROM policies WHERE policy_id = ? RETURNING policy_id",
                    [policy_id]) is not None


# ---- what a rule is checked against -----------------------------------------

def _delivered(db_path, supplier):
    return db.read(db_path, "SELECT count(DISTINCT note_id) FROM inventory_lines "
                            "WHERE upper(supplier) = upper(?)", [supplier or ""])[0][0]


def _spent(db_path, policy_id, since):
    return float(db.read(db_path, """
            SELECT coalesce(sum(amount), 0) FROM policy_log
            WHERE policy_id = ? AND decision = 'allowed' AND happened_at >= ?""", [policy_id, since])[0][0])


def _today_count(db_path, policy_id, supplier, day):
    return db.read(db_path, """
            SELECT count(*) FROM policy_log WHERE policy_id = ? AND decision = 'allowed'
              AND upper(coalesce(supplier, '')) = upper(?) AND CAST(happened_at AS DATE) = ?""",
                   [policy_id, supplier or "", day])[0][0]


def _reference_price(db_path, article, supplier, day):
    agreed = pricing.agreement_for(db_path, article, day)
    if agreed and agreed["supplier"].upper() == (supplier or "").upper():
        return agreed["unit_price"]
    return prices.usual(db_path, article)


def order_value(db_path, mail, day):
    """(value in euros, reasons it cannot be checked, price drift reasons) for an order mail."""
    unchecked, drift, total = [], [], 0.0
    try:
        _site, _work, groups, _left = outbox._order_groups(db_path, mail["ref_id"])
    except ValueError:
        return 0.0, ["the order no longer exists"], []
    for article, _d, _r, quantity in groups.get(mail["supplier"], []):
        packed = None
        from . import units

        packed = units.to_packs(db_path, article, mail["supplier"], quantity)
        wanted = packed["base"] if packed else float(quantity)
        reference = _reference_price(db_path, article, mail["supplier"], day)
        if reference is None:
            unchecked.append(f"no reference price for {article}")
            continue
        total += wanted * reference
        seen = prices.sightings(db_path, article, 1)
        drift.append((article, seen[0] / reference - 1) if seen and reference else (article, 0.0))
    return round(total, 2), unchecked, drift


def _window(policy, now, calendar):
    reasons = []
    if not calendar.working(now.date()):
        reasons.append("not a working day")
    if not (policy["hours_from"] <= now.hour < policy["hours_to"]):
        reasons.append(f"outside {policy['hours_from']}h to {policy['hours_to']}h")
    return reasons


def _budget(db_path, policy, amount, now):
    reasons = []
    if amount > HARD_CEILING:
        reasons.append(f"over the absolute limit of {HARD_CEILING:g} EUR")
    if amount > policy["max_total"]:
        reasons.append(f"{amount:.2f} is over this policy's {policy['max_total']:g} EUR")
    if policy["per_day"] and _spent(db_path, policy["policy_id"], datetime.combine(now.date(), datetime.min.time())) + amount > policy["per_day"]:
        reasons.append(f"it would pass the {policy['per_day']:g} EUR a day")
    if policy["per_month"] and _spent(db_path, policy["policy_id"], now - timedelta(days=30)) + amount > policy["per_month"]:
        reasons.append(f"it would pass the {policy['per_month']:g} EUR a month")
    return reasons


def _matches(policy, supplier):
    return policy["supplier"] in ("*", "") or (supplier or "").upper() == policy["supplier"].upper()


def check_order(db_path, policy, mail, now):
    """Every reason a drafted order may not be sent under this policy (empty: it may)."""
    calendar = workdays.Calendar(db_path, mail["supplier"])
    reasons = _window(policy, now, calendar)
    if not (mail.get("to_addr") or "").strip():
        reasons.append("the draft has no address")
    value, unchecked, drift = order_value(db_path, mail, now.date())
    reasons += unchecked
    if not unchecked and value <= 0:
        reasons.append("the order comes to nothing")
    for article, moved in drift:
        if abs(moved) > policy["price_band"]:
            reasons.append(f"the price of {article} is {moved * 100:+.0f}% off the reference")
    if _delivered(db_path, mail["supplier"]) < policy["min_deliveries"]:
        reasons.append(f"fewer than {policy['min_deliveries']} deliveries from {mail['supplier']}")
    if _today_count(db_path, policy["policy_id"], mail["supplier"], now.date()) >= policy["per_supplier_day"]:
        reasons.append(f"already {policy['per_supplier_day']} mails to {mail['supplier']} today")
    reasons += _budget(db_path, policy, value, now)
    return reasons, value


def check_chase(db_path, policy, thread, now):
    calendar = workdays.Calendar(db_path, thread["supplier"])
    reasons = _window(policy, now, calendar)
    if not (thread.get("to_addr") or "").strip():
        reasons.append("no address to write to")
    if thread["chase_count"] >= policy["max_chases"]:
        reasons.append(f"already chased {thread['chase_count']} times: a person takes it now")
    if _delivered(db_path, thread["supplier"]) < policy["min_deliveries"]:
        reasons.append(f"fewer than {policy['min_deliveries']} deliveries from {thread['supplier']}")
    if _today_count(db_path, policy["policy_id"], thread["supplier"], now.date()) >= policy["per_supplier_day"]:
        reasons.append(f"already {policy['per_supplier_day']} mails to {thread['supplier']} today")
    return reasons


def check_basket(db_path, policy, request, now):
    """Reasons a held basket may not be approved under this policy."""
    calendar = workdays.Calendar(db_path)
    reasons = _window(policy, now, calendar)
    allowed = {r for r in policy["allowed_reasons"].split(",") if r}
    for why in request["reasons"]:
        if why not in allowed:
            reasons.append(f"held for '{why}', which this policy does not cover")
    if request.get("agent_verdict") != "same":
        reasons.append("Terminus has not read the names as the same")
    total = float(request.get("total") or 0)
    reasons += _budget(db_path, policy, total, now)
    for line in request["lines"]:
        price, usual = line.get("unit_price"), line.get("usual")
        if price is None:
            reasons.append(f"{line.get('article')} has no price")
        elif usual and abs(price / usual - 1) > policy["price_band"]:
            reasons.append(f"{line.get('article')} is {(price / usual - 1) * 100:+.0f}% off the usual price")
    return reasons, total


# ---- the log and the run ----------------------------------------------------

def _logged_today(db_path, policy_id, subject, decision, day):
    return bool(db.read(db_path, """
            SELECT 1 FROM policy_log WHERE policy_id = ? AND subject = ? AND decision = ?
              AND CAST(happened_at AS DATE) = ?""", [policy_id, subject, decision, day]))


def _log(db_path, policy, action, subject, supplier, amount, reasons, acted, now):
    decision = "denied" if reasons else "allowed"
    db.write(db_path, """
            INSERT INTO policy_log (happened_at, policy_id, policy_name, action, subject, supplier, amount,
                                    decision, reasons, mode, acted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING log_id""",
             [now, policy["policy_id"], policy["name"], action, subject, supplier, amount, decision,
              "; ".join(reasons) or None, policy["mode"], acted])
    return decision


def _try(db_path, policies, action, subject, supplier, check, now, act):
    """Offer a subject to each active policy of this action, in order, until one lets it go.
    `check()` gives (reasons, amount) for a policy, `act(policy)` does the thing. Gives back the
    name of the policy that acted (live), or None."""
    for policy in policies:
        if not policy["active"] or policy["action"] != action or not _matches(policy, supplier):
            continue
        reasons, amount = check(policy)
        if reasons:
            if not _logged_today(db_path, policy["policy_id"], subject, "denied", now.date()):
                _log(db_path, policy, action, subject, supplier, amount, reasons, False, now)
            continue
        if policy["mode"] != "live":
            if not _logged_today(db_path, policy["policy_id"], subject, "allowed", now.date()):
                _log(db_path, policy, action, subject, supplier, amount, [], False, now)
            return None
        try:
            act(policy)
        except Exception as e:                       # a refused send is a refusal, not a crash
            _log(db_path, policy, action, subject, supplier, amount, [f"it failed: {e}"], False, now)
            continue
        _log(db_path, policy, action, subject, supplier, amount, [], True, now)
        audit.record(db_path, "POLICY", f"policy.{action}", subject,
                     f"{policy['name']}" + (f", {amount:.2f} EUR" if amount else ""))
        return policy["name"]
    return None


def run(db_path, config, now=None, connect=None):
    """What the server does every hour: offer what is waiting to the policies. Does nothing at
    all while the kill switch is off."""
    now = now or datetime.now()
    if not enabled(db_path):
        return {"skipped": "kill switch is off", "acted": 0}
    policies = [p for p in listing(db_path) if p["active"]]
    if not policies:
        return {"skipped": "no active policy", "acted": 0}
    acted, sendkw = 0, ({"connect": connect} if connect else {})
    ready = outbox.readiness(config.mail)["ready"] if config is not None else False

    def sendable(result):
        reasons, amount = result
        return (reasons if ready else reasons + ["outgoing mail is not set up"]), amount

    # An order Plutus created is drafted to its suppliers, so there is something to offer.
    if config is not None and any(p["action"] == "send_order" for p in policies):
        for (list_id,) in db.read(db_path, """
                SELECT list_id FROM purchase_orders h WHERE upper(coalesce(drafter, '')) = 'PLUTUS'
                  AND created_at >= now() - INTERVAL 3 DAY
                  AND NOT EXISTS (SELECT 1 FROM supplier_mail m WHERE m.ref_kind = 'po'
                                  AND m.ref_id = h.list_id AND m.direction = 'out')"""):
            try:
                outbox.draft_order(db_path, list_id, "POLICY")
            except ValueError:
                pass
        for row in outbox.overview(db_path, config.mail)["drafts"]:
            if row["kind"] != "order" or row["state"] != "draft":
                continue
            who = _try(db_path, policies, "send_order", f"mail:{row['mail_id']}", row["supplier"],
                       lambda p, r=row: sendable(check_order(db_path, p, r, now)), now,
                       lambda p, r=row: outbox.send(db_path, r["mail_id"], config.mail,
                                                    f"POLICY: {p['name']}", **sendkw))
            acted += 1 if who else 0

    if config is not None and any(p["action"] == "send_chase" for p in policies):
        for thread in outbox.threads(db_path, now.date()):
            if not thread["needs_chase"]:
                continue
            draft = outbox.draft_chase(db_path, thread["mail_id"], "POLICY")["drafts"][0]
            who = _try(db_path, policies, "send_chase", f"mail:{draft['mail_id']}", thread["supplier"],
                       lambda p, t=thread: sendable((check_chase(db_path, p, t, now), 0.0)), now,
                       lambda p, d=draft: outbox.send(db_path, d["mail_id"], config.mail,
                                                      f"POLICY: {p['name']}", **sendkw))
            acted += 1 if who else 0

    acted += len(auto_decide(db_path, now=now, policies=policies))
    return {"acted": acted}


def auto_decide(db_path, request_id=None, now=None, policies=None):
    """Offer the baskets the gate is holding to the approve policies. Gives back the ids approved."""
    now = now or datetime.now()
    if not enabled(db_path):
        return []
    policies = policies if policies is not None else [p for p in listing(db_path) if p["active"]]
    approved = []
    for request in gate.waiting(db_path):
        if request_id is not None and request["request_id"] != request_id:
            continue
        if str(request.get("channel") or "").startswith("po:"):
            continue                       # Plutus's own order is decided by Plutus
        supplier = next((l.get("supplier") for l in request["lines"] if l.get("supplier")), None)
        who = _try(db_path, policies, "approve", f"basket:{request['request_id']}", supplier,
                   lambda p, r=request: check_basket(db_path, p, r, now), now,
                   lambda p, r=request: gate.approve(db_path, r["request_id"], f"POLICY: {p['name']}"))
        if who:
            approved.append(request["request_id"])
    return approved


def log_recent(db_path, limit=60):
    return [{"log_id": i, "at": a.isoformat(timespec="minutes"), "policy": n, "action": ac,
             "subject": s, "supplier": su, "amount": am, "decision": d, "reasons": r, "mode": m,
             "acted": bool(acted)}
            for i, a, n, ac, s, su, am, d, r, m, acted in db.read(db_path, """
                SELECT log_id, happened_at, policy_name, action, subject, supplier, amount, decision,
                       reasons, mode, acted FROM policy_log ORDER BY log_id DESC LIMIT ?""", [limit])]


def dry_run_summary(db_path, days=7):
    """What each policy has allowed and refused lately, so a person can judge it before it goes live."""
    since = datetime.now() - timedelta(days=days)
    out = []
    for policy in listing(db_path):
        rows = db.read(db_path, """
                SELECT decision, count(*), coalesce(sum(amount), 0) FROM policy_log
                WHERE policy_id = ? AND happened_at >= ? GROUP BY decision""", [policy["policy_id"], since])
        got = {d: (int(n), float(a)) for d, n, a in rows}
        out.append({"policy_id": policy["policy_id"], "name": policy["name"], "mode": policy["mode"],
                    "allowed": got.get("allowed", (0, 0.0))[0],
                    "allowed_value": round(got.get("allowed", (0, 0.0))[1], 2),
                    "denied": got.get("denied", (0, 0.0))[0]})
    return out


def overview(db_path):
    return {"enabled": enabled(db_path), "hard_ceiling": HARD_CEILING, "min_dry_days": MIN_DRY_DAYS,
            "policies": listing(db_path), "log": log_recent(db_path),
            "summary": dry_run_summary(db_path), "reasons": list(REASONS)}
