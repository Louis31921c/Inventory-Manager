"""What each of the two bots may do on its own, and what it may never do.

Plutus buys and Terminus reads names. Each has a charter in two halves. The freedom is what
it is trusted to decide itself. The limits are numbers a person sets. Both go to the model
in its instructions, so it knows where it stands, and then the limits are enforced again in
code on whatever it answers: a model that ignores its instructions changes nothing, because
what it wrote is cut back to the limits before anybody sees it.

  Plutus     the buyer, and autonomous. It checks the inventory and the DDMRP buffers, writes the
             purchase orders on a rhythm (one big order each cycle, a small urgent one between), sends
             them to the suppliers, approves and places baskets, and pays matched bills. It spends from a
             wallet a person funds, inside limits, and starts in dry run.
  Terminus   the gate keeper. It reads names, and then decides the baskets the gate holds: it approves
             when every line is the same article and the limits allow, and cancels a repeat, a
             twice-refused article or a stale basket. A doubt is "unsure", which leaves the basket
             to a person. It never touches orders or prices.
"""

import os
import re
from pathlib import Path

from . import db, prices, units
from .models import normalize_name

# The built-in charters. They are what the bots follow when no rules folder is set, and what a
# file falls back to for anything it leaves out. With BOT_RULES_DIR set, the folder's plutus.md and
# terminus.md are read instead, so the rules can be edited as plain text.
# (default, what it means). Stored as text; the second is the sentence the screen shows.
PLUTUS = {
    "send_orders": (1, "send the orders it writes to their suppliers by itself (1 yes, 0 no: a person sends them)"),
    "send_max_total": (5000.0, "EUR: the most one order to one supplier may come to before a person has to send it"),
    "send_per_day": (20000.0, "EUR: the most it sends to suppliers in a day"),
    "send_min_deliveries": (1, "it sends only to a supplier that has delivered at least this many times"),
    "approve_baskets": (1, "approve held baskets by itself (1 yes, 0 no)"),
    "approve_max_total": (500.0, "EUR: it approves no basket worth more than this"),
    "approve_per_day": (1000.0, "EUR: it approves no more than this in a day"),
    "allow_dear_line": (0, "approve a basket held for a dear line (1 yes, 0 no: a person decides)"),
    "allow_new_supplier": (0, "approve a basket held for a new supplier (1 yes, 0 no: a person decides)"),
    "allow_no_price": (0, "approve a basket held for a line with no price (1 yes, 0 no: a person decides)"),
    "send_chases": (1, "chase a supplier that has not confirmed an order, or is past its day, by itself (1 yes, 0 no)"),
    "max_chases": (2, "it chases one order at most this many times, then a person takes it"),
    "send_disputes": (1, "open a dispute and mail it for a bill that does not match its deliveries (1 yes, 0 no)"),
    "dispute_max_claim": (2000.0, "EUR: it disputes no bill by itself for more than this; a person decides"),
    "bank_upload": (1, "hand a batch of payments to the bank by SFTP by itself (1 yes, 0 no: a person uploads it)"),
    "count_tolerance_pct": (2.0, "a hand count within this percent of the books (at least one unit) is posted at once; "
                                 "a bigger gap needs a second count that agrees"),
    "pay_bills": (1, "pay supplier bills out of the wallet by itself (1 yes, 0 no)"),
    "pay_max_total": (5000.0, "EUR: the most one bill may be before a person has to pay it"),
    "pay_per_day": (10000.0, "EUR: the most it pays in a day"),
    "pay_days_before_due": (2, "it pays a matched bill this many days before its due date, never earlier"),
    "wallet_reserve": (0.0, "EUR the wallet always keeps: it spends nothing that would take it below this"),
    "cycle_days": (14, "days between two big orders: Plutus orders once per cycle, not every hour"),
    "order_weekday": (0, "the weekday the cycle order is written (0 Monday, 4 Friday); a holiday moves it on"),
    "max_cover_days": (90, "never order more than this many days of use in all, however the buffer is set"),
    "min_supplier_order": (30.0, "EUR: a supplier's whole cycle order under this waits for the next sitting, "
                                 "unless a line is already in red"),
    "urgent_max_lines": (10, "the most lines on an urgent order, written between cycles for an article already in red"),
    "urgent_max_value": (2000.0, "EUR an urgent order may come to"),
    "max_lines": (60, "the most lines on the cycle order"),
    "max_qty_multiple": (1.25, "a line may be at most this many times the quantity the DDMRP buffers ask for"),
    "min_qty_multiple": (0.8, "and at least this many times (an article a person named is exempt)"),
    "only_planner_articles": (1, "only articles the DDMRP or the planner puts forward, or named by a person (1 yes, 0 no)"),
    "block_refused_after": (2, "leave out an article whose baskets were refused this many times in 90 days, unless named"),
    "max_order_value": (25000.0, "EUR the priced lines of the cycle order may come to; lines past it are cut, least urgent first"),
    "require_reason": (1, "every line must say why (1 yes, 0 no)"),
    "whole_packs": (1, "round a line up to whole packs where the pack size is known (1 yes, 0 no)"),
}
TERMINUS = {
    "min_confidence": (0.8, "a 'same' answer under this confidence (0 to 1) becomes 'unsure'"),
    "same_needs_known_match": (1, "'same' must name a known article exactly, or it becomes 'unsure' (1 yes, 0 no)"),
    "max_lines": (60, "the most lines it will read in one basket; a longer basket comes back 'unsure'"),
    "approve_max_total": (500.0, "EUR: it approves no basket worth more than this, whatever else is true"),
    "approve_per_day": (1000.0, "EUR: it approves no more than this in a day"),
    "allow_dear_line": (0, "approve a basket held for a dear line (1 yes, 0 no: a person decides)"),
    "allow_new_supplier": (0, "approve a basket held for a new supplier (1 yes, 0 no: a person decides)"),
    "allow_no_price": (0, "approve a basket held for a line with no price (1 yes, 0 no: a person decides)"),
    "cancel_refused_after": (2, "cancel a basket with an article whose baskets were refused this many times in 90 days"),
    "cancel_duplicate_days": (3, "cancel a basket that repeats one approved or ordered in this many days"),
    "cancel_after_days": (14, "cancel a basket nobody has answered in this many days"),
}
BUILTIN = {"plutus": PLUTUS, "terminus": TERMINUS}

