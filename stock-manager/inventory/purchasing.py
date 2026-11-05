"""Plutus runs the purchasing: send, approve, place, pay.

replenish.py is where Plutus decides what to buy and writes the purchase order. This is the rest
of the job, the part that used to wait for a person:

  send     a purchase order Plutus wrote goes to its suppliers, as soon as it is within the limits
  approve  a basket the gate holds, that Terminus read as the same article, is approved
  place    an approved basket is turned into an order with its supplier (step 7 of the round)
  pay      a supplier bill that is matched to its deliveries is paid, a few days before it is due

Everything is bounded by the wallet (step 6): an order is sent only if the wallet has the money free,
and sending it commits that money; a bill is paid only if the wallet holds it. Beyond the wallet
there are Plutus's own limits (charters.py), and above both a mode that starts off, then dry run,
which writes down what would have been done and does nothing, and goes live only after three days
of that. Each decision is in the policy log under PLUTUS, with its reasons.

Whatever fails a check is left for a person, with the reasons shown. Nothing is retried in a way
that could send twice: an order that has gone is no longer a draft, a basket that is placed is no
longer approved, a bill that has a payment is no longer to pay.
"""

import asyncio
from datetime import date, datetime, timedelta

from . import (audit, charters, db, gate, judge, matching, outbox, payments, policies, prices,
               verify, wallet, workdays)
from .policies import MIN_DRY_DAYS

MODES = ("off", "dry_run", "live")
WHO = "PLUTUS"


def _setting(db_path, key):
    rows = db.read(db_path, "SELECT value FROM policy_settings WHERE key = ?", [key])
    return rows[0][0] if rows else None


def _put(db_path, key, value):
    db.write(db_path, "INSERT INTO policy_settings (key, value) VALUES (?, ?) "
                      "ON CONFLICT (key) DO UPDATE SET value = excluded.value RETURNING key", [key, value])


def mode(db_path):
    return _setting(db_path, "plutus_mode") or "off"


def status(db_path, today=None):
    since = _setting(db_path, "plutus_dry_since")
    days = (((today or date.today()) - date.fromisoformat(since)).days if since else 0)
    return {"mode": mode(db_path), "dry_days": days, "may_go_live": days >= MIN_DRY_DAYS,
            "min_dry_days": MIN_DRY_DAYS, "limits": charters.limits(db_path, "plutus")}


def set_mode(db_path, new, user=None, today=None):
    if new not in MODES:
        raise ValueError("The mode is off, dry_run or live.")
    now = status(db_path, today)
    if new == "live" and not now["may_go_live"]:
        raise ValueError(f"Plutus runs {MIN_DRY_DAYS} days in dry run before it can spend for real "
                         f"({now['dry_days']} so far).")
    if new == "dry_run" and now["mode"] == "off":
        _put(db_path, "plutus_dry_since", (today or date.today()).isoformat())
    if new == "off":
        _put(db_path, "plutus_dry_since", "")
    _put(db_path, "plutus_mode", new)
    audit.record(db_path, user or WHO, "plutus.mode", "plutus", new)
    return status(db_path, today)


# ---- the log ----------------------------------------------------------------

def _spent_today(db_path, action, now):
    return float(db.read(db_path, """
            SELECT coalesce(sum(amount), 0) FROM policy_log WHERE policy_name = ? AND action = ?
              AND decision = 'allowed' AND CAST(happened_at AS DATE) = ?""", [WHO, action, now.date()])[0][0])


def _seen_today(db_path, action, subject, decision, now, reasons=None):
    """Already logged today. A refusal for a new reason is a new entry, the same reason is not repeated."""
    return bool(db.read(db_path, """
            SELECT 1 FROM policy_log WHERE policy_name = ? AND action = ? AND subject = ? AND decision = ?
              AND CAST(happened_at AS DATE) = ? AND reasons IS NOT DISTINCT FROM ?""",
                        [WHO, action, subject, decision, now.date(), "; ".join(reasons) if reasons else None]))


def _log(db_path, action, subject, supplier, amount, reasons, acted, now, current):
    db.write(db_path, """
            INSERT INTO policy_log (happened_at, policy_id, policy_name, action, subject, supplier, amount,
                                    decision, reasons, mode, acted)
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING log_id""",
             [now, WHO, action, subject, supplier, amount, "denied" if reasons else "allowed",
              "; ".join(reasons) or None, current, acted])


