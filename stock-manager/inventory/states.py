"""Every life cycle in the app, written down once.

Many things here go through steps: a bill is to pay, then paid; a basket is waiting, checked,
approved, ordered. Each step is a word in a `state` column, and until now the words and the
allowed moves lived only inside the code that happened to change them. Nobody, person or agent,
could ask "what can happen to a dispatch after it is loaded?" without reading six files.

This module is that answer. It is a map, not a gate: the modules keep doing their own updates, so
nothing here can break them. Three things keep the map honest:

  * a test reads every state word the code writes and fails if the map does not know it;
  * a test checks each table's starting state against the map;
  * `move()` is there for new code, so a step is taken in one line and a wrong step is refused
    in plain words.

`python -m inventory.states` prints the map as markdown (that is STATES.md), and
GET /api/states serves it to the screen and to agents.
"""

from dataclasses import dataclass, field

from . import db


@dataclass(frozen=True)
class Machine:
    """One life cycle: where a row starts, which words it can have, and which moves are allowed."""
    table: str                      # the table the state lives in
    key: str                        # that table's id column
    title: str                      # what a person calls it
    about: str                      # one plain sentence on what the steps mean
    start: str                      # the state a new row gets
    steps: dict                     # state -> what it means, in plain words
    moves: dict = field(default_factory=dict)   # state -> the states it may go to next
    by_hand: bool = False           # True when a person may set any state directly (a drop-down)

    @property
    def ends(self):
        """The states nothing leaves: the row is finished."""
        return [state for state in self.steps if not self.moves.get(state)]


MACHINES = {m.table: m for m in (
    Machine("intake", "intake_id", "Files in the inbox",
            "A photo, PDF or mail attachment that has arrived and is waiting to be read.",
            "waiting",
            {"waiting": "arrived, not read yet or needs a person",
             "read": "turned into a delivery note, bill or quote",
             "dropped": "thrown away on purpose"},
            {"waiting": ["read", "dropped"], "read": ["waiting"], "dropped": ["waiting"]}, by_hand=True),

    Machine("gate_requests", "request_id", "Baskets needing approval",
            "A purchase basket on its way through Terminus's check, a person's approval and the order.",
            "waiting",
            {"waiting": "drafted, nobody has looked",
             "checked": "Terminus has read the names and prices",
             "approved": "a person (or a policy) said yes",
             "ordered": "the order went out",
             "cancelled": "turned down or taken back"},
            {"waiting": ["checked", "approved", "cancelled"], "checked": ["approved", "cancelled"],
             "approved": ["ordered", "cancelled"]}),

    Machine("order_queue", "queue_id", "The 15-day queue",
            "An article that will be ordered at the next sitting, not today.",
            "waiting",
            {"waiting": "queued", "ordered": "went into an order", "dropped": "no longer needed"},
            {"waiting": ["ordered", "dropped"]}),

    Machine("bills", "bill_id", "Bills",
            "A supplier's invoice.",
            "to pay",
            {"to pay": "received, not paid", "paid": "paid", "disputed": "something is wrong with it"},
            {"to pay": ["paid", "disputed"], "disputed": ["to pay", "paid"], "paid": ["to pay"]},
            by_hand=True),

    Machine("disputes", "dispute_id", "Bill disputes",
            "A claim against a supplier over a bill.",
            "open",
            {"open": "claimed, nothing back yet", "credited": "part of a credit note arrived",
             "closed": "the credit covers the claim, or it was dropped"},
            {"open": ["credited", "closed"], "credited": ["credited", "closed"]}),

    Machine("quotes", "quote_id", "Quote requests",
            "A request for a price sent to suppliers.",
            "open",
            {"open": "being prepared", "sent": "asked", "answered": "prices came back", "closed": "finished"},
            {"open": ["sent", "answered", "closed"], "sent": ["answered", "closed"],
             "answered": ["closed"]}, by_hand=True),

    Machine("dispatches", "dispatch_id", "Dispatches to sites",
            "Goods going from the shelf to a site on a van.",
            "planned",
            {"planned": "set for a day and a van", "loaded": "on the van",
             "delivered": "signed for, all of it", "part delivered": "signed for, some of it",
             "failed": "did not arrive"},
            {"planned": ["loaded", "delivered", "part delivered", "failed"],
             "loaded": ["delivered", "part delivered", "failed"]}),

    Machine("supplier_mail", "mail_id", "Mail with suppliers",
            "An order or question mail going out, or an answer coming in.",
            "draft",
            {"draft": "written, not sent", "failed": "sending did not work",
             "sent": "gone out", "confirmed": "the supplier confirmed",
             "received": "came in, not answered", "closed": "no longer waiting on it"},
            {"draft": ["sent", "failed", "closed"], "failed": ["sent", "draft", "closed"],
             "sent": ["confirmed", "closed"], "confirmed": ["closed"], "received": ["closed"]}),

    Machine("payment_batches", "batch_id", "Payment batches",
            "A set of bill payments handed to the bank together.",
            "open",
            {"open": "being put together", "exported": "file handed to the bank",
             "executed": "the bank paid", "rejected": "the bank refused it"},
            {"open": ["exported", "rejected"], "exported": ["executed", "open", "rejected"]}),

    Machine("payments", "payment_id", "Payments",
            "One bill's payment inside a batch.",
            "instructed",
            {"instructed": "in a batch, not paid", "executed": "paid", "cancelled": "stopped"},
            {"instructed": ["executed", "cancelled"]}),

    Machine("commitments", "commit_id", "Money set aside",
            "Wallet money held back for an order not yet paid.",
            "open",
            {"open": "held", "released": "given back or spent"},
            {"open": ["released"]}),

    Machine("agreements", "agreement_id", "Price agreements",
            "A price a supplier agreed to hold.",
            "active",
            {"active": "in force", "ended": "over"},
            {"active": ["ended"]}),

    Machine("reservations", "reservation_id", "Stock held back",
            "Shelf stock kept for a site or a job.",
            "active",
            {"active": "held", "released": "free again"},
            {"active": ["released"]}),

    Machine("requisitions", "req_id", "Site requests",
            "A site asking for material.",
            "open",
            {"open": "asked", "done": "supplied", "cancelled": "withdrawn"},
            {"open": ["done", "cancelled"]}),

    Machine("putaways", "putaway_id", "Put-away tasks",
            "Delivered goods waiting for a shelf place.",
            "to put away",
            {"to put away": "on the floor", "put away": "on its shelf"},
            {"to put away": ["put away"]}),

    Machine("count_tasks", "article", "Count tasks",
            "An article someone is asked to count today.",
            "open",
            {"open": "to count", "done": "counted"},
            {"open": ["done"]}),

    Machine("count_checks", "check_id", "Recounts",
            "A second count of an article whose first count looked wrong.",
            "recount",
            {"recount": "waiting for the second count", "confirmed": "the two counts agree",
             "disputed": "they still differ", "settled": "a person decided"},
            {"recount": ["confirmed", "disputed"], "disputed": ["settled"]}),
)}