BUILTIN_FREEDOM = {
    "plutus": [
        "Run the purchasing by yourself: check the inventory and the DDMRP buffers, decide what to buy and how "
        "much, write the purchase order, send it to the supplier, and pay the bill when it comes. A person funds "
        "the wallet, and the wallet and the limits below are what you may spend.",
        "At each sitting (every cycle, on the order weekday) write one consolidated order for everything that will "
        "not last until the next sitting: any counted article in use whose net flow, less its use over the cycle, "
        "would fall under the top of its yellow, up to the top of its green plus the use of the cycle.",
        "Between sittings write a small urgent order, once a day at most, for an article already in its red zone "
        "with nothing on the way.",
        "Put every order you write through the gate, like any basket: it enters the sealed chain, Terminus reads it "
        "for sense, you approve what clears it, the wallet covers it, and only then is it sent.",
        "Send an order to its supplier when it is within the limits: the supplier's address is known, the prices "
        "can be checked, the supplier has delivered before, and the wallet covers it.",
        "Approve a held basket that Terminus read as the same article and that fits the limits, and place an "
        "approved basket with its supplier.",
        "Pay a supplier bill out of the wallet once it is matched to its deliveries, is not disputed, and is "
        "within a few days of its due date.",
        "Leave an article out when it will last until the next sitting, when something is already on its way, "
        "drafted, queued or owed, or when its supplier's whole order is too small to be worth placing. Say so in "
        "the note when a note is asked for.",
        "Count stock held back for a work item as not free, and ask for a count of any article whose books read "
        "below zero.",
        "Adjust a quantity only inside the narrow band around the DDMRP's own number, and round it up to whole "
        "packs where the trade buys in packs.",
    ],
    "terminus": [
        "Read every line of a basket against the known article names and answer 'same', 'new' or 'unsure', with "
        "the known name it matches and your reason.",
        "Answer 'unsure' whenever the line is too vague, or two known names fit equally well.",
        "Read every order Plutus writes before it goes anywhere, as a second pair of eyes: are the names known, is "
        "an article on it twice, does the buffer still ask for it and for that quantity, is there a supplier and a "
        "price, is it whole packs. Any finding makes the answer 'unsure', and the order waits for a person.",
        "Approve a held basket when every line is the same article, and everything that held it is settled by the "
        "names being known or allowed by the limits below.",
        "Cancel a held basket that repeats one already approved or ordered, that holds an article refused "
        "repeatedly, or that nobody has answered for too long.",
    ],
}
BUILTIN_NEVER = {
    "plutus": [
        "Never send an order Terminus has not read as coherent: a finding, a doubt or no reading means it waits.",
        "Never spend more than the wallet has free, or past the limits below. The wallet is the hard ceiling, and "
        "only a person puts money in or takes it out.",
        "Never send, approve or pay while the mode is off or still in dry run: dry run writes down what you would "
        "have done and does nothing.",
        "Never pay a bill that is not matched to its deliveries, is disputed, is not yet near its due date, or has "
        "no bank details on file. Never pay anyone but the supplier on the bill, and never pay a bill twice.",
        "Never approve a basket Terminus read as new or unsure, or one held for a reason the limits leave to a person.",
        "Never write a cycle order more than once a cycle, and never one every hour. Between sittings the only "
        "order is the small urgent one.",
        "Never order an article the buffers mark as uncounted, or one with no use in the last eight weeks. An "
        "article whose books read below zero is ordered from zero, one full buffer at most, and a count is asked for.",
        "Never order more than the cover limit allows, even when the buffer says so.",
        "Never undo what a person decided, and never put back a line a person took off an order: learn from it.",
        "Never invent an article, a supplier, a price or a quantity the numbers do not support.",
        "When asked to draft in words, never answer with anything but the JSON the schema asks for.",
    ],
    "terminus": [
        "Never call a different size, grade or finish the same article.",
        "Never approve a basket unless every line is the same article. Never approve past the limits below, or a "
        "basket held for a reason the limits leave to a person.",
        "Never cancel on a doubt: 'unsure' keeps the basket for a person to decide, it does not end it.",
        "Never pass an order that does not make sense: an article twice, one the buffers no longer ask for, a "
        "quantity that is not theirs, no supplier, no price. Say what is wrong, in a sentence.",
        "Never undo what a person decided: a basket a person approved or cancelled is theirs.",
        "Never write, send or place an order or change a price: Terminus only decides the baskets the gate holds.",
        "Never answer outside the schema. When you cannot answer, the answer is 'unsure'.",
    ],
}


