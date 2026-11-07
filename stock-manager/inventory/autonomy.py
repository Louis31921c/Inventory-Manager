"""How autonomous the software is as a logistics manager, as a map to build from.

This is about the architecture, not about what anybody did today. The job is split into
nineteen capabilities, each worth a share of 100 points, and each is rated on a five-step
ladder of how much of it the software does without a person:

  0  not built          nothing here does it
  1  records only       it stores the data, a person does the work
  2  assisted           it prepares the work, a person carries it out
  3  supervised         it acts alone on the routine cases, a person takes the exceptions
  4  autonomous         it acts, checks itself and escalates only by exception

A capability's points are its weight times its level over four. Some levels depend on what is
switched on (a mailbox, an outgoing mail server, a way to push alerts, the Claude command line)
and some on what has been built (a module that does not exist yet, a step of the purchase
round that is still not walked), so the figure moves when the system does, and each line says
what would raise it. The lines are ranked by the points still to win.
"""

import importlib.util

from . import agents, channels, gate, outbox, pricing, watch

LEVELS = ("not built", "records only", "assisted", "supervised", "autonomous")


def _built(name):
    """Whether a module of this name has been written yet."""
    return importlib.util.find_spec(f"{__package__}.{name}") is not None


def _facts(config):
    mail = config.mail if config is not None else None
    return {
        "mailbox": bool(mail and mail.enabled),
        "auto_read": bool(mail and mail.enabled and mail.auto_read),
        "smtp": bool(mail and outbox.readiness(mail)["ready"]),
        "push": bool(watch.channels()),
        "plutus": agents.available(),
        "policies": _built("policies"),
        "requisitions": _built("requisitions"),
        "scanning": _built("scanning"),
        "payments": _built("payments"),
        "projects": _built("projects"),
        "feedback": _built("feedback"),
        "replenish": _built("replenish"),
        "judge": _built("judge"),
        "replies": _built("replies") and agents.available(),
        "webhooks": False,                      # set in measure(): it depends on a supplier being set up
        "bank_drop": _built("bankdrop"),
        "counting": _built("counting"),
        "receiving": _built("receiving"),

        "order_step_walked": 7 not in getattr(gate, "UNBUILT", (7,)),
    }


