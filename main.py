"""
QuickSell - Discord NFT buyer/seller matching bot
NEW BUILD - do not put secrets in this file.

Flow:
BUY/SELL -> free request -> real matching -> paid contact access
-> private deal room -> both sides confirm -> DEAL_CONFIRMED.

IMPORTANT:
- Prices are entered by users; there are NO hard-coded 300/500 values.
- Different prices do NOT block a potential match.
- This file expects DISCORD_TOKEN in the environment.
"""

import os
import asyncio
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

DB_PATH = os.getenv("QUICKSELL_DB", "quicksell.db")
MATCH_INTERVAL = 1.0


class RequestType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class DealStatus(str, Enum):
    POTENTIAL_MATCH = "POTENTIAL_MATCH"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    DEAL_CONFIRMED = "DEAL_CONFIRMED"
    DECLINED = "DECLINED"
    CLOSED = "CLOSED"


@dataclass
class Request:
    id: int
    guild_id: int
    user_id: int
    request_type: RequestType
    collection: str
    mint: Optional[str]
    amount: float
    active: bool = True


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        request_type TEXT NOT NULL,
        collection TEXT NOT NULL,
        mint TEXT,
        amount REAL NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        buy_request_id INTEGER NOT NULL,
        sell_request_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        buyer_confirmed INTEGER NOT NULL DEFAULT 0,
        seller_confirmed INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        UNIQUE(buy_request_id, sell_request_id)
    );

    CREATE TABLE IF NOT EXISTS access_passes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan_hours INTEGER NOT NULL,
        tx_signature TEXT UNIQUE,
        confirmed_at REAL,
        expires_at REAL,
        status TEXT NOT NULL DEFAULT 'WAITING'
    );
    """)
    con.commit()
    con.close()


def collections_match(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def mints_match(a: Optional[str], b: Optional[str]) -> bool:
    # If neither side specifies a mint, collection matching is sufficient.
    if not a or not b:
        return True
    return a.strip() == b.strip()


def compatible(buy: sqlite3.Row, sell: sqlite3.Row) -> bool:
    """
    PRICE IS INTENTIONALLY NOT CHECKED.
    9 SOL vs 12.5 SOL can still create a potential match.
    """
    return (
        buy["active"] == 1
        and sell["active"] == 1
        and buy["guild_id"] == sell["guild_id"]
        and buy["user_id"] != sell["user_id"]
        and collections_match(buy["collection"], sell["collection"])
        and mints_match(buy["mint"], sell["mint"])
    )


async def matching_loop(bot: commands.Bot):
    await bot.wait_until_ready()
    while not bot.is_closed():
        con = db()
        buys = con.execute(
            "SELECT * FROM requests WHERE request_type='BUY' AND active=1"
        ).fetchall()
        sells = con.execute(
            "SELECT * FROM requests WHERE request_type='SELL' AND active=1"
        ).fetchall()

        for buy in buys:
            for sell in sells:
                if not compatible(buy, sell):
                    continue

                exists = con.execute(
                    """SELECT id FROM matches
                       WHERE buy_request_id=? AND sell_request_id=?""",
                    (buy["id"], sell["id"])
                ).fetchone()

                if not exists:
                    con.execute(
                        """INSERT INTO matches
                           (buy_request_id, sell_request_id, status, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (
                            buy["id"],
                            sell["id"],
                            DealStatus.POTENTIAL_MATCH.value,
                            time.time(),
                        ),
                    )
                    con.commit()

                    await notify_match(
                        bot,
                        buy_user_id=buy["user_id"],
                        sell_user_id=sell["user_id"],
                        collection=buy["collection"],
                        buyer_offer=buy["amount"],
                        seller_asking=sell["amount"],
                    )

        con.close()
        await asyncio.sleep(MATCH_INTERVAL)


async def notify_match(
    bot,
    buy_user_id: int,
    sell_user_id: int,
    collection: str,
    buyer_offer: float,
    seller_asking: float,
):
    """
    Sends a DM when possible. The actual identity/contact remains
    behind the paid access layer in production.
    """
    buyer = bot.get_user(buy_user_id)
    seller = bot.get_user(sell_user_id)

    buyer_text = (
        "🎯 **INTERESTED SELLER FOUND**\n\n"
        f"Collection: **{collection}**\n"
        f"Seller asking price: **{seller_asking:g} SOL**\n"
        f"Your offer/budget: **{buyer_offer:g} SOL**\n\n"
        "💬 The price can be negotiated directly.\n"
        "Contact access is available through QuickSell."
    )

    seller_text = (
        "🎯 **INTERESTED BUYER FOUND**\n\n"
        f"Collection: **{collection}**\n"
        f"Your asking price: **{seller_asking:g} SOL**\n"
        f"Buyer offer: **{buyer_offer:g} SOL**\n\n"
        "💬 The price can be negotiated directly.\n"
        "Contact access is available through QuickSell."
    )

    if buyer:
        try:
            await buyer.send(buyer_text)
        except discord.HTTPException:
            pass

    if seller:
        try:
            await seller.send(seller_text)
        except discord.HTTPException:
            pass