def _offer(db_path, action, subject, supplier, amount, reasons, now, current, act):
    """Log a decision, and when it is allowed and the mode is live, do it. Gives back whether it was done."""
    if reasons:
        if not _seen_today(db_path, action, subject, "denied", now, reasons):
            _log(db_path, action, subject, supplier, amount, reasons, False, now, current)
        return False
    if current != "live":
        if not _seen_today(db_path, action, subject, "allowed", now):
            _log(db_path, action, subject, supplier, amount, [], False, now, current)
        return False
    try:
        act()
    except Exception as e:                           # a refusal is a refusal, not a crash
        _log(db_path, action, subject, supplier, amount, [f"it failed: {e}"], False, now, current)
        return False
    _log(db_path, action, subject, supplier, amount, [], True, now, current)
    audit.record(db_path, WHO, f"plutus.{action}", subject, f"{amount:.2f} EUR" if amount else None)
    return True


# ---- send -------------------------------------------------------------------

_WINDOW = {"hours_from": 8, "hours_to": 17}


def check_send(db_path, mail, now, rules):
    """(reasons it may not be sent, value in euros) for a drafted order."""
    reasons = policies._window(_WINDOW, now, workdays.Calendar(db_path, mail["supplier"]))
    if not rules["send_orders"]:
        reasons.append("sending orders is switched off in its limits")
    if not (mail.get("to_addr") or "").strip():
        reasons.append("the draft has no address")
    value, unchecked, drift = policies.order_value(db_path, mail, now.date())
    reasons += unchecked
    if not unchecked and value <= 0:
        reasons.append("the order comes to nothing")
    for article, moved in drift:
        if abs(moved) > 0.10:
            reasons.append(f"the price of {article} is {moved * 100:+.0f}% off the reference")
    if policies._delivered(db_path, mail["supplier"]) < rules["send_min_deliveries"]:
        reasons.append(f"{mail['supplier']} has delivered fewer than {rules['send_min_deliveries']:g} times")
    if value > rules["send_max_total"]:
        reasons.append(f"{value:.2f} EUR is over the {rules['send_max_total']:g} a person must send")
    if _spent_today(db_path, "send_order", now) + value > rules["send_per_day"]:
        reasons.append(f"it would pass {rules['send_per_day']:g} EUR sent today")
    if value > wallet.available(db_path, rules["wallet_reserve"]) + 1e-9:
        reasons.append(f"the wallet has {wallet.available(db_path, rules['wallet_reserve']):.2f} EUR free")
    return reasons, value


def _po_of(request):
    """The purchase order a gate request stands for, or None when it is a chat basket."""
    channel = str(request.get("channel") or "")
    return int(channel[3:]) if channel.startswith("po:") and channel[3:].isdigit() else None


def _open_requests(db_path, now, current, rules):
    """Step 1 and 2: an order Plutus wrote enters the gate, like any basket, so it leaves a sealed trail."""
    opened = 0
    have = {_po_of(r) for r in gate.history(db_path, 500)} - {None}
    for list_id, site, work in db.read(db_path, """
            SELECT list_id, site, work_item FROM purchase_orders
            WHERE upper(coalesce(drafter, '')) = 'PLUTUS' AND created_at >= now() - INTERVAL 3 DAY
            ORDER BY list_id"""):
        if list_id in have:
            continue

        def go(i=list_id, st=site, wk=work):
            rows = [{"article": a, "quantity": float(q), "supplier": outbox.supplier_of(db_path, a),
                     "unit_price": (prices.sightings(db_path, a, 1) or [None])[0]}
                    for a, q in db.read(db_path, "SELECT article, quantity FROM purchase_order_lines "
                                                 "WHERE list_id = ? AND in_stock IS NOT TRUE ORDER BY line_no", [i])]
            gate.ask(db_path, gate.look(db_path, rows), channel=f"po:{i}", shop=f"PO #{i}",
                     asked_by=WHO, site=st, work_item=wk)
        opened += _offer(db_path, "open_gate", f"po:{list_id}", None, None, [], now, current, go)
    return opened