def _capabilities(f):
    """(area, name, weight, level, why, next step) for every capability, given the facts."""
    return [
        ("Intake", "Delivery notes", 6,
         3 if f["auto_read"] else 2,
         "Reads photos, PDFs and sheets; stores clean notes from the mailbox alone" if f["auto_read"]
         else "Reads photos, PDFs and sheets, a person confirms each note (no mailbox set up)",
         "Feed corrections back so more notes read clean; connect a mailbox" if not f["auto_read"]
         else "Learn from corrections to raise the clean-note rate and settle unknown articles itself"),
        ("Intake", "Supplier bills", 4, 2,
         "Reads a bill, a person confirms it",
         "Store bills from the mailbox alone when they match a delivery"),
        ("Intake", "Site requests", 4, 3 if f["requisitions"] else 1,
         "A request for a work item holds back what the site has, orders the rest through the purchase round "
         "and reports each line's state" if f["requisitions"]
         else "Chat baskets and stored lists carry the need, but there is no request with a status",
         "Tell the requester in the chat as it moves, and learn the requests that come back every week"
         if f["requisitions"]
         else "Requisitions: a request for a work item, checked against stock first, with a status back"),
        ("Intake", "Supplier replies", 5,
         4 if f["mailbox"] and f["replies"] else 3 if f["mailbox"] else 1,
         "Reads a reply for its new date, its prices and its credit note and files them; a question waits for "
         "a person" if f["mailbox"] and f["replies"]
         else "Files a reply that names the order and takes the promised date" if f["mailbox"]
         else "Needs a mailbox to read replies",
         "Answer the simple questions itself" if f["mailbox"] and f["replies"]
         else "Read prices and delivery notices out of replies (needs the Claude command line)"),
        ("Planning", "Demand and buffers", 7, 4 if f["feedback"] else 3,
         "Sizes buffers from history and tunes them from stock-outs, short counts and the quantities "
         "people correct" if f["feedback"] else "Sizes buffers per article from history, with a demand outlook",
         "Add demand from the project plan" if f["feedback"]
         else "Tune buffers from the stock-outs that really happened, and from what people correct"),
        ("Planning", "Lead times", 4, 3,
         "Learns each supplier's real lead time and plans with it",
         "Score suppliers on promised against delivered and shift orders to the reliable ones"),
        ("Planning", "Replenishment", 8,
         4 if f["replenish"] and f["policies"] else 3 if f["replenish"] else 2,
         "Plutus writes the order the DDMRP buffers ask for, by itself, every hour" if f["replenish"]
         else "Buffers say what to order, a person writes the order",
         "Judge each order afterwards against what arrived and adjust" if f["replenish"] and f["policies"]
         else "Let policies release routine orders without a person"),
        ("Planning", "Stock accuracy", 5, 3 if f["scanning"] or f["counting"] else 2,
         "Puts a few articles on the count list every day, takes a blind count, asks for a second one when the "
         "gap is large and looks for the cause" if f["counting"]
         else "Says which articles to count next, a person counts",
         "Count by scan or photo instead of by hand" if f["counting"]
         else "Scanning, and finding the cause of a gap by itself"),
        ("Execution", "Placing orders", 14,
         4 if f["policies"] and (f["smtp"] or f["webhooks"]) and f["order_step_walked"]
         else 3 if f["policies"] and (f["smtp"] or f["webhooks"]) else 2 if f["smtp"] else 1,
         "Drafts the order and mails it when a person presses send" if f["smtp"]
         else "Drafts the order mail, but no outgoing mail is set up",
         "Policies: routine orders sent without a person, within limits and with a kill switch"
         if not f["policies"] else "Order by mail or by a supplier's webhook; a shop checkout is not driven"),
        ("Execution", "Chasing suppliers", 6, 4 if f["policies"] and f["smtp"] and f["replies"]
         else 3 if f["policies"] and f["smtp"] else 2,
         "Chases a silent or late order and reminds a supplier of what it owes, twice at most, then a person"
         if f["policies"] and f["smtp"]
         else "Spots silent and late orders and drafts the reminder, a person sends it",
         "Escalate by phone or to a second contact when a supplier stays silent"
         if f["policies"] and f["smtp"] else "Send routine reminders by itself under a policy"),
        ("Execution", "Receiving and put-away", 4, 3 if f["scanning"] or f["receiving"] else 1,
         "Checks every delivered line against the orders, suggests where it goes and learns the place; a person "
         "confirms it" if f["receiving"] else "Records what arrived; nothing scans, locates or puts away",
         "Scan the pile on the dock instead of typing the place" if f["receiving"]
         else "Scan-in against the order, with a location for each pile"),
        ("Execution", "Deliveries to sites", 4, 2,
         "Plans the delivery, prints the note and labels, records the signature",
         "Route and load it from the day's orders, and chase a delivery that was not signed"),
        ("Execution", "Quotes and awards", 4, 3 if hasattr(pricing, "from_reply") else 2,
         "Compares answers and awards one; the prices are typed in",
         "Send requests to several suppliers and read the prices out of the replies"),
        ("Control", "Spend control", 8, 4 if (f["policies"] or f["judge"]) else 3,
         "Routine baskets pass, unusual ones are held for a person" if not (f["policies"] or f["judge"])
         else "Policies and Terminus decide what passes and what is cancelled, the rest is held for a person",
         "Rules a person sets (supplier, ceiling, price band) that release routine buys on their own"
         if not f["policies"] else "Check each automatic pass afterwards against the delivered price"),
        ("Control", "Invoice matching", 4, 3,
         "Matches every bill to its deliveries and flags the gaps alone",
         "Pay or hold from the match, and tie credit notes back automatically"),
        ("Control", "Disputes", 2, 4 if f["policies"] and f["smtp"] and f["replies"] else 3 if f["policies"] and f["smtp"] else 2,
         "Opens the dispute, mails it and records the credit note from the reply, inside its limits"
         if f["policies"] and f["smtp"] and f["replies"]
         else "Opens the dispute and drafts the mail, a person sends it and records the credit",
         "Escalate a dispute that stays unanswered" if f["policies"] and f["smtp"]
         else "Send the dispute and read the credit note out of the reply"),
        ("Control", "Alerts", 6, 3 if f["push"] else 2,
         "Finds what needs a person and pushes it out" if f["push"]
         else "Finds what needs a person and lists it, nothing is pushed (NOTIFY_VIA is empty)",
         "Push to where the team is, and escalate what stays unanswered"),
        ("Control", "Audit trail", 3, 4,
         "Every change recorded; every purchase decision sealed in a hash chain",
         "Nothing more to add"),
        ("Control", "Payments", 2, 4 if f["payments"] and f["bank_drop"] else 3 if f["payments"] else 0,
         "Plutus instructs matched bills from its wallet into a bank file, hands it to the bank by SFTP and "
         "settles the bills from the bank's report" if f["payments"] and f["bank_drop"]
         else "Plutus instructs matched bills from its wallet into a bank file; a person uploads it, or a "
         "connector would send it" if f["payments"] else "Nothing schedules or makes a payment",
         "Reconcile against the bank statement, not only the batch report"
         if f["payments"] and f["bank_drop"]
         else "A bank connector, so the file is sent and the bills confirmed without a person"
         if f["payments"] else "A payment run from due dates, as a file for the bank"),
    ]


def measure(db_path=None, today=None, config=None):
    """The score and the table behind it. `db_path` and `today` are kept for the callers that
    pass them: nothing here depends on what was recorded."""
    facts = _facts(config)
    if db_path is not None:
        try:
            facts["webhooks"] = bool(channels.listing(db_path))
        except Exception:
            pass
    rows, score = [], 0.0
    for area, name, weight, level, why, nxt in _capabilities(facts):
        points = weight * level / 4
        score += points
        rows.append({"area": area, "capability": name, "weight": weight, "level": level,
                     "level_name": LEVELS[level], "points": round(points, 2),
                     "to_win": round(weight - points, 2), "why": why,
                     "next": nxt if level < 4 else None})
    areas = {}
    for row in rows:
        held = areas.setdefault(row["area"], {"area": row["area"], "weight": 0, "points": 0.0})
        held["weight"] += row["weight"]
        held["points"] += row["points"]
    ranked = sorted((r for r in rows if r["to_win"] > 0), key=lambda r: -r["to_win"])
    return {"score": round(score, 2), "parts": rows,
            "areas": [{**a, "points": round(a["points"], 2)} for a in areas.values()],
            "next_steps": [{"capability": r["capability"], "to_win": r["to_win"], "step": r["next"]}
                           for r in ranked[:6]],
            "levels": list(LEVELS)}
