"""The Slack door onto the same room: baskets, quotes, the shelf, and the gate.

Slack cannot knock on this app's door — it runs on loopback with no address of its own —
so the connection is made the other way about, in socket mode. That wants two tokens: a
bot token (xoxb-) for what it says, and an app-level token (xapp-) for the socket itself.

A Slack slash command carries one line of free text, where Discord carried a form. So
`/basket` with nothing after it opens the same form as a dialog, and `/basket` with
something after it reads the line straight, for whoever would rather type.

Nothing here buys anything, same as the other door.
"""
import asyncio
import logging
import os

from . import baskets, chat, gate, quotes, verify
from .config import load_config

log = logging.getLogger(__name__)

BOT_TOKEN_KEY = "SLACK_BOT_TOKEN"
APP_TOKEN_KEY = "SLACK_APP_TOKEN"
CHANNELS_KEY = "SLACK_CHANNELS"

# The dialog's own name, so a submission can be told from any other view.
BASKET_VIEW = "stock-manager-basket"
QUOTE_VIEW = "stock-manager-quote"


def settings():
    return {"bot_token": os.environ.get(BOT_TOKEN_KEY, "").strip(),
            "app_token": os.environ.get(APP_TOKEN_KEY, "").strip(),
            "channels": chat.channel_list(os.environ.get(CHANNELS_KEY, ""))}


def status():
    kept = settings()
    installed = available()
    both = bool(kept["bot_token"]) and bool(kept["app_token"])
    return {"installed": installed, "bot_token_set": bool(kept["bot_token"]),
            "app_token_set": bool(kept["app_token"]), "channels": kept["channels"],
            "ready": installed and both}


def field(block_id, label, placeholder="", optional=False, multiline=False):
    return {
        "type": "input", "block_id": block_id, "optional": optional,
        "label": {"type": "plain_text", "text": label},
        "element": {"type": "plain_text_input", "action_id": "value",
                    "multiline": multiline,
                    "placeholder": {"type": "plain_text", "text": placeholder or label}},
    }


def basket_dialog(channel_id):
    """The form Discord gave for nothing, drawn by hand."""
    return {
        "type": "modal", "callback_id": BASKET_VIEW,
        "private_metadata": str(channel_id),
        "title": {"type": "plain_text", "text": "Add to the basket"},
        "submit": {"type": "plain_text", "text": "Add"},
        "blocks": [
            field("shop", "Where it should be ordered from", "foussier.fr"),
            field("items", "What is needed", "12 vis th 6x25 @ 0.11, 4 rondelle m8",
                  multiline=True),
            field("site", "Job site", "OSTREA", optional=True),
            field("work_item", "Part of the job", "PORTAIL BAT C", optional=True),
            field("supplier", "Supplier, if the shop is not the supplier", "FOUSSIER",
                  optional=True),
        ],
    }


def quote_dialog(channel_id):
    return {
        "type": "modal", "callback_id": QUOTE_VIEW,
        "private_metadata": str(channel_id),
        "title": {"type": "plain_text", "text": "Ask for a quote"},
        "submit": {"type": "plain_text", "text": "Ask"},
        "blocks": [
            field("supplier", "Who should price it", "SAINT-GOBAIN VITRAGE"),
            field("items", "What to price", "120 ml skirting board; 3 m3 gravel",
                  multiline=True),
            field("site", "Job site", "OSTREA", optional=True),
            field("work_item", "Part of the job", "VITRAGE", optional=True),
        ],
    }


def typed(view, name):
    """One field out of a submitted dialog, empty when it was left alone."""
    block = (view.get("state", {}).get("values", {}) or {}).get(name, {})
    return (block.get("value", {}) or {}).get("value", "") or ""


def check():
    """Try both tokens against Slack and say what came back.

    The bot token says who the app is; the app token is what opens the socket. They fail
    in different ways and are easy to paste the wrong way round, so both are tried and
    each is answered for on its own.
    """
    kept = settings()
    if not available():
        return False, "The library is missing: pip install 'stock-manager[slack]'."
    if not kept["bot_token"] and not kept["app_token"]:
        return False, "No token stored yet."

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    said = []
    if not kept["bot_token"]:
        return False, "The bot token (xoxb-) is missing."
    try:
        who = WebClient(token=kept["bot_token"]).auth_test()
        said.append(f"{who['user']} in {who['team']}")
    except SlackApiError as e:
        return False, f"The bot token was refused: {e.response.get('error', e)}"
    except Exception as e:
        return False, f"Slack could not be reached: {e}"

    if not kept["app_token"]:
        return False, "The app token (xapp-) is missing, so the socket cannot open."
    try:
        # The app token goes as an argument, not as the client's own: this call is the one
        # place Slack asks for it that way.
        WebClient().apps_connections_open(app_token=kept["app_token"])
        said.append("the socket opens")
    except SlackApiError as e:
        return False, f"The app token was refused: {e.response.get('error', e)}"
    except Exception as e:
        return False, f"The socket could not be opened: {e}"

    where = (", ".join(kept["channels"]) if kept["channels"]
             else "every channel it is invited to")
    said.append(f"answering in {where}")
    return True, " · ".join(said)


def available():
    try:
        import slack_bolt  # noqa: F401
        return True
    except ImportError:
        return False