# ---- the rules folder -------------------------------------------------------

TITLES = {"plutus": "Plutus, the buyer", "terminus": "Terminus, the gate keeper"}
_LIMIT = re.compile(r"^-\s*([a-z_]+)\s*=\s*([-0-9.]+)\s*(?::\s*(.*))?$")
_cache = {}


def folder():
    """The folder holding plutus.md and terminus.md, or None when none is set."""
    named = os.environ.get("BOT_RULES_DIR", "").strip()
    return Path(named).expanduser() if named else None


def _parse(text):
    """(freedom lines, never lines, {limit: (value, meaning)}) from a rules file."""
    section, free, never, numbers = None, [], [], {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if not line.startswith("-") or section is None:
            continue
        if section.startswith("free"):
            free.append(line[1:].strip())
        elif section.startswith("never"):
            never.append(line[1:].strip())
        elif section.startswith("limit"):
            found = _LIMIT.match(line)
            if found:
                number = float(found.group(2))
                numbers[found.group(1)] = (int(number) if number.is_integer() and "." not in found.group(2)
                                           else number, (found.group(3) or "").strip())
    return free, never, numbers


def rules(bot):
    """(freedom, never, limit defaults) for a bot: the file where there is one, the built-in
    text for anything the file leaves out."""
    free, never, numbers = list(BUILTIN_FREEDOM[bot]), list(BUILTIN_NEVER[bot]), dict(BUILTIN[bot])
    where = folder()
    path = where / f"{bot}.md" if where else None
    if path is not None and path.is_file():
        stamp = path.stat().st_mtime
        if _cache.get(bot, (None,))[0] != stamp:
            _cache[bot] = (stamp, _parse(path.read_text(encoding="utf-8")))
        got_free, got_never, got_numbers = _cache[bot][1]
        free, never = got_free or free, got_never or never
        for key, (value, what) in got_numbers.items():
            if key in numbers:                 # a limit the code does not know does nothing
                kind = type(numbers[key][0])
                numbers[key] = (kind(value), what or numbers[key][1])
    return free, never, numbers


def source(bot):
    where = folder()
    path = where / f"{bot}.md" if where else None
    return str(path) if path is not None and path.is_file() else "built in"


def render(bot):
    """A rules file for a bot, as the built-in charter reads."""
    out = [f"# {TITLES[bot]}", "",
           "Edit this file to change what the bot is told and its default limits.",
           "A limit set on the screen overrides the number here until it is reset.", "",
           "## Free to", ""]
    out += [f"- {text}" for text in BUILTIN_FREEDOM[bot]]
    out += ["", "## Never", ""] + [f"- {text}" for text in BUILTIN_NEVER[bot]]
    out += ["", "## Limits", "", "One per line: `- name = number : what it means`.", ""]
    out += [f"- {key} = {value:g} : {what}" for key, (value, what) in BUILTIN[bot].items()]
    return "\n".join(out) + "\n"


def limits(db_path, bot):
    """The bot's numbers: what a person has set, over the defaults."""
    out = {key: value for key, (value, _what) in rules(bot)[2].items()}
    for key, value in db.read(db_path, "SELECT key, value FROM bot_limits WHERE bot = ?", [bot]):
        if key in out:
            try:
                out[key] = float(value) if isinstance(out[key], float) else int(float(value))
            except ValueError:
                pass
    return out


def set_limit(db_path, bot, key, value):
    if bot not in BUILTIN or key not in BUILTIN[bot]:
        raise ValueError(f"{bot} has no limit called {key}.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("A limit is a number.") from None
    if number < 0:
        raise ValueError("A limit cannot be negative.")
    if key in ("min_confidence",) and number > 1:
        raise ValueError("A confidence is between 0 and 1.")
    db.write(db_path, "INSERT INTO bot_limits (bot, key, value) VALUES (?, ?, ?) "
                      "ON CONFLICT (bot, key) DO UPDATE SET value = excluded.value RETURNING key",
             [bot, key, str(number)])


def reset(db_path, bot, key):
    db.write(db_path, "DELETE FROM bot_limits WHERE bot = ? AND key = ? RETURNING key", [bot, key])


def overview(db_path):
    out = {}
    for bot in BUILTIN:
        free, never, table = rules(bot)
        now = limits(db_path, bot)
        out[bot] = {"freedom": free, "never": never, "source": source(bot),
                    "limits": [{"key": key, "value": now[key], "default": default, "what": what,
                                "changed": now[key] != default}
                               for key, (default, what) in table.items()]}
    return out


def prompt_block(db_path, bot):
    """The charter as the model reads it, numbers included."""
    now = limits(db_path, bot)
    free, never, table = rules(bot)
    lines = [f"YOUR CHARTER ({bot.upper()})", "", "You are free to:"]
    lines += [f"- {text}" for text in free]
    lines += ["", "You must never:"] + [f"- {text}" for text in never]
    lines += ["", "Limits, set by a person, applied again to your answer in code:"]
    lines += [f"- {key} = {now[key]:g}: {what}" for key, (_d, what) in table.items()]
    return "\n".join(lines)


# ---- Plutus: what is kept of its answer -------------------------------------

def _price(db_path, article, supplier):
    seen = prices.sightings(db_path, article, 1)
    return seen[0] if seen else None


def enforce_plutus(db_path, lines, planner_rows, named=(), refused=None, caps=None):
    """Cut a Plutus draft back to its charter. Gives back (kept lines, cuts, adjustments):
    a cut is a line that did not survive and why, an adjustment is a line that was changed."""
    now = dict(limits(db_path, "plutus"), **(caps or {}))
    refused = refused or {}
    by_name = {normalize_name(row["article"]): row for row in planner_rows}
    named = {normalize_name(name) for name in named}
    kept, cuts, changed = [], [], []

    for line in lines:
        article = normalize_name(line.get("article"))
        row = by_name.get(article)
        asked = article in named
        if now["only_planner_articles"] and row is None and not asked:
            cuts.append({"article": article, "why": "not in the planner's numbers and not named"})
            continue
        if (not asked and now["block_refused_after"]
                and refused.get(article, 0) >= now["block_refused_after"]):
            cuts.append({"article": article, "why": f"its baskets were refused {refused[article]} times"})
            continue
        if now["require_reason"] and not str(line.get("reason") or "").strip():
            cuts.append({"article": article, "why": "no reason given"})
            continue

        quantity = float(line["quantity"])
        suggested = float((row or {}).get("suggested") or 0)
        buffer = (row or {}).get("ddmrp") or {}
        if buffer.get("to_order"):               # the DDMRP's number is the one to be near
            suggested = float(buffer["to_order"])
        if not asked and suggested > 0:
            low, high = suggested * now["min_qty_multiple"], suggested * now["max_qty_multiple"]
            if quantity > high or quantity < low:
                fixed = max(low, min(high, quantity))
                changed.append({"article": article, "from": quantity, "to": fixed,
                                "why": f"outside {now['min_qty_multiple']:g} to "
                                       f"{now['max_qty_multiple']:g} times the suggested {suggested:g}"})
                quantity = fixed
        elif not asked and row is not None and suggested <= 0:
            cuts.append({"article": article, "why": "the planner suggests nothing for it"})
            continue
        if now["whole_packs"]:
            packed = units.to_packs(db_path, article, line.get("supplier") or (row or {}).get("supplier"),
                                    quantity)
            if packed and packed["base"] != quantity:
                changed.append({"article": article, "from": quantity, "to": packed["base"],
                                "why": f"whole packs of {packed['pack_qty']:g}"})
                quantity = packed["base"]
        if quantity <= 0:
            cuts.append({"article": article, "why": "a quantity of nothing"})
            continue
        kept.append(dict(line, article=line.get("article"), quantity=quantity))

    if len(kept) > now["max_lines"]:
        for line in kept[int(now["max_lines"]):]:
            cuts.append({"article": normalize_name(line["article"]),
                         "why": f"past the {now['max_lines']:g}-line limit"})
        kept = kept[:int(now["max_lines"])]

    total, within = 0.0, []
    for line in kept:
        price = _price(db_path, normalize_name(line["article"]), line.get("supplier"))
        value = (price or 0.0) * line["quantity"]
        if total + value > now["max_order_value"] and price is not None:
            cuts.append({"article": normalize_name(line["article"]),
                         "why": f"the draft would pass {now['max_order_value']:g} EUR"})
            continue
        total += value
        within.append(line)
    return within, cuts, changed