def _terminus_reads(db_path, now, current):
    """Step 3: Terminus reads each order for sense before Plutus may approve it."""
    read = 0
    for request in gate.waiting(db_path):
        list_id = _po_of(request)
        if list_id is None or request.get("agent_verdict") is not None:
            continue
        answer = asyncio.run(verify.check_order(db_path, list_id, now.date()))
        gate.record_check(db_path, request["request_id"], answer["verdict"], answer["note"], answer["lines"])
        read += 1
    return read


def _send_orders(db_path, config, now, current, rules, connect):
    """Step 6 and 7 for an order Plutus wrote: once it is approved, send it from the wallet."""
    sent = 0
    ready = outbox.readiness(config.mail)["ready"]
    for request in [r for r in gate.history(db_path, 200) if r["state"] == "approved"]:
        list_id = _po_of(request)
        if list_id is None:
            continue
        drafts = outbox.draft_order(db_path, list_id, WHO)["drafts"]
        for row in drafts:
            if row["state"] != "draft":
                continue
            reasons, value = check_send(db_path, row, now, rules)
            if not ready:
                reasons.append("outgoing mail is not set up")

            def go(r=row, v=value):
                wallet.commit(db_path, r["supplier"], v, "po", r["ref_id"], rules["wallet_reserve"], WHO)
                try:
                    outbox.send(db_path, r["mail_id"], config.mail, WHO,
                                **({"connect": connect} if connect else {}))
                except Exception:
                    db.write(db_path, "UPDATE commitments SET state = 'released', released_on = current_date "
                                      "WHERE ref_kind = 'po' AND ref_id = ? AND supplier = ? AND state = 'open' "
                                      "RETURNING 1", [r["ref_id"], r["supplier"]])
                    raise
            sent += _offer(db_path, "send_order", f"mail:{row['mail_id']}", row["supplier"], value, reasons,
                           now, current, go)
        fresh = outbox.draft_order(db_path, list_id, WHO)["drafts"]
        if fresh and all(r["state"] == "sent" for r in fresh):
            gate.mark_ordered(db_path, request["request_id"], WHO, reference=f"PO #{list_id}")
    return sent


# ---- approve and place baskets ------------------------------------------------

def _approve_baskets(db_path, now, current, rules):
    approved = 0
    mine = {"approve_max_total": rules["approve_max_total"], "approve_per_day": rules["approve_per_day"],
            "allow_dear_line": rules["allow_dear_line"], "allow_new_supplier": rules["allow_new_supplier"],
            "allow_no_price": rules["allow_no_price"]}
    terminus = charters.limits(db_path, "terminus")
    for request in gate.waiting(db_path):
        own = _po_of(request) is not None
        if not own and judge._cancel_reason(db_path, request, terminus, now):
            continue                                     # a repeat or a refused article is Terminus's to cancel
        lim = (dict(mine, approve_max_total=rules["send_max_total"], approve_per_day=rules["send_per_day"])
               if own else mine)                         # its own order: the order limits are the limits
        reasons = []
        if not rules["approve_baskets"]:
            reasons.append("approving baskets is switched off in its limits")
        if request.get("agent_verdict") is None:
            continue                                     # Terminus has not read it yet
        reasons += judge._approve_blockers(db_path, request, dict(terminus, **lim), now)
        total = float(request.get("total") or 0)
        if _spent_today(db_path, "approve", now) + total > lim["approve_per_day"]:
            reasons.append(f"it would pass {lim['approve_per_day']:g} EUR approved today")
        if total > wallet.available(db_path, rules["wallet_reserve"]) + 1e-9:
            reasons.append(f"the wallet has {wallet.available(db_path, rules['wallet_reserve']):.2f} EUR free")
        reasons += policies._window(_WINDOW, now, workdays.Calendar(db_path))
        reasons = list(dict.fromkeys(reasons))
        approved += _offer(db_path, "approve", f"basket:{request['request_id']}", None, total, reasons, now,
                           current, lambda r=request: gate.approve(db_path, r["request_id"], WHO))
    return approved