def build_app(config, channels):
    """The Slack side, built only when the library is installed."""
    from slack_bolt.app.async_app import AsyncApp

    kept = settings()
    app = AsyncApp(token=kept["bot_token"])

    def refused(body):
        return not chat.allowed(body.get("channel_id") or body.get("channel", {}).get("id"),
                                channels)

    def say_it(text):
        return chat.to_slack(text)

    async def add_lines(channel_id, shop, items, site, work_item, supplier, who):
        parsed = baskets.parse_items(items)
        if not parsed:
            return None
        await asyncio.to_thread(baskets.add, config.db_path, channel_id, parsed,
                                shop.strip() or None, site.strip() or None,
                                work_item.strip() or None, who, supplier.strip() or None)
        return await asyncio.to_thread(baskets.lines, config.db_path, channel_id)

    @app.command("/basket")
    async def basket(ack, body, client, respond):
        await ack()
        if refused(body):
            await respond("This channel is not on the list Stock Manager answers in.")
            return
        text = (body.get("text") or "").strip()
        if not text:
            await client.views_open(trigger_id=body["trigger_id"],
                                    view=basket_dialog(body["channel_id"]))
            return
        rows = await add_lines(body["channel_id"], "", text, "", "", "",
                               body.get("user_name"))
        await respond(say_it(chat.no_article() if rows is None else chat.basket_text(rows)))

    @app.view(BASKET_VIEW)
    async def basket_submitted(ack, body, view, client):
        await ack()
        channel_id = view.get("private_metadata") or ""
        rows = await add_lines(channel_id, typed(view, "shop"), typed(view, "items"),
                               typed(view, "site"), typed(view, "work_item"),
                               typed(view, "supplier"),
                               body.get("user", {}).get("username"))
        await client.chat_postMessage(
            channel=channel_id,
            text=say_it(chat.no_article() if rows is None else chat.basket_text(rows)))

    @app.command("/basket_show")
    async def basket_show(ack, body, respond):
        await ack()
        if refused(body):
            return
        rows = await asyncio.to_thread(baskets.lines, config.db_path, body["channel_id"])
        await respond(say_it(chat.basket_text(rows)))

    @app.command("/basket_clear")
    async def basket_clear(ack, body, respond):
        await ack()
        if refused(body):
            return
        held = await asyncio.to_thread(baskets.clear, config.db_path, body["channel_id"])
        await respond(f"Basket emptied, {held} line(s) dropped.")

    @app.command("/purchase")
    async def purchase(ack, body, respond):
        await ack()
        if refused(body):
            return
        channel_id = body["channel_id"]
        who = body.get("user_name")
        held = await asyncio.to_thread(baskets.lines, config.db_path, channel_id)
        if not held:
            await respond(say_it(chat.purchase_text(None)))
            return

        reading = await asyncio.to_thread(gate.look, config.db_path, held)
        if reading["held"]:
            # Held: Terminus reads the names, then it waits for a person. The basket stays
            # where it is, so nothing is lost either way the answer goes.
            request_id = await asyncio.to_thread(
                gate.ask, config.db_path, reading, channel_id,
                next((row["shop"] for row in held if row["shop"]), None), who,
                next((row["site"] for row in held if row["site"]), None),
                next((row["work_item"] for row in held if row["work_item"]), None))
            answer = await verify.check(config.db_path, reading["lines"])
            await asyncio.to_thread(gate.record_check, config.db_path, request_id,
                                    answer["verdict"], answer["note"], answer["lines"])
            await respond(say_it(chat.held_text(request_id, reading, answer)))
            return

        answer = await asyncio.to_thread(baskets.to_purchase_order, config.db_path,
                                         channel_id, who)
        await respond(say_it(chat.purchase_text(answer)))

    @app.command("/quote")
    async def quote(ack, body, client, respond):
        await ack()
        if refused(body):
            return
        text = (body.get("text") or "").strip()
        if not text:
            await client.views_open(trigger_id=body["trigger_id"],
                                    view=quote_dialog(body["channel_id"]))
            return
        stored = await save_quote("", text, "", "", body.get("user_name"))
        await respond(say_it(chat.no_article() if stored is None else chat.quote_text(stored)))

    async def save_quote(supplier, items, site, work_item, who):
        asked = quotes.parse_request(items)
        if not asked:
            return None
        head = {"supplier": supplier, "site": site, "work_item": work_item}
        quote_id = await asyncio.to_thread(quotes.save, config.db_path, head, asked, who)
        return await asyncio.to_thread(quotes.one, config.db_path, quote_id)

    @app.view(QUOTE_VIEW)
    async def quote_submitted(ack, body, view, client):
        await ack()
        channel_id = view.get("private_metadata") or ""
        stored = await save_quote(typed(view, "supplier"), typed(view, "items"),
                                  typed(view, "site"), typed(view, "work_item"),
                                  body.get("user", {}).get("username"))
        await client.chat_postMessage(
            channel=channel_id,
            text=say_it(chat.no_article() if stored is None else chat.quote_text(stored)))

    @app.command("/stock")
    async def stock(ack, body, respond):
        await ack()
        if refused(body):
            return
        article = (body.get("text") or "").strip()
        if not article:
            await respond("Which article? `/stock rondelle m8`")
            return
        rows = await asyncio.to_thread(baskets.stock_of, config.db_path, article)
        await respond(say_it(chat.stock_text(article.upper(), rows)))

    return app


async def run(config=None):
    """Open the socket and stay on it, as long as both tokens are set."""
    config = config or load_config()
    kept = settings()
    if not kept["bot_token"] or not kept["app_token"]:
        log.info("slack: no token pair set, bot not started")
        return
    try:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    except ImportError:
        log.warning("slack: the library is missing, "
                    "install it with: pip install 'stock-manager[slack]'")
        return
    try:
        app = build_app(config, kept["channels"])
        await AsyncSocketModeHandler(app, kept["app_token"]).start_async()
    except Exception as e:
        log.warning("slack: %s", e)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(run())