class RequestModal(discord.ui.Modal):
    def __init__(self, request_type: RequestType):
        super().__init__(title="QuickSell • Create Request")
        self.request_type = request_type

        self.collection = discord.ui.TextInput(
            label="NFT collection",
            placeholder="Enter the collection name",
            required=True,
            max_length=100,
        )
        self.amount = discord.ui.TextInput(
            label="Amount in SOL",
            placeholder="Enter your amount in SOL",
            required=True,
            max_length=30,
        )
        self.mint = discord.ui.TextInput(
            label="Mint address (optional)",
            placeholder="Leave empty if you do not have a specific mint",
            required=False,
            max_length=80,
        )

        self.add_item(self.collection)
        self.add_item(self.amount)
        self.add_item(self.mint)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = float(str(self.amount.value).replace(",", "."))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Please enter a valid SOL amount greater than 0.",
                ephemeral=True,
            )
            return

        con = db()
        con.execute(
            """INSERT INTO requests
               (guild_id, user_id, request_type, collection, mint, amount, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                interaction.guild_id,
                interaction.user.id,
                self.request_type.value,
                str(self.collection.value).strip(),
                str(self.mint.value).strip() or None,
                amount,
                time.time(),
            ),
        )
        con.commit()
        con.close()

        label = "BUY" if self.request_type == RequestType.BUY else "SELL"

        await interaction.response.send_message(
            f"✅ **{label} request created.**\n\n"
            f"Collection: **{self.collection.value}**\n"
            f"Amount: **{amount:g} SOL**\n\n"
            "🎯 QuickSell will look for a real interested counterparty.\n"
            "Different prices do not prevent a potential match.",
            ephemeral=True,
        )


class MainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="I WANT TO BUY",
        emoji="🛒",
        style=discord.ButtonStyle.primary,
        custom_id="quicksell:buy",
    )
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RequestModal(RequestType.BUY))

    @discord.ui.button(
        label="I WANT TO SELL",
        emoji="💰",
        style=discord.ButtonStyle.success,
        custom_id="quicksell:sell",
    )
    async def sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RequestModal(RequestType.SELL))

    @discord.ui.button(
        label="MY MATCHES",
        emoji="🎯",
        style=discord.ButtonStyle.secondary,
        custom_id="quicksell:matches",
    )
    async def matches(self, interaction: discord.Interaction, button: discord.ui.Button):
        con = db()
        rows = con.execute(
            """SELECT m.id, m.status, b.collection, b.amount AS buyer_offer,
                      s.amount AS seller_asking
               FROM matches m
               JOIN requests b ON b.id=m.buy_request_id
               JOIN requests s ON s.id=m.sell_request_id
               WHERE (b.user_id=? OR s.user_id=?)
               ORDER BY m.created_at DESC LIMIT 10""",
            (interaction.user.id, interaction.user.id),
        ).fetchall()
        con.close()

        if not rows:
            await interaction.response.send_message(
                "🎯 You have no matches yet.", ephemeral=True
            )
            return

        lines = ["🎯 **YOUR MATCHES**"]
        for row in rows:
            lines.append(
                f"• #{row['id']} — {row['collection']} — "
                f"Buyer: {row['buyer_offer']:g} SOL / "
                f"Seller: {row['seller_asking']:g} SOL — "
                f"`{row['status']}`"
            )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(
        label="MY REQUESTS",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="quicksell:requests",
    )
    async def requests(self, interaction: discord.Interaction, button: discord.ui.Button):
        con = db()
        rows = con.execute(
            """SELECT id, request_type, collection, amount, active
               FROM requests WHERE user_id=? ORDER BY created_at DESC LIMIT 10""",
            (interaction.user.id,),
        ).fetchall()
        con.close()

        if not rows:
            await interaction.response.send_message(
                "📋 You have no requests.", ephemeral=True
            )
            return

        lines = ["📋 **YOUR REQUESTS**"]
        for row in rows:
            state = "ACTIVE" if row["active"] else "CLOSED"
            lines.append(
                f"• #{row['id']} — {row['request_type']} — "
                f"{row['collection']} — {row['amount']:g} SOL — `{state}`"
            )

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class QuickSellBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.match_task: Optional[asyncio.Task] = None

    async def setup_hook(self):
        init_db()
        self.add_view(MainView())
        self.match_task = asyncio.create_task(matching_loop(self))

        # Keep global command synchronization explicit.
        try:
            await self.tree.sync()
        except Exception as exc:
            print(f"[QuickSell] command sync failed: {exc}")

    async def on_ready(self):
        print(f"[QuickSell] connected as {self.user} (id={self.user.id})")
        print("[QuickSell] matching engine running every 1 second")


bot = QuickSellBot()


@bot.tree.command(name="quicksell", description="Open the QuickSell marketplace")
async def quicksell(interaction: discord.Interaction):
    await interaction.response.send_message(
        "👻 **QUICKSELL**\n\n"
        "Find a real NFT buyer or seller.\n"
        "Search/matching is free. Contact access is paid only after a "
        "real potential match is found.\n\n"
        "💡 Prices can be negotiated directly between the two parties.",
        view=MainView(),
        ephemeral=True,
    )


@bot.tree.command(name="help", description="How QuickSell works")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "ℹ️ **HOW QUICKSELL WORKS**\n\n"
        "1. Create a BUY or SELL request.\n"
        "2. QuickSell searches active requests for a real counterparty.\n"
        "3. Price differences do not block a potential match.\n"
        "4. Contact access is unlocked with an active pass.\n"
        "5. Both parties discuss the price privately.\n"
        "6. Both parties must confirm before the deal becomes "
        "**DEAL_CONFIRMED**.\n\n"
        "QuickSell connects the parties; it does not guarantee the "
        "NFT transfer or payment between them.",
        ephemeral=True,
    )


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
    bot.run(token)