def place_basket(db_path, request, config, rules, connect=None):
    """Turn an approved basket into an order with its supplier(s) and mark the request ordered."""
    ok, why = gate.may_order(db_path, request["request_id"])
    if not ok:
        raise ValueError(why)
    lines = [dict(l) for l in request["lines"] if l.get("article") and l.get("quantity")]
    if not lines:
        raise ValueError("the basket has no line to order")
    for line in lines:                                   # the basket names the supplier and the price
        if line.get("unit_price") is not None and line.get("supplier"):
            prices.remember(db_path, line["article"], line["unit_price"], supplier=line["supplier"],
                            source="basket")
    list_id = db.save_purchase_order(db_path, {
        "list_date": date.today(), "site": request.get("site"), "work_item": request.get("work_item"),
        "drafter": WHO, "lines": [{"article": l["article"], "quantity": float(l["quantity"]),
                                   "designation": f"basket #{request['request_id']}", "in_stock": False}
                                  for l in lines]}, None, None, WHO)
    made = outbox.draft_order(db_path, list_id, WHO)
    if made["unassigned"] or not made["drafts"]:
        raise ValueError(f"no supplier is known for {', '.join(made['unassigned']) or 'the basket'}")
    for draft in made["drafts"]:
        if not (draft.get("to_addr") or "").strip():
            raise ValueError(f"no address for {draft['supplier']}")
    ready = outbox.readiness(config.mail)
    if not ready["ready"]:
        raise ValueError(ready["detail"])
    for draft in made["drafts"]:
        value = sum(float(l.get("unit_price") or 0) * float(l["quantity"]) for l in lines
                    if (l.get("supplier") or "").upper() == draft["supplier"].upper()) or float(request["total"] or 0)
        wallet.commit(db_path, draft["supplier"], value, "po", list_id, rules["wallet_reserve"], WHO)
        outbox.send(db_path, draft["mail_id"], config.mail, WHO, **({"connect": connect} if connect else {}))
    gate.mark_ordered(db_path, request["request_id"], WHO, reference=f"PO #{list_id}")
    return list_id


def _place_baskets(db_path, config, now, current, rules, connect):
    placed = 0
    for request in [r for r in gate.history(db_path, 100) if r["state"] == "approved" and _po_of(r) is None]:
        subject = f"basket:{request['request_id']}"
        reasons = []
        if not rules["send_orders"]:
            reasons.append("sending orders is switched off in its limits")
        total = float(request.get("total") or 0)
        if total > wallet.available(db_path, rules["wallet_reserve"]) + 1e-9:
            reasons.append(f"the wallet has {wallet.available(db_path, rules['wallet_reserve']):.2f} EUR free")
        reasons += policies._window(_WINDOW, now, workdays.Calendar(db_path))
        placed += _offer(db_path, "place_basket", subject, None, total, reasons, now, current,
                         lambda r=request: place_basket(db_path, r, config, rules, connect))
    return placed


# ---- pay --------------------------------------------------------------------

def _pay_bills(db_path, now, current, rules):
    paid = 0
    today = now.date()
    for row in matching.bills(db_path, today)["bills"]:
        if row["state"] != "to pay":
            continue
        bill = db.read(db_path, "SELECT due_date, bill_date, supplier FROM bills WHERE bill_id = ?", [row["bill_id"]])[0]
        due = bill[0] or (bill[1] + timedelta(days=30))
        amount = float(row["billed"] or 0)
        subject = f"bill:{row['bill_id']}"
        reasons = []
        if not rules["pay_bills"]:
            reasons.append("paying bills is switched off in its limits")
        if row["verdict"] != "matched":
            reasons.append(f"the bill is not matched to its deliveries ({row['verdict']})")
        if today < due - timedelta(days=int(rules["pay_days_before_due"])):
            reasons.append(f"not yet near its due date, {due.isoformat()}")
        if payments.bank_for(db_path, row["supplier"]) is None:
            reasons.append(f"no bank details for {row['supplier']}")
        if amount <= 0:
            reasons.append("the bill comes to nothing")
        if amount > rules["pay_max_total"]:
            reasons.append(f"{amount:.2f} EUR is over the {rules['pay_max_total']:g} a person must pay")
        if _spent_today(db_path, "pay_bill", now) + amount > rules["pay_per_day"]:
            reasons.append(f"it would pass {rules['pay_per_day']:g} EUR paid today")
        if amount > wallet.balance(db_path) - float(rules["wallet_reserve"]) + 1e-9:
            reasons.append(f"the wallet holds {wallet.balance(db_path):.2f} EUR")
        reasons += policies._window(_WINDOW, now, workdays.Calendar(db_path, row["supplier"]))
        reasons = list(dict.fromkeys(reasons))
        paid += _offer(db_path, "pay_bill", subject, row["supplier"], amount, reasons, now, current,
                       lambda b=row["bill_id"], a=amount: payments.instruct(db_path, b, a, WHO))
    return paid


