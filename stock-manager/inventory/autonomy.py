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

from . import agents, gate, outbox, pricing, watch

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
         "Chat baskets and stored lists carry the need, but there is no request with a status",
         "Requisitions: a request for a work item, checked against stock first, with a status back"),
        ("Intake", "Supplier replies", 5, 3 if f["mailbox"] else 1,
         "Files a reply that names the order and takes the promised date" if f["mailbox"]
         else "Needs a mailbox to read replies",
         "Read prices and delivery notices out of replies; answer simple questions itself"),
        ("Planning", "Demand and buffers", 7, 4 if f["feedback"] else 3,
         "Sizes buffers from history and tunes them from stock-outs, short counts and the quantities "
         "people correct" if f["feedback"] else "Sizes buffers per article from history, with a demand outlook",
         "Add demand from the project plan" if f["feedback"]
         else "Tune buffers from the stock-outs that really happened, and from what people correct"),
        ("Planning", "Lead times", 4, 3,
         "Learns each supplier's real lead time and plans with it",
         "Score suppliers on promised against delivered and shift orders to the reliable ones"),
        ("Planning", "Replenishment", 8, 4 if f["policies"] and f["plutus"] else 3 if f["plutus"] else 2,
         "Plutus drafts orders from the numbers and orders the 'to order' lines after 15 days"
         if f["plutus"] else "Buffers say what to order, but Plutus needs the Claude command line",
         "Let policies release routine orders without a person" if not f["policies"]
         else "Close the loop: judge each order afterwards and adjust"),
        ("Planning", "Stock accuracy", 5, 3 if f["scanning"] else 2,
         "Says which articles to count next, a person counts",
         "Scanning, and finding the cause of a gap by itself"),
        ("Execution", "Placing orders", 14,
         4 if f["policies"] and f["smtp"] and f["order_step_walked"]
         else 3 if f["policies"] and f["smtp"] else 2 if f["smtp"] else 1,
         "Drafts the order and mails it when a person presses send" if f["smtp"]
         else "Drafts the order mail, but no outgoing mail is set up",
         "Policies: routine orders sent without a person, within limits and with a kill switch"
         if not f["policies"] else "Walk step 7 of the purchase round so a placed order is confirmed back"),
        ("Execution", "Chasing suppliers", 6, 3 if f["policies"] and f["smtp"] else 2,
         "Spots silent and late orders and drafts the reminder, a person sends it",
         "Send routine reminders by itself under a policy"),
        ("Execution", "Receiving and put-away", 4, 3 if f["scanning"] else 1,
         "Records what arrived; nothing scans, locates or puts away",
         "Scan-in against the order, with a location for each pile"),
        ("Execution", "Deliveries to sites", 4, 2,
         "Plans the delivery, prints the note and labels, records the signature",
         "Route and load it from the day's orders, and chase a delivery that was not signed"),
        ("Execution", "Quotes and awards", 4, 3 if hasattr(pricing, "from_reply") else 2,
         "Compares answers and awards one; the prices are typed in",
         "Send requests to several suppliers and read the prices out of the replies"),
        ("Control", "Spend control", 8, 4 if f["policies"] else 3,
         "Routine baskets pass, unusual ones are held for a person" if not f["policies"]
         else "Policies decide what passes, the rest is held",
         "Rules a person sets (supplier, ceiling, price band) that release routine buys on their own"
         if not f["policies"] else "Check each automatic pass afterwards against the delivered price"),
        ("Control", "Invoice matching", 4, 3,
         "Matches every bill to its deliveries and flags the gaps alone",
         "Pay or hold from the match, and tie credit notes back automatically"),
        ("Control", "Disputes", 2, 2,
         "Opens the dispute and drafts the mail, a person sends it and records the credit",
         "Send the dispute and read the credit note out of the reply"),
        ("Control", "Alerts", 6, 3 if f["push"] else 2,
         "Finds what needs a person and pushes it out" if f["push"]
         else "Finds what needs a person and lists it, nothing is pushed (NOTIFY_VIA is empty)",
         "Push to where the team is, and escalate what stays unanswered"),
        ("Control", "Audit trail", 3, 4,
         "Every change recorded; every purchase decision sealed in a hash chain",
         "Nothing more to add"),
        ("Control", "Payments", 2, 3 if f["payments"] else 0,
         "Nothing schedules or makes a payment",
         "A payment run from due dates, as a file for the bank"),
    ]


def measure(db_path=None, today=None, config=None):
    """The score and the table behind it. `db_path` and `today` are kept for the callers that
    pass them: nothing here depends on what was recorded."""
    facts = _facts(config)
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