def machine(table):
    """The life cycle of a table, or a plain error naming the ones that exist."""
    if table not in MACHINES:
        raise ValueError(f"No life cycle is written down for '{table}'. Known: {', '.join(sorted(MACHINES))}.")
    return MACHINES[table]


def allowed(table, now, then):
    """May a row of this table go from `now` to `then`?"""
    cycle = machine(table)
    return then in cycle.moves.get(now, ()) or (cycle.by_hand and then in cycle.steps)


def check(table, now, then):
    """Say no, in plain words, to a move the map does not allow."""
    cycle = machine(table)
    if then not in cycle.steps:
        raise ValueError(f"'{then}' is not a step of {cycle.title.lower()}. They are: {', '.join(cycle.steps)}.")
    if not allowed(table, now, then):
        nexts = cycle.moves.get(now)
        raise ValueError(f"{cycle.title}: from '{now}' it can go to {', '.join(nexts)}, not '{then}'."
                         if nexts else f"{cycle.title}: '{now}' is the end, nothing follows it.")


def move(db_path, table, row_id, then, sets="", args=()):
    """Take one step, only if the row is in a state that may take it. True if it moved.

    `sets` is extra SQL to set in the same update, for example "closed_at = now()", with its
    `args`. The update names the allowed starting states, so two people clicking at once cannot
    both win."""
    cycle = machine(table)
    if then not in cycle.steps:
        check(table, cycle.start, then)                     # names the real steps in the error
    starts = [s for s in cycle.steps if allowed(table, s, then)]
    if not starts:
        raise ValueError(f"{cycle.title}: nothing may go to '{then}'.")
    marks = ", ".join("?" for _ in starts)
    extra = f", {sets}" if sets else ""
    done = db.write(db_path, f"UPDATE {table} SET state = ?{extra} "
                             f"WHERE {cycle.key} = ? AND state IN ({marks}) "
                             f"RETURNING {cycle.key}", [then, *args, row_id, *starts])
    return done is not None


def describe():
    """The whole map as plain data, for the screen and for agents."""
    return [{"table": m.table, "title": m.title, "about": m.about, "start": m.start,
             "by_hand": m.by_hand, "ends": m.ends,
             "steps": [{"state": s, "meaning": text, "next": list(m.moves.get(s, ()))}
                       for s, text in m.steps.items()]} for m in MACHINES.values()]


def markdown():
    """STATES.md: the same map, for a person or an agent reading the repository."""
    out = ["# Life cycles", "",
           "Every table with a `state` column, its steps and the moves between them. Written by",
           "`python -m inventory.states > STATES.md` from `inventory/states.py`; do not edit by hand.", ""]
    for m in MACHINES.values():
        out += [f"## {m.title} (`{m.table}`)", "", m.about, ""]
        if m.by_hand:
            out += ["A person can set any of these directly.", ""]
        for state, text in m.steps.items():
            nexts = m.moves.get(state)
            tag = " (start)" if state == m.start else ""
            out.append(f"- **{state}**{tag}: {text}. " + (f"Next: {', '.join(nexts)}." if nexts else "End."))
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    print(markdown())