# ---- the run ----------------------------------------------------------------

def run(db_path, config, now=None, connect=None):
    """What the server does every hour once Plutus is on: send, approve, place, pay."""
    now = now or datetime.now()
    current = mode(db_path)
    if current == "off":
        return {"skipped": "Plutus is off", "done": 0}
    rules = charters.limits(db_path, "plutus")
    # The blockchain process, for an order Plutus wrote as for any basket: it enters the gate, Terminus
    # reads it for sense, Plutus approves what is inside its limits, the wallet covers it, it is sent.
    done = {"opened": _open_requests(db_path, now, current, rules)}
    if current == "live":
        done["read"] = _terminus_reads(db_path, now, current)
    done["approved"] = _approve_baskets(db_path, now, current, rules)
    done["sent"] = _send_orders(db_path, config, now, current, rules, connect)
    done["placed"] = _place_baskets(db_path, config, now, current, rules, connect)
    done["paid"] = _pay_bills(db_path, now, current, rules)
    done["done"] = sum(done.values())
    return done


# ---- what a check stopped ------------------------------------------------------

def _pending(db_path, subject):
    """Whether what a refused decision was about is still waiting for somebody."""
    kind, _, ident = subject.partition(":")
    if kind == "mail" and ident.isdigit():
        return bool(db.read(db_path, "SELECT 1 FROM supplier_mail WHERE mail_id = ? AND state IN ('draft', 'failed')",
                            [int(ident)]))
    if kind == "basket" and ident.isdigit():
        return bool(db.read(db_path, "SELECT 1 FROM gate_requests WHERE request_id = ? "
                                     "AND state IN ('waiting', 'checked', 'approved')", [int(ident)]))
    if kind == "bill" and ident.isdigit():
        return bool(db.read(db_path, "SELECT 1 FROM bills b WHERE bill_id = ? AND state = 'to pay' AND NOT EXISTS "
                                     "(SELECT 1 FROM payments p WHERE p.bill_id = b.bill_id "
                                     "AND p.state IN ('instructed', 'executed'))", [int(ident)]))
    if kind == "po":
        return bool(db.read(db_path, "SELECT 1 FROM gate_requests WHERE channel = ? AND state IN "
                                     "('waiting', 'checked', 'approved')", [subject]))
    return False


def blocked(db_path):
    """Everything a check has stopped and nobody has settled: the latest refusal for each subject, as long
    as it is still the latest word and the thing is still waiting."""
    rows = db.read(db_path, """
            SELECT l.policy_name, l.action, l.subject, l.supplier, l.amount, l.reasons, l.mode, l.happened_at
            FROM policy_log l
            WHERE l.decision = 'denied' AND l.policy_name IN ('PLUTUS', 'TERMINUS')
              AND l.log_id = (SELECT max(m.log_id) FROM policy_log m WHERE m.policy_name = l.policy_name
                              AND m.action = l.action AND m.subject = l.subject)
            ORDER BY l.log_id DESC""")
    out = []
    for who, action, subject, supplier, amount, reasons, current, at in rows:
        if _pending(db_path, subject):
            out.append({"who": who, "action": action, "subject": subject, "supplier": supplier,
                        "amount": amount, "reasons": reasons, "mode": current,
                        "at": at.isoformat(timespec="minutes")})
    # A request Terminus read as not coherent is stopped too, whatever the log says.
    for request_id, shop, said in db.read(db_path, """
            SELECT request_id, shop, agent_said FROM gate_requests
            WHERE channel LIKE 'po:%' AND state IN ('waiting', 'checked') AND agent_verdict = 'unsure'"""):
        out.append({"who": "TERMINUS", "action": "coherence", "subject": f"basket:{request_id}",
                    "supplier": None, "amount": None, "reasons": said, "mode": "live",
                    "at": datetime.now().isoformat(timespec="minutes"), "order": shop})
    return out
