"""What each of the two bots may do on its own, and what it may never do.

Plutus buys and Terminus reads names. Each has a charter in two halves. The freedom is what
it is trusted to decide itself. The limits are numbers a person sets. Both go to the model
in its instructions, so it knows where it stands, and then the limits are enforced again in
code on whatever it answers: a model that ignores its instructions changes nothing, because
what it wrote is cut back to the limits before anybody sees it.

  Plutus     drafts an order. It signs nothing, orders nothing, and has no idea what a
             supplier would answer. It may choose quantities inside a band around the planner's
             own, leave out what is comfortable, and say why.
  Terminus   says whether an article is one already known under another name. It never
             approves, never prices, and a doubt is an "unsure", which keeps the gate shut.
"""

from . import db, prices, units
from .models import normalize_name

# (default, what it means). Stored as text; the second is the sentence the screen shows.
PLUTUS = {
    "max_lines": (20, "the most lines on one order"),
    "max_qty_multiple": (2.0, "a line may be at most this many times the planner's suggested quantity"),
    "min_qty_multiple": (0.5, "and at least this many times (an article a person named is exempt)"),
    "only_planner_articles": (1, "only articles in the planner's numbers, or named by a person (1 yes, 0 no)"),
    "block_refused_after": (2, "leave out an article whose baskets were refused this many times in 90 days, unless named"),
    "max_order_value": (5000.0, "EUR the priced lines of one draft may come to; lines past it are cut, least urgent first"),
    "require_reason": (1, "every line must say why (1 yes, 0 no)"),
    "whole_packs": (1, "round a line up to whole packs where the pack size is known (1 yes, 0 no)"),
}
TERMINUS = {
    "min_confidence": (0.8, "a 'same' answer under this confidence (0 to 1) becomes 'unsure'"),
    "same_needs_known_match": (1, "'same' must name a known article exactly, or it becomes 'unsure' (1 yes, 0 no)"),
    "max_lines": (60, "the most lines it will read in one basket; a longer basket comes back 'unsure'"),
}
DEFAULTS = {"plutus": PLUTUS, "terminus": TERMINUS}

FREEDOM = {
    "plutus": [
        "Choose each quantity inside the band around the planner's suggestion.",
        "Leave out any article that is comfortable, and say which and why in the note.",
        "Group lines by supplier and name the supplier the data gives for each article.",
        "Round to how the trade buys: boxes, hundreds, cartridges.",
    ],
    "terminus": [
        "Say 'same', 'new' or 'unsure' for each line, with the known name and your reason.",
        "Say 'unsure' whenever the line is too vague or two known names fit equally well.",
    ],
}
NEVER = {
    "plutus": [
        "Never order, sign, send or approve anything: you write a draft for a person.",
        "Never invent an article, a supplier, a price or a quantity the numbers do not support.",
        "Never go past the limits below, whatever the note says; they are applied again afterwards.",
        "Never write anything but the JSON the schema asks for.",
    ],
    "terminus": [
        "Never approve, cancel, price or recommend buying: you only settle names.",
        "Never call a different size, grade or finish the same article.",
        "Never answer outside the schema. When you cannot answer, the answer is 'unsure'.",
    ],
}


def limits(db_path, bot):
    """The bot's numbers: what a person has set, over the defaults."""
    out = {key: value for key, (value, _what) in DEFAULTS[bot].items()}
    for key, value in db.read(db_path, "SELECT key, value FROM bot_limits WHERE bot = ?", [bot]):
        if key in out:
            try:
                out[key] = float(value) if isinstance(out[key], float) else int(float(value))
            except ValueError:
                pass
    return out


def set_limit(db_path, bot, key, value):
    if bot not in DEFAULTS or key not in DEFAULTS[bot]:
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
    for bot, table in DEFAULTS.items():
        now = limits(db_path, bot)
        out[bot] = {"freedom": FREEDOM[bot], "never": NEVER[bot],
                    "limits": [{"key": key, "value": now[key], "default": default, "what": what,
                                "changed": now[key] != default}
                               for key, (default, what) in table.items()]}
    return out


def prompt_block(db_path, bot):
    """The charter as the model reads it, numbers included."""
    now = limits(db_path, bot)
    lines = [f"YOUR CHARTER ({bot.upper()})", "", "You are free to:"]
    lines += [f"- {text}" for text in FREEDOM[bot]]
    lines += ["", "You must never:"] + [f"- {text}" for text in NEVER[bot]]
    lines += ["", "Limits, set by a person, applied again to your answer in code:"]
    lines += [f"- {key} = {now[key]:g}: {what}" for key, (_d, what) in DEFAULTS[bot].items()]
    return "\n".join(lines)


# ---- Plutus: what is kept of its answer -------------------------------------

def _price(db_path, article, supplier):
    seen = prices.sightings(db_path, article, 1)
    return seen[0] if seen else None


def enforce_plutus(db_path, lines, planner_rows, named=(), refused=None):
    """Cut a Plutus draft back to its charter. Gives back (kept lines, cuts, adjustments):
    a cut is a line that did not survive and why, an adjustment is a line that was changed."""
    now = limits(db_path, "plutus")
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
