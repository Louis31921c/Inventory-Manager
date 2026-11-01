"""What a group chat is told, whichever group chat it is.

Discord and Slack are two doors onto the same room: the same baskets, the same gate, the
same purchase orders. Only the plumbing differs, so only the plumbing is written twice.
Everything a person reads is written once, here.

The text is written in Discord's markdown because that is the one the app started with;
`to_slack` turns it into Slack's mrkdwn on the way out.
"""

import re

from . import gate, quotes

MAX_SHOWN = 25

# Discord doubles the star for bold, Slack takes a single one. Italics are the other way
# about, so both have to move at once or the second pass eats the first.
BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
ITALIC = re.compile(r"(?<![\w*])_(.+?)_(?![\w*])", re.S)


def to_slack(text):
    """Discord markdown out, Slack mrkdwn in."""
    held = BOLD.sub("\x00\\1\x00", text)
    held = ITALIC.sub("_\\1_", held)
    return held.replace("\x00", "*")


def allowed(channel_id, channels):
    """No allow-list means every channel the bot was invited to."""
    return not channels or str(channel_id) in channels


def channel_list(raw):
    return [piece.strip() for piece in str(raw or "").split(",") if piece.strip()]


def show_number(value):
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def basket_text(rows):
    if not rows:
        return "The basket is empty. `/basket shop:... items:...` fills it."
    shop = next((row["shop"] for row in rows if row["shop"]), None)
    site = next((row["site"] for row in rows if row["site"]), None)
    head = f"**Basket** ({len(rows)} line(s))"
    if shop:
        head += f" for **{shop}**"
    if site:
        head += f", site {site}"
    body = "\n".join(f"· {show_number(row['quantity'])} × {row['article']}"
                     + (f" @ {row['unit_price']:.2f}"
                        if row.get("unit_price") is not None else "")
                     + (f"  _{row['added_by']}_" if row["added_by"] else "")
                     for row in rows[:MAX_SHOWN])
    if len(rows) > MAX_SHOWN:
        body += f"\n· ... and {len(rows) - MAX_SHOWN} more"
    return f"{head}\n{body}"


def purchase_text(answer):
    if answer is None:
        return "Nothing in the basket to order."
    lines = "\n".join(f"· {show_number(row['quantity'])} × {row['article']}"
                      for row in answer["lines"][:MAX_SHOWN])
    where = f" on {answer['shop']}" if answer["shop"] else ""
    site = f", site {answer['site']}" if answer["site"] else ""
    return (f"**Purchase order #{answer['list_id']}** is in Stock Manager"
            f" ({len(answer['lines'])} line(s){site}){where}.\n{lines}\n"
            f"Someone has to place it on the supplier's site and tick it off in the app: "
            f"the bot does not pay for anything.")


def held_text(request_id, reading, answer):
    """What the channel is told when the gate holds a basket."""
    why = "\n".join(f"· {word}" for word in gate.said(reading["reasons"]))
    lines = "\n".join(
        f"· {show_number(row['quantity'])} × {row['article']}"
        + (f" @ {row['unit_price']:.2f}" if row.get("unit_price") is not None else " (no price)")
        for row in reading["lines"][:MAX_SHOWN])
    agent = {"same": "Terminus recognises every article",
             "new": "Terminus says at least one article is genuinely new",
             "unsure": "Terminus is not sure"}.get(answer["verdict"], answer["verdict"])
    return (f"**Held for approval — request #{request_id}** "
            f"({reading['total']:.2f} EUR)\n{lines}\n\nWhy:\n{why}\n\n"
            f"First check: {agent}. {answer['note']}\n"
            f"Nothing is bought until it is approved in Stock Manager. "
            f"The basket stays as it is.")


def quote_text(quote):
    """A quote asks for a price; it never says what the price is."""
    lines = "\n".join(f"· {quotes.show_quantity(row)} {row['article']}"
                      for row in quote["rows"][:MAX_SHOWN])
    where = f" from **{quote['supplier']}**" if quote["supplier"] else ""
    site = f", site {quote['site']}" if quote["site"] else ""
    return (f"**Quote request #{quote['quote_id']}**{where} is in Stock Manager"
            f" ({len(quote['rows'])} line(s){site}).\n{lines}\n"
            f"Send it out and type the prices in when they answer.")


def stock_text(article, rows):
    if not rows:
        return f"Nothing on the shelf matches **{article}**."
    body = "\n".join(f"· {row['article']}: {show_number(row['quantity'])}" for row in rows)
    return f"**{article}**\n{body}"


def no_article():
    return "I read no article in that."
