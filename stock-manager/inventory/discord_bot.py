"""The Discord door: a basket people fill in a channel, a purchase order at the end.

Nothing here buys anything. `/purchase` writes the order into Stock Manager and hands it
back to a person to place on the supplier's own site: a bot with a company card is not
something this app will do on its own.

What anyone reads is written once, in chat.py, which the Slack door uses as well. Only the
plumbing is here.
"""
import asyncio
import logging
import os

from . import baskets, chat, gate, quotes, verify
from .chat import (MAX_SHOWN, allowed, basket_text, held_text, purchase_text, quote_text,
                   show_number, stock_text)
from .config import load_config

log = logging.getLogger(__name__)

TOKEN_KEY = "DISCORD_BOT_TOKEN"
CHANNELS_KEY = "DISCORD_CHANNELS"


def settings():
    return {"token": os.environ.get(TOKEN_KEY, "").strip(),
            "channels": chat.channel_list(os.environ.get(CHANNELS_KEY, ""))}


def build_client(config, channels):
    """The Discord side, built only when the library is installed."""
    import discord
    from discord import app_commands

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    async def refuse(interaction):
        if allowed(interaction.channel_id, channels):
            return False
        await interaction.response.send_message(
            "This channel is not on the list Stock Manager answers in.", ephemeral=True)
        return True

    def who(interaction):
        user = interaction.user
        return getattr(user, "display_name", None) or getattr(user, "name", "?")

    @tree.command(name="basket", description="Add what you need to this channel's basket")
    @app_commands.describe(shop="Where it should be ordered from",
                           items="12 hex bolt m6x25 @ 0.42, rondelle m8 x100",
                           site="Job site the goods are for",
                           work_item="Part of the job, if it matters",
                           supplier="Who it comes from, if the shop is not the supplier")
    async def basket(interaction, shop: str, items: str, site: str = "", work_item: str = "",
                     supplier: str = ""):
        if await refuse(interaction):
            return
        parsed = baskets.parse_items(items)
        if not parsed:
            await interaction.response.send_message(chat.no_article(), ephemeral=True)
            return
        await asyncio.to_thread(baskets.add, config.db_path, interaction.channel_id, parsed,
                                shop.strip() or None, site.strip() or None,
                                work_item.strip() or None, who(interaction),
                                supplier.strip() or None)
        rows = await asyncio.to_thread(baskets.lines, config.db_path, interaction.channel_id)
        await interaction.response.send_message(basket_text(rows))

    @tree.command(name="basket_show", description="Show this channel's basket")
    async def basket_show(interaction):
        if await refuse(interaction):
            return
        rows = await asyncio.to_thread(baskets.lines, config.db_path, interaction.channel_id)
        await interaction.response.send_message(basket_text(rows))

    @tree.command(name="basket_clear", description="Empty this channel's basket")
    async def basket_clear(interaction):
        if await refuse(interaction):
            return
        held = await asyncio.to_thread(baskets.clear, config.db_path, interaction.channel_id)
        await interaction.response.send_message(f"Basket emptied, {held} line(s) dropped.")

    @tree.command(name="purchase", description="Turn the basket into a purchase order")
    async def purchase(interaction):
        if await refuse(interaction):
            return
        held = await asyncio.to_thread(baskets.lines, config.db_path, interaction.channel_id)
        if not held:
            await interaction.response.send_message(purchase_text(None))
            return

        reading = await asyncio.to_thread(gate.look, config.db_path, held)
        if reading["held"]:
            # Held: the agent reads the names, then it waits for a person. The basket stays
            # where it is, so nothing is lost either way the answer goes.
            await interaction.response.defer(thinking=True)
            request_id = await asyncio.to_thread(
                gate.ask, config.db_path, reading, interaction.channel_id,
                next((row["shop"] for row in held if row["shop"]), None), who(interaction),
                next((row["site"] for row in held if row["site"]), None),
                next((row["work_item"] for row in held if row["work_item"]), None))
            answer = await verify.check(config.db_path, reading["lines"])
            await asyncio.to_thread(gate.record_check, config.db_path, request_id,
                                    answer["verdict"], answer["note"], answer["lines"])
            await interaction.followup.send(held_text(request_id, reading, answer))
            return

        answer = await asyncio.to_thread(baskets.to_purchase_order, config.db_path,
                                         interaction.channel_id, who(interaction))
        await interaction.response.send_message(purchase_text(answer))

    @tree.command(name="quote",
                  description="Ask a supplier to price something by the metre, kilo or m3")
    @app_commands.describe(supplier="Who should price it",
                           items="120 ml skirting board; 3 m3 gravel; 12 anchors",
                           site="Job site the goods are for",
                           work_item="Part of the job, if it matters")
    async def quote(interaction, supplier: str, items: str, site: str = "", work_item: str = ""):
        if await refuse(interaction):
            return
        asked = quotes.parse_request(items)
        if not asked:
            await interaction.response.send_message(chat.no_article(), ephemeral=True)
            return
        head = {"supplier": supplier, "site": site, "work_item": work_item}
        quote_id = await asyncio.to_thread(quotes.save, config.db_path, head, asked,
                                           who(interaction))
        stored = await asyncio.to_thread(quotes.one, config.db_path, quote_id)
        await interaction.response.send_message(quote_text(stored))

    @tree.command(name="stock", description="What the shelf holds for an article")
    @app_commands.describe(article="Part of the name, e.g. rondelle m8")
    async def stock(interaction, article: str):
        if await refuse(interaction):
            return
        rows = await asyncio.to_thread(baskets.stock_of, config.db_path, article)
        await interaction.response.send_message(stock_text(article.upper(), rows))

    @client.event
    async def on_ready():
        await tree.sync()
        log.info("discord: signed in as %s", client.user)

    return client


async def run(config=None):
    """Sign in and stay on, as long as a token is set and the library is there."""
    config = config or load_config()
    kept = settings()
    if not kept["token"]:
        log.info("discord: no token set, bot not started")
        return
    try:
        client = build_client(config, kept["channels"])
    except ImportError:
        log.warning("discord: the library is missing, "
                    "install it with: pip install 'stock-manager[discord]'")
        return
    try:
        await client.start(kept["token"])
    except Exception as e:
        log.warning("discord: %s", e)


def status():
    kept = settings()
    try:
        import discord  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    return {"installed": installed, "token_set": bool(kept["token"]),
            "channels": kept["channels"],
            "ready": installed and bool(kept["token"])}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(run())
