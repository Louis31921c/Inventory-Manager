"""Terminus decides the baskets the gate holds.

Terminus has always read the names. It now also approves and cancels, inside a charter:

  approve   every line read as the same article, the total within its limit, the day's total
            within its budget, and everything that held the basket settled: a first article
            is settled by the names being known, a dear line, a new supplier or a missing price
            only when the limits allow it, and a basket over the gate's ceiling only when its own
            limit is higher.
  cancel    a basket that repeats one already approved or ordered, one with an article refused
            repeatedly, or one nobody has answered for too long.

It never touches what a person decided, and it starts off, then in dry run, which writes down
what it would have done, and goes live only after the same three days of that as a policy. Every
decision is in the log and in the gate's sealed chain, signed TERMINUS.
"""

import json
from datetime import date, datetime, timedelta

from . import audit, charters, db, feedback, gate
from .policies import HARD_CEILING, MIN_DRY_DAYS

MODES = ("off", "dry_run", "live")
WHO = "TERMINUS"


def _setting(db_path, key):
    rows = db.read(db_path, "SELECT value FROM policy_settings WHERE key = ?", [key])
    return rows[0][0] if rows else None


def _put(db_path, key, value):
    db.write(db_path, "INSERT INTO policy_settings (key, value) VALUES (?, ?) "
                      "ON CONFLICT (key) DO UPDATE SET value = excluded.value RETURNING key", [key, value])


def mode(db_path):
    return _setting(db_path, "terminus_mode") or "off"


def status(db_path, today=None):
    since = _setting(db_path, "terminus_dry_since")
    days = (((today or date.today()) - date.fromisoformat(since)).days if since else 0)
    return {"mode": mode(db_path), "dry_days": days, "may_go_live": days >= MIN_DRY_DAYS,
            "min_dry_days": MIN_DRY_DAYS, "limits": charters.limits(db_path, "terminus")}


def set_mode(db_path, new, user=None, today=None):
    if new not in MODES:
        raise ValueError("The mode is off, dry_run or live.")
    now = status(db_path, today)
    if new == "live" and not now["may_go_live"]:
        raise ValueError(f"Terminus runs {MIN_DRY_DAYS} days in dry run before it can decide for real "
                         f"({now['dry_days']} so far).")
    if new == "dry_run" and now["mode"] == "off":
        _put(db_path, "terminus_dry_since", (today or date.today()).isoformat())
    if new == "off":
        _put(db_path, "terminus_dry_since", "")
    _put(db_path, "terminus_mode", new)
    audit.record(db_path, user or WHO, "terminus.mode", "terminus", new)
    return status(db_path, today)


def _lines_key(lines):
    return sorted((str(l.get("article") or "").upper(), float(l.get("quantity") or 0)) for l in lines)


def _cancel_reason(db_path, request, rules, now):
    refused = feedback.refusals(db_path, today=now.date())
    for line in request["lines"]:
        article = str(line.get("article") or "").upper()
        if rules["cancel_refused_after"] and refused.get(article, 0) >= rules["cancel_refused_after"]:
            return f"{article} was refused {refused[article]} times in 90 days"
    days = int(rules["cancel_duplicate_days"])
    if days:
        mine = _lines_key(request["lines"])
        for other in gate.history(db_path, 200):
            if other["request_id"] == request["request_id"] or other["state"] not in ("approved", "ordered"):
                continue
            decided = other.get("decided_at") or other.get("asked_at")
            if decided and now - datetime.fromisoformat(decided) <= timedelta(days=days) \
                    and _lines_key(other["lines"]) == mine:
                return f"it repeats request #{other['request_id']}, already {other['state']}"
    old = int(rules["cancel_after_days"])
    if old and request.get("asked_at") and now - datetime.fromisoformat(request["asked_at"]) > timedelta(days=old):
        return f"nobody answered it in {old} days"
    return None


def _approve_blockers(db_path, request, rules, now):
    """Everything that stops Terminus approving this basket (empty: it may)."""
    out = []
    if request.get("agent_verdict") != "same":
        out.append("the names are not all read as the same article")
    total = float(request.get("total") or 0)
    cap = min(rules["approve_max_total"], HARD_CEILING)
    if total > cap:
        out.append(f"{total:.2f} EUR is over its limit of {cap:g}")
    spent = float(db.read(db_path, """
            SELECT coalesce(sum(amount), 0) FROM policy_log WHERE policy_name = ? AND action = 'approve'
              AND decision = 'allowed' AND CAST(happened_at AS DATE) = ?""", [WHO, now.date()])[0][0])
    if spent + total > rules["approve_per_day"]:
        out.append(f"it would pass {rules['approve_per_day']:g} EUR approved today")
    allowed = {"first-article": True, "over-ceiling": total <= cap,
               "dear-line": bool(rules["allow_dear_line"]), "new-supplier": bool(rules["allow_new_supplier"]),
               "no-price": bool(rules["allow_no_price"])}
    for why in request["reasons"]:
        if not allowed.get(why, False):
            out.append(f"it was held for '{why}', which a person decides")
    return out


def _log(db_path, action, request, amount, reasons, acted, now, current):
    db.write(db_path, """
            INSERT INTO policy_log (happened_at, policy_id, policy_name, action, subject, supplier, amount,
                                    decision, reasons, mode, acted)
            VALUES (?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?) RETURNING log_id""",
             [now, WHO, action, f"basket:{request['request_id']}", amount,
              "denied" if (reasons and action == "approve") else "allowed",
              "; ".join(reasons) or None, current, acted])


def _seen_today(db_path, action, request_id, now):
    return bool(db.read(db_path, """
            SELECT 1 FROM policy_log WHERE policy_name = ? AND action = ? AND subject = ?
              AND CAST(happened_at AS DATE) = ?""", [WHO, action, f"basket:{request_id}", now.date()]))


def decide(db_path, request_id=None, now=None):
    """Offer the open baskets to Terminus. Gives back [(id, what it did or would do)]."""
    now = now or datetime.now()
    current = mode(db_path)
    if current == "off":
        return []
    rules = charters.limits(db_path, "terminus")
    done = []
    for request in gate.waiting(db_path):
        if request_id is not None and request["request_id"] != request_id:
            continue
        if str(request.get("channel") or "").startswith("po:"):
            continue                       # Plutus's own order: it goes through the process, and is read, not cancelled
        why = _cancel_reason(db_path, request, rules, now)
        if why:
            if not _seen_today(db_path, "cancel", request["request_id"], now):
                _log(db_path, "cancel", request, request.get("total"), [why], current == "live", now, current)
            if current == "live":
                gate.cancel(db_path, request["request_id"], WHO, why)
                audit.record(db_path, WHO, "terminus.cancel", f"request #{request['request_id']}", why)
            done.append((request["request_id"], "cancel"))
            continue
        blockers = _approve_blockers(db_path, request, rules, now)
        if blockers:
            if request.get("agent_verdict") and not _seen_today(db_path, "approve", request["request_id"], now):
                _log(db_path, "approve", request, request.get("total"), blockers, False, now, current)
            continue
        if not _seen_today(db_path, "approve", request["request_id"], now):
            _log(db_path, "approve", request, request.get("total"), [], current == "live", now, current)
        if current == "live":
            gate.approve(db_path, request["request_id"], WHO)
            audit.record(db_path, WHO, "terminus.approve", f"request #{request['request_id']}",
                         f"{request.get('total') or 0:.2f} EUR")
        done.append((request["request_id"], "approve"))
    return done
