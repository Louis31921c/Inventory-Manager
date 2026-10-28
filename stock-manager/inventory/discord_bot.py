"""The group chat side: a basket people fill in Discord, a purchase order at the end.

Nothing here buys anything. `/purchase` writes the order into Stock Manager and hands it back
to a person to place on the supplier's own site: a bot with a company card is not something
this app will do on its own.
"""
import asyncio
import logging
import os

from . import baskets
from .config import load_config

log = logging.getLogger(__name__)

TOKEN_KEY = "DISCORD_BOT_TOKEN"
CHANNELS_KEY = "DISCORD_CHANNELS"
MAX_SHOWN = 25


def settings():
    allow = os.environ.get(CHANNELS_KEY, "")
    return {"token": os.environ.get(TOKEN_KEY, "").strip(),
            "channels": [piece.strip() for piece in allow.split(",") if piece.strip()]}


def allowed(channel_id, channels):
    """No allow-list means every channel the bot was invited to."""
    return not channels or str(channel_id) in channels


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


def stock_text(article, rows):
    if not rows:
        return f"Nothing on the shelf matches **{article}**."
    body = "\n".join(f"· {row['article']}: {show_number(row['quantity'])}" for row in rows)
    return f"**{article}**\n{body}"


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
                           items="12 hex bolt m6x25, rondelle m8 x100",
                           site="Job site the goods are for",
                           work_item="Part of the job, if it matters")
    async def basket(interaction, shop: str, items: str, site: str = "", work_item: str = ""):
        if await refuse(interaction):
            return
        parsed = baskets.parse_items(items)
        if not parsed:
            await interaction.response.send_message("I read no article in that.", ephemeral=True)
            return
        await asyncio.to_thread(baskets.add, config.db_path, interaction.channel_id, parsed,
                                shop.strip() or None, site.strip() or None,
                                work_item.strip() or None, who(interaction))
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
        answer = await asyncio.to_thread(baskets.to_purchase_order, config.db_path,
                                         interaction.channel_id, who(interaction))
        await interaction.response.send_message(purchase_text(answer))

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
