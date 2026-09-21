"""
QuickSell - Discord-only NFT buyer/seller matching marketplace.

Flow:
1) BUY/SELL request is free.
2) Matching runs continuously against real active requests.
3) Price NEVER blocks a potential match.
4) Only after a real match, contact is locked behind a 24H/48H pass.
5) Payment is verified on-chain on Solana before access is granted.
6) A private deal room is created for buyer + seller + bot.
7) Both sides must confirm the negotiated deal.

Secrets are read from environment variables.
Required: DISCORD_TOKEN
Recommended for payments: QUICKSELL_RPC_URL and QUICKSELL_PAYMENT_WALLET
"""

import os
import asyncio
import json
import sqlite3
import time
import urllib.request
import urllib.error
from enum import Enum
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

DB_PATH = os.getenv("QUICKSELL_DB", "quicksell.db")
MATCH_INTERVAL = 1.0
QUICKSELL_CATEGORY = "🛒 QUICKSELL"
QUICKSELL_CHANNEL = "🛒-quicksell"
PRIVATE_MATCH_CATEGORY = "🔐 PRIVATE MATCHES"
QUICKSELL_PAYMENT_WALLET = os.getenv(
    "QUICKSELL_PAYMENT_WALLET",
    "Hj142M1XAPZb8T3SiawuDyxuCt2Z8SXmKSR1CdQKLZWx",
)
QUICKSELL_RPC_URL = os.getenv("QUICKSELL_RPC_URL", "https://api.mainnet-beta.solana.com")
PASS_24H_SOL = os.getenv("QUICKSELL_24H_SOL")
PASS_48H_SOL = os.getenv("QUICKSELL_48H_SOL")
PAYMENT_POLL_SECONDS = 5
PAYMENT_MAX_AGE_SECONDS = 60 * 60 * 6
DEAL_CHANNEL_TTL_HOURS = 48


class RequestType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class DealStatus(str, Enum):
    POTENTIAL_MATCH = "POTENTIAL_MATCH"
    CONTACT_UNLOCKED = "CONTACT_UNLOCKED"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    DEAL_CONFIRMED = "DEAL_CONFIRMED"
    DECLINED = "DECLINED"
    CLOSED = "CLOSED"


class PaymentStatus(str, Enum):
    WAITING = "WAITING"
    VERIFYING = "VERIFYING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    ALREADY_USED = "ALREADY_USED"


def now() -> float:
    return time.time()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def add_column_if_missing(con: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    con = db()
    con.executescript(
        """
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
            channel_id INTEGER,
            channel_name TEXT,
            created_at REAL NOT NULL,
            closed_at REAL,
            UNIQUE(buy_request_id, sell_request_id)
        );

        CREATE TABLE IF NOT EXISTS access_passes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            match_id INTEGER,
            plan_hours INTEGER NOT NULL,
            expected_sol REAL,
            tx_signature TEXT UNIQUE,
            confirmed_at REAL,
            expires_at REAL,
            status TEXT NOT NULL DEFAULT 'WAITING',
            created_at REAL NOT NULL
        );
        """
    )
    add_column_if_missing(con, "matches", "channel_id", "INTEGER")
    add_column_if_missing(con, "matches", "channel_name", "TEXT")
    add_column_if_missing(con, "matches", "closed_at", "REAL")
    add_column_if_missing(con, "access_passes", "match_id", "INTEGER")
    add_column_if_missing(con, "access_passes", "expected_sol", "REAL")
    add_column_if_missing(con, "access_passes", "created_at", "REAL")
    con.execute("UPDATE access_passes SET created_at=? WHERE created_at IS NULL", (now(),))
    con.commit()
    con.close()


def collections_match(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def mints_match(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return True
    return a.strip() == b.strip()


def compatible(buy: sqlite3.Row, sell: sqlite3.Row) -> bool:
    # Deliberately NO price comparison here. A 300 SOL offer can match a 500 SOL ask.
    return (
        buy["active"] == 1
        and sell["active"] == 1
        and buy["guild_id"] == sell["guild_id"]
        and buy["user_id"] != sell["user_id"]
        and collections_match(buy["collection"], sell["collection"])
        and mints_match(buy["mint"], sell["mint"])
    )


def format_sol(value: float) -> str:
    return f"{value:g} SOL"


def plan_price(hours: int) -> Optional[float]:
    raw = PASS_24H_SOL if hours == 24 else PASS_48H_SOL if hours == 48 else None
    if raw is None:
        return None
    try:
        value = float(raw)
        return value if value > 0 else None
    except ValueError:
        return None


async def solana_rpc(method: str, params: list) -> Optional[dict]:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(
        QUICKSELL_RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        loop = asyncio.get_running_loop()
        def do_request():
            with urllib.request.urlopen(request, timeout=12) as response:
                return json.loads(response.read().decode())
        return await loop.run_in_executor(None, do_request)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


async def verify_solana_payment(signature: str, expected_sol: float) -> tuple[str, Optional[float]]:
    """Return (state, observed_amount). State is CONFIRMED, PENDING or INVALID."""
    status_response = await solana_rpc(
        "getSignatureStatuses",
        [[signature], {"searchTransactionHistory": True}],
    )
    if status_response is None:
        return "PENDING", None
    statuses = (status_response.get("result") or {}).get("value") or []
    status = statuses[0] if statuses else None
    if status is None:
        return "PENDING", None
    if status.get("err") is not None:
        return "INVALID", None
    confirmation = status.get("confirmationStatus")
    if confirmation not in {"confirmed", "finalized"}:
        return "PENDING", None

    tx_response = await solana_rpc(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
    )
    if tx_response is None:
        return "PENDING", None
    tx = (tx_response.get("result") or None)
    if tx is None:
        return "PENDING", None
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return "INVALID", None

    message = (((tx.get("transaction") or {}).get("message")) or {})
    account_keys = message.get("accountKeys") or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in account_keys]
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    observed_lamports = 0
    for idx, key in enumerate(keys):
        if key == QUICKSELL_PAYMENT_WALLET and idx < len(pre) and idx < len(post):
            observed_lamports += post[idx] - pre[idx]
    observed_sol = observed_lamports / 1_000_000_000
    if observed_sol + 1e-9 < expected_sol:
        return "INVALID", observed_sol
    return "CONFIRMED", observed_sol


async def notify_match(bot: commands.Bot, match_id: int, buy: sqlite3.Row, sell: sqlite3.Row) -> None:
    buyer = bot.get_user(buy["user_id"]) or await safe_fetch_user(bot, buy["user_id"])
    seller = bot.get_user(sell["user_id"]) or await safe_fetch_user(bot, sell["user_id"])
    buyer_text = (
        "🎯 **INTERESTED SELLER FOUND**\n\n"
        f"Collection: **{buy['collection']}**\n"
        f"Seller asking price: **{format_sol(sell['amount'])}**\n"
        f"Your offer/budget: **{format_sol(buy['amount'])}**\n\n"
        "💬 The price can be negotiated directly.\n"
        "🔒 Contact is unlocked only after the access pass is verified.\n\n"
        f"Match ID: **#{match_id}**"
    )
    seller_text = (
        "🎯 **INTERESTED BUYER FOUND**\n\n"
        f"Collection: **{sell['collection']}**\n"
        f"Your asking price: **{format_sol(sell['amount'])}**\n"
        f"Buyer offer: **{format_sol(buy['amount'])}**\n\n"
        "💬 The price can be negotiated directly.\n"
        "🔒 Contact is unlocked only after the access pass is verified.\n\n"
        f"Match ID: **#{match_id}**"
    )
    if buyer:
        try:
            await buyer.send(buyer_text, view=MatchContactView(match_id))
        except discord.HTTPException:
            pass
    if seller:
        try:
            await seller.send(seller_text, view=MatchContactView(match_id))
        except discord.HTTPException:
            pass


async def safe_fetch_user(bot: commands.Bot, user_id: int):
    try:
        return await bot.fetch_user(user_id)
    except (discord.NotFound, discord.HTTPException):
        return None


async def matching_loop(bot: commands.Bot):
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            con = db()
            buys = con.execute("SELECT * FROM requests WHERE request_type='BUY' AND active=1").fetchall()
            sells = con.execute("SELECT * FROM requests WHERE request_type='SELL' AND active=1").fetchall()
            for buy in buys:
                for sell in sells:
                    if not compatible(buy, sell):
                        continue
                    exists = con.execute(
                        "SELECT id FROM matches WHERE buy_request_id=? AND sell_request_id=?",
                        (buy["id"], sell["id"]),
                    ).fetchone()
                    if exists:
                        continue
                    cur = con.execute(
                        """INSERT INTO matches
                           (buy_request_id, sell_request_id, status, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (buy["id"], sell["id"], DealStatus.POTENTIAL_MATCH.value, now()),
                    )
                    con.commit()
                    match_id = cur.lastrowid
                    await notify_match(bot, match_id, buy, sell)
            con.close()
        except Exception:
            print("[QuickSell] matching loop error", flush=True)
        await asyncio.sleep(MATCH_INTERVAL)


class RequestModal(discord.ui.Modal):
    def __init__(self, request_type: RequestType):
        super().__init__(title="QuickSell • Create Request")
        self.request_type = request_type
        self.collection = discord.ui.TextInput(
            label="NFT collection", placeholder="Enter the collection name", required=True, max_length=100
        )
        self.amount = discord.ui.TextInput(
            label="Amount in SOL", placeholder="Enter your amount in SOL", required=True, max_length=30
        )
        self.mint = discord.ui.TextInput(
            label="Mint address (optional)", placeholder="Leave empty if you do not have a specific mint", required=False, max_length=80
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
            await interaction.response.send_message("❌ Enter a valid SOL amount greater than 0.", ephemeral=True)
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This request must be created inside a server.", ephemeral=True)
            return
        con = db()
        con.execute(
            """INSERT INTO requests
               (guild_id,user_id,request_type,collection,mint,amount,active,created_at)
               VALUES (?,?,?,?,?,?,1,?)""",
            (interaction.guild_id, interaction.user.id, self.request_type.value,
             str(self.collection.value).strip(), str(self.mint.value).strip() or None, amount, now()),
        )
        con.commit()
        con.close()
        label = "BUY" if self.request_type == RequestType.BUY else "SELL"
        await interaction.response.send_message(
            f"✅ **{label} request created.**\n\n"
            f"Collection: **{self.collection.value}**\nAmount: **{format_sol(amount)}**\n\n"
            "🎯 QuickSell is now checking for a real interested counterparty.\n"
            "Different prices do not block a potential match.",
            ephemeral=True,
        )


class MatchContactView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=24 * 60 * 60)
        self.match_id = match_id

    @discord.ui.button(label="Contact Interested Party", emoji="📩", style=discord.ButtonStyle.success)
    async def contact(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_contact(interaction, self.match_id)


async def handle_contact(interaction: discord.Interaction, match_id: int):
    match = get_match_for_user(interaction.user.id, match_id)
    if not match:
        await interaction.response.send_message("❌ This match is not available to you.", ephemeral=True)
        return
    if match["status"] in {DealStatus.DECLINED.value, DealStatus.CLOSED.value, DealStatus.DEAL_CONFIRMED.value}:
        await interaction.response.send_message("❌ This match is already closed.", ephemeral=True)
        return
    if has_active_pass(interaction.user.id, match_id):
        await unlock_contact(interaction, match_id)
        return
    await interaction.response.send_message(
        embed=access_embed(match),
        view=AccessPlanView(match_id),
        ephemeral=True,
    )


def get_match_for_user(user_id: int, match_id: int) -> Optional[sqlite3.Row]:
    con = db()
    row = con.execute(
        """SELECT m.*, b.user_id buyer_id, b.collection, b.mint,
                  b.amount buyer_offer, b.guild_id, s.user_id seller_id, s.amount seller_asking
           FROM matches m
           JOIN requests b ON b.id=m.buy_request_id
           JOIN requests s ON s.id=m.sell_request_id
           WHERE m.id=? AND (b.user_id=? OR s.user_id=?)""",
        (match_id, user_id, user_id),
    ).fetchone()
    con.close()
    return row


def has_active_pass(user_id: int, match_id: int) -> bool:
    con = db()
    row = con.execute(
        "SELECT id FROM access_passes WHERE user_id=? AND match_id=? AND status='CONFIRMED' AND expires_at>?",
        (user_id, match_id, now()),
    ).fetchone()
    con.close()
    return row is not None


def access_embed(match: sqlite3.Row) -> discord.Embed:
    p24 = plan_price(24)
    p48 = plan_price(48)
    price24 = format_sol(p24) if p24 is not None else "not configured"
    price48 = format_sol(p48) if p48 is not None else "not configured"
    return discord.Embed(
        title="🔒 Contact Locked",
        description=(
            "A **real potential match** has been found. Search was free.\n\n"
            f"Collection: **{match['collection']}**\n"
            f"Seller asking: **{format_sol(match['seller_asking'])}**\n"
            f"Buyer offer: **{format_sol(match['buyer_offer'])}**\n\n"
            "💬 Price differences do not block contact; the parties can negotiate.\n\n"
            f"⚡ **24H access:** {price24}\n"
            f"🚀 **48H access:** {price48}\n\n"
            "Payment is accepted only after on-chain Solana verification."
        ),
        color=discord.Color.gold(),
    )


class AccessPlanView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=15 * 60)
        self.match_id = match_id

    @discord.ui.button(label="24H", emoji="⚡", style=discord.ButtonStyle.primary)
    async def p24(self, interaction: discord.Interaction, button: discord.ui.Button):
        await start_payment(interaction, self.match_id, 24)

    @discord.ui.button(label="48H", emoji="🚀", style=discord.ButtonStyle.success)
    async def p48(self, interaction: discord.Interaction, button: discord.ui.Button):
        await start_payment(interaction, self.match_id, 48)


async def start_payment(interaction: discord.Interaction, match_id: int, hours: int):
    amount = plan_price(hours)
    if amount is None:
        await interaction.response.send_message(
            "⚠️ This access price is not configured yet. The bot will not ask you to send money until the price is configured.",
            ephemeral=True,
        )
        return
    match = get_match_for_user(interaction.user.id, match_id)
    if not match:
        await interaction.response.send_message("❌ This match is not available to you.", ephemeral=True)
        return
    con = db()
    cur = con.execute(
        "INSERT INTO access_passes (user_id,match_id,plan_hours,expected_sol,status,created_at) VALUES (?,?,?,?,?,?)",
        (interaction.user.id, match_id, hours, amount, PaymentStatus.WAITING.value, now()),
    )
    pass_id = cur.lastrowid
    con.commit()
    con.close()
    await interaction.response.send_message(
        embed=discord.Embed(
            title=f"💳 {hours}H QuickSell Access",
            description=(
                f"Amount to send: **{format_sol(amount)}**\n\n"
                f"Solana wallet:\n`{QUICKSELL_PAYMENT_WALLET}`\n\n"
                "1. Send the exact amount on Solana.\n"
                "2. Copy the transaction signature.\n"
                "3. Click **Verify Payment** and submit the signature.\n\n"
                "⚠️ QuickSell unlocks contact only after the transaction is verified on-chain. "
                "Never send a seed phrase or private key."
            ),
            color=discord.Color.blurple(),
        ),
        view=PaymentVerifyView(pass_id),
        ephemeral=True,
    )


class SignatureModal(discord.ui.Modal):
    def __init__(self, pass_id: int):
        super().__init__(title="Verify Solana Payment")
        self.pass_id = pass_id
        self.signature = discord.ui.TextInput(
            label="Solana transaction signature",
            placeholder="Paste the transaction signature",
            required=True,
            min_length=40,
            max_length=100,
        )
        self.add_item(self.signature)

    async def on_submit(self, interaction: discord.Interaction):
        signature = str(self.signature.value).strip()
        con = db()
        used = con.execute(
            "SELECT id,status FROM access_passes WHERE tx_signature=? AND id<>?",
            (signature, self.pass_id),
        ).fetchone()
        if used:
            con.close()
            await interaction.response.send_message("❌ This transaction signature has already been used for another access pass.", ephemeral=True)
            return
        row = con.execute(
            "SELECT * FROM access_passes WHERE id=? AND user_id=?",
            (self.pass_id, interaction.user.id),
        ).fetchone()
        if not row:
            con.close()
            await interaction.response.send_message("❌ Payment request not found.", ephemeral=True)
            return
        con.execute("UPDATE access_passes SET tx_signature=?,status=? WHERE id=?", (signature, PaymentStatus.VERIFYING.value, self.pass_id))
        con.commit()
        con.close()
        await interaction.response.send_message("🔎 Payment verification started. I will check the Solana transaction now.", ephemeral=True)
        result = await verify_and_apply_payment(interaction, self.pass_id, signature)
        if result == "CONFIRMED":
            return
        if result == "PENDING":
            try:
                await interaction.followup.send(
                    "⏳ The RPC has not returned final proof yet. Verification is still pending; submit **Verify Payment** again later with the same signature. No access has been granted.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        else:
            try:
                await interaction.followup.send("❌ The transaction could not be verified as a valid payment to the QuickSell wallet. No access was granted.", ephemeral=True)
            except discord.HTTPException:
                pass


class PaymentVerifyView(discord.ui.View):
    def __init__(self, pass_id: int):
        super().__init__(timeout=15 * 60)
        self.pass_id = pass_id

    @discord.ui.button(label="Verify Payment", emoji="🔎", style=discord.ButtonStyle.success)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SignatureModal(self.pass_id))


async def verify_and_apply_payment(interaction: discord.Interaction, pass_id: int, signature: str) -> str:
    con = db()
    row = con.execute("SELECT * FROM access_passes WHERE id=? AND user_id=?", (pass_id, interaction.user.id)).fetchone()
    con.close()
    if not row:
        return "INVALID"
    state, observed = await verify_solana_payment(signature, row["expected_sol"])
    con = db()
    if state == "CONFIRMED":
        confirmed = now()
        expires = confirmed + row["plan_hours"] * 3600
        con.execute(
            "UPDATE access_passes SET status='CONFIRMED',confirmed_at=?,expires_at=? WHERE id=? AND status='VERIFYING'",
            (confirmed, expires, pass_id),
        )
        con.execute(
            "UPDATE matches SET status=? WHERE id=? AND status=?",
            (DealStatus.CONTACT_UNLOCKED.value, row["match_id"], DealStatus.POTENTIAL_MATCH.value),
        )
        con.commit()
        con.close()
        try:
            await interaction.followup.send(
                f"✅ **Payment confirmed on-chain.**\n\nAccess: **{row['plan_hours']}H**\nExpires: <t:{int(expires)}:F>\n\nYour contact access is now unlocked.",
                ephemeral=True,
                view=DealAccessView(row["match_id"]),
            )
        except discord.HTTPException:
            pass
        return "CONFIRMED"
    if state == "INVALID":
        con.execute("UPDATE access_passes SET status='FAILED' WHERE id=?", (pass_id,))
        con.commit()
    con.close()
    return state


class DealAccessView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=24 * 60 * 60)
        self.match_id = match_id

    @discord.ui.button(label="📩 Open Private Deal Room", style=discord.ButtonStyle.success)
    async def open_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        await unlock_contact(interaction, self.match_id)


async def unlock_contact(interaction: discord.Interaction, match_id: int):
    match = get_match_for_user(interaction.user.id, match_id)
    if not match:
        await interaction.response.send_message("❌ Match not found.", ephemeral=True)
        return
    if not has_active_pass(interaction.user.id, match_id):
        await interaction.response.send_message("🔒 Your contact access has expired or is not confirmed.", ephemeral=True)
        return
    guild = interaction.guild
    if guild is None or guild.id != match["guild_id"]:
        await interaction.response.send_message("❌ Use this inside the QuickSell server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    channel = await ensure_deal_channel(guild, match)
    if channel is None:
        await interaction.followup.send("❌ I could not create the private deal room. Check Manage Channels permissions.", ephemeral=True)
        return
    await interaction.followup.send(
        f"✅ **Contact unlocked.**\n\nPrivate deal room: {channel.mention}\n\n"
        "Discuss the price directly. Both buyer and seller must confirm the final deal.",
        ephemeral=True,
    )


def match_users(match: sqlite3.Row) -> tuple[int, int]:
    return int(match["buyer_id"]), int(match["seller_id"])


FINAL_CONFIRMATION_PROMPTS = {}


async def disable_confirmation_prompt(bot: commands.Bot, match_id: int):
    message_id = FINAL_CONFIRMATION_PROMPTS.pop(match_id, None)
    if not message_id:
        return
    match = get_match_for_user(0, match_id) if False else None
    # The prompt is only an in-memory UX helper; DB state remains authoritative.
    for guild in bot.guilds:
        channel = None
        con = db()
        row = con.execute("SELECT channel_id FROM matches WHERE id=?", (match_id,)).fetchone()
        con.close()
        if row and row["channel_id"]:
            channel = guild.get_channel(row["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            message = await channel.fetch_message(message_id)
            view = message.components
            # Replace the old interactive view with disabled buttons.
            disabled = DealConfirmView(match_id, disabled=True)
            await message.edit(view=disabled)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        break


async def close_deal_channel(channel: Optional[discord.TextChannel], delay: int = 10):
    if channel is None:
        return
    await asyncio.sleep(delay)
    try:
        await channel.delete(reason="QuickSell deal closed")
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        print(f"[QuickSell] deal channel deletion failed: {exc}")


async def ensure_deal_channel(guild: discord.Guild, match: sqlite3.Row):
    existing = guild.get_channel(match["channel_id"]) if match["channel_id"] else None
    if isinstance(existing, discord.TextChannel):
        return existing
    category = discord.utils.get(guild.categories, name=PRIVATE_MATCH_CATEGORY)
    if category is None:
        category = await guild.create_category(PRIVATE_MATCH_CATEGORY, reason="QuickSell private deal rooms")
    buyer = guild.get_member(match["buyer_id"]) or await guild.fetch_member(match["buyer_id"])
    seller = guild.get_member(match["seller_id"]) or await guild.fetch_member(match["seller_id"])
    bot_member = guild.me
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        buyer: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        seller: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if bot_member:
        overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
    channel = await guild.create_text_channel(
        f"deal-{match['id']:04d}", category=category, overwrites=overwrites,
        topic="QuickSell private buyer/seller negotiation room", reason="QuickSell real potential match"
    )
    con = db()
    con.execute("UPDATE matches SET channel_id=?,channel_name=?,status=?,buyer_confirmed=0,seller_confirmed=0 WHERE id=?", (channel.id, channel.name, DealStatus.WAITING_CONFIRMATION.value, match["id"]))
    con.commit()
    con.close()
    embed = discord.Embed(
        title="🤝 QUICKSELL DEAL ROOM",
        description=(
            f"NFT / Collection: **{match['collection']}**\n"
            f"Seller asking price: **{format_sol(match['seller_asking'])}**\n"
            f"Buyer offer: **{format_sol(match['buyer_offer'])}**\n\n"
            "💬 **DISCUSS THE PRICE HERE**\n"
            "Continue the negotiation directly in this private room. You can discuss the final price and conditions freely.\n\n"
            "⚠️ **DO NOT CONFIRM YET.**\n"
            "When you are both genuinely finished negotiating, use `/confirm` to open the final confirmation step."
        ), color=discord.Color.blurple()
    )
    await channel.send(content=f"<@{match['buyer_id']}> <@{match['seller_id']}>", embed=embed)
    await channel.send(
        "💬 **NEGOTIATION IN PROGRESS**\n\n"
        "Talk to each other first. When you are finished, type `/confirm`.\n"
        "`/confirm` does **not** accept the deal; it only opens the final confirmation buttons at the bottom."
    )
    return channel


class DealConfirmView(discord.ui.View):
    def __init__(self, match_id: int, disabled: bool = False):
        super().__init__(timeout=15 * 60)
        self.match_id = match_id
        self.disabled_state = disabled

    @discord.ui.button(label="I AGREE TO THE DEAL", emoji="🤝", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.disabled_state:
            await interaction.response.send_message("⚠️ This confirmation is no longer valid. If negotiation continues, use `/confirm` again.", ephemeral=True)
            return
        await confirm_deal(interaction, self.match_id, True)

    @discord.ui.button(label="I DON'T AGREE", emoji="❌", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.disabled_state:
            await interaction.response.send_message("⚠️ This confirmation is no longer valid. If negotiation continues, use `/confirm` again.", ephemeral=True)
            return
        await confirm_deal(interaction, self.match_id, False)


async def confirm_deal(interaction: discord.Interaction, match_id: int, agree: bool):
    match = get_match_for_user(interaction.user.id, match_id)
    if not match or match["channel_id"] != getattr(interaction.channel, "id", None):
        await interaction.response.send_message("❌ This deal is not available in this channel.", ephemeral=True)
        return
    if match["status"] in {DealStatus.DEAL_CONFIRMED.value, DealStatus.DECLINED.value, DealStatus.CLOSED.value}:
        await interaction.response.send_message("❌ This deal is already closed.", ephemeral=True)
        return
    con = db()
    if not agree:
        con.execute("UPDATE matches SET status=?,closed_at=? WHERE id=?", (DealStatus.DECLINED.value, now(), match_id))
        con.commit()
        con.close()
        FINAL_CONFIRMATION_PROMPTS.pop(match_id, None)
        await interaction.response.send_message("❌ **DEAL DECLINED** — your side did not agree. The deal is now closed.")
        asyncio.create_task(close_deal_channel(interaction.channel))
        return
    if interaction.user.id == match["buyer_id"]:
        con.execute("UPDATE matches SET buyer_confirmed=1 WHERE id=?", (match_id,))
    else:
        con.execute("UPDATE matches SET seller_confirmed=1 WHERE id=?", (match_id,))
    con.commit()
    row = con.execute("SELECT buyer_confirmed,seller_confirmed FROM matches WHERE id=?", (match_id,)).fetchone()
    con.close()
    if row["buyer_confirmed"] and row["seller_confirmed"]:
        con = db()
        con.execute("UPDATE matches SET status=?,closed_at=? WHERE id=?", (DealStatus.DEAL_CONFIRMED.value, now(), match_id))
        con.commit()
        con.close()
        FINAL_CONFIRMATION_PROMPTS.pop(match_id, None)
        await interaction.response.send_message(
            "🎉 **DEAL CONFIRMED**\n\nBoth buyer and seller confirmed the final deal.\n\n🗑️ This private Deal Room will be closed and removed shortly."
        )
        asyncio.create_task(close_deal_channel(interaction.channel))
        return
    waiting_for = "seller" if interaction.user.id == match["buyer_id"] else "buyer"
    await interaction.response.send_message(
        f"🤝 Your final confirmation has been recorded. Waiting for the **{waiting_for}** to confirm.", ephemeral=True
    )


async def confirm_command(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("❌ Use `/confirm` inside your private QuickSell Deal Room.", ephemeral=True)
        return
    con = db()
    match = con.execute(
        "SELECT m.*, b.user_id buyer_id, b.collection, b.guild_id, s.user_id seller_id "
        "FROM matches m JOIN requests b ON b.id=m.buy_request_id JOIN requests s ON s.id=m.sell_request_id "
        "WHERE m.channel_id=?", (interaction.channel.id,)
    ).fetchone()
    con.close()
    if not match or interaction.user.id not in {match["buyer_id"], match["seller_id"]}:
        await interaction.response.send_message("❌ This is not your QuickSell Deal Room.", ephemeral=True)
        return
    if match["status"] in {DealStatus.DEAL_CONFIRMED.value, DealStatus.DECLINED.value, DealStatus.CLOSED.value}:
        await interaction.response.send_message("❌ This deal is already closed.", ephemeral=True)
        return
    con = db()
    con.execute("UPDATE matches SET buyer_confirmed=0,seller_confirmed=0,status=? WHERE id=?", (DealStatus.WAITING_CONFIRMATION.value, match["id"]))
    con.commit()
    con.close()
    old = FINAL_CONFIRMATION_PROMPTS.pop(match["id"], None)
    if old:
        try:
            old_message = await interaction.channel.fetch_message(old)
            await old_message.edit(view=DealConfirmView(match["id"], disabled=True))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    await interaction.response.send_message(
        "🔔 **FINAL CONFIRMATION**\n\n"
        "The negotiation appears to be finished. Review the final price and conditions one last time.\n\n"
        "⚠️ **Important:** Clicking **I AGREE TO THE DEAL** means you accept the final terms you discussed. "
        "If you are not ready, do not click it.\n\n"
        "If negotiation continues after this message, this confirmation will be cancelled and you can use `/confirm` again.",
        view=DealConfirmView(match["id"]),
    )
    # The response itself is the bottom-most confirmation prompt.
    try:
        # Interaction response is not directly addressable, so fetch the latest bot message.
        async for msg in interaction.channel.history(limit=5):
            if msg.author.id == interaction.client.user.id and msg.components:
                FINAL_CONFIRMATION_PROMPTS[match["id"]] = msg.id
                break
    except discord.HTTPException:
        pass


class MainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="I WANT TO BUY", emoji="🛒", style=discord.ButtonStyle.primary, custom_id="quicksell:buy")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RequestModal(RequestType.BUY))

    @discord.ui.button(label="I WANT TO SELL", emoji="💰", style=discord.ButtonStyle.success, custom_id="quicksell:sell")
    async def sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RequestModal(RequestType.SELL))

    @discord.ui.button(label="MY MATCHES", emoji="🎯", style=discord.ButtonStyle.secondary, custom_id="quicksell:matches")
    async def matches(self, interaction: discord.Interaction, button: discord.ui.Button):
        con = db()
        rows = con.execute(
            """SELECT m.id,m.status,b.collection,b.amount buyer_offer,s.amount seller_asking,
                      b.user_id buyer_id,s.user_id seller_id
               FROM matches m JOIN requests b ON b.id=m.buy_request_id JOIN requests s ON s.id=m.sell_request_id
               WHERE b.user_id=? OR s.user_id=? ORDER BY m.created_at DESC LIMIT 10""",
            (interaction.user.id, interaction.user.id),
        ).fetchall()
        con.close()
        if not rows:
            await interaction.response.send_message("🎯 You have no matches yet.", ephemeral=True)
            return
        embed = discord.Embed(title="🎯 YOUR MATCHES", color=discord.Color.blurple())
        for row in rows:
            embed.add_field(
                name=f"#{row['id']} • {row['collection']} • {row['status']}",
                value=f"Buyer offer: {format_sol(row['buyer_offer'])}\nSeller asking: {format_sol(row['seller_asking'])}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, view=MyMatchesView(rows), ephemeral=True)

    @discord.ui.button(label="MY REQUESTS", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="quicksell:requests")
    async def requests(self, interaction: discord.Interaction, button: discord.ui.Button):
        con = db()
        rows = con.execute("SELECT id,request_type,collection,amount,active FROM requests WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (interaction.user.id,)).fetchall()
        con.close()
        if not rows:
            await interaction.response.send_message("📋 You have no requests.", ephemeral=True)
            return
        lines = ["📋 **YOUR REQUESTS**"]
        for row in rows:
            state = "ACTIVE" if row["active"] else "CLOSED"
            lines.append(f"• #{row['id']} — {row['request_type']} — {row['collection']} — {format_sol(row['amount'])} — `{state}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="HOW IT WORKS", emoji="ℹ️", style=discord.ButtonStyle.secondary, custom_id="quicksell:help")
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "ℹ️ **HOW QUICKSELL WORKS**\n\n"
            "1. BUY or SELL is free.\n"
            "2. QuickSell checks real active requests continuously.\n"
            "3. Matching uses collection/mint compatibility; price does not block the match.\n"
            "4. After a real potential match, contact is locked.\n"
            "5. A 24H or 48H pass is verified on-chain before contact is unlocked.\n"
            "6. Buyer and seller negotiate privately.\n"
            "7. Both must confirm the final deal for DEAL_CONFIRMED.\n\n"
            "QuickSell connects the parties; it does not guarantee the NFT transfer or payment between them.",
            ephemeral=True,
        )

    @discord.ui.button(label="PAYMENT WALLET", emoji="💳", style=discord.ButtonStyle.secondary, custom_id="quicksell:payment_wallet")
    async def wallet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"💳 **QuickSell payment wallet**\n`{QUICKSELL_PAYMENT_WALLET}`\n\n"
            "Payments are verified on-chain before access is granted.", ephemeral=True
        )


class MyMatchesView(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=15 * 60)
        for row in rows[:5]:
            if row["status"] not in {DealStatus.DECLINED.value, DealStatus.CLOSED.value, DealStatus.DEAL_CONFIRMED.value}:
                button = discord.ui.Button(label=f"Contact #{row['id']}", emoji="📩", style=discord.ButtonStyle.success)
                button.callback = self.make_callback(row["id"])
                self.add_item(button)

    def make_callback(self, match_id: int):
        async def callback(interaction: discord.Interaction):
            await handle_contact(interaction, match_id)
        return callback


class QuickSellBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.match_task: Optional[asyncio.Task] = None

    async def setup_hook(self):
        init_db()
        self.add_view(MainView())
        self.match_task = asyncio.create_task(matching_loop(self))
        try:
            await self.tree.sync()
        except Exception as exc:
            print(f"[QuickSell] command sync failed: {exc}")

    async def ensure_public_quicksell(self, guild: discord.Guild):
        me = guild.me
        if me is None:
            return None
        category = discord.utils.get(guild.categories, name=QUICKSELL_CATEGORY)
        if category is None:
            category = await guild.create_category(QUICKSELL_CATEGORY, reason="QuickSell public marketplace")
        channel = discord.utils.get(category.text_channels, name=QUICKSELL_CHANNEL)
        if channel is None:
            channel = await guild.create_text_channel(QUICKSELL_CHANNEL, category=category, topic="QuickSell NFT buyer/seller marketplace", reason="QuickSell dedicated channel")
        try:
            await channel.set_permissions(guild.default_role, view_channel=True, read_message_history=True, send_messages=False)
        except discord.HTTPException:
            pass
        await self.remove_old_launchers(guild)
        found = False
        try:
            async for message in channel.history(limit=50):
                if message.author.id != self.user.id:
                    continue
                ids = [getattr(c, "custom_id", "") for r in message.components for c in r.children]
                if "quicksell:buy" in ids and "quicksell:sell" in ids:
                    found = True
                    break
        except discord.HTTPException:
            pass
        if not found:
            print(f"[QuickSell] no QuickSell menu found in #{QUICKSELL_CHANNEL}; publishing it now")
            embed = discord.Embed(
                title="👻 QuickSell",
                description=(
                    "🛒 **NFT buyer/seller matching marketplace**\n\n"
                    "🔎 Search and matching are FREE.\n"
                    "💬 Different prices do not block a potential match.\n"
                    "🔒 Contact is paid only after a real potential match.\n\n"
                    "Choose your action below."
                ), color=discord.Color.blurple()
            )
            try:
                await channel.send(embed=embed, view=MainView())
                print(f"[QuickSell] full menu published DIRECTLY in #{QUICKSELL_CHANNEL}")
            except discord.Forbidden:
                print(f"[QuickSell] CANNOT PUBLISH MENU IN #{QUICKSELL_CHANNEL}: check View Channel, Send Messages, Embed Links, and Read Message History permissions")
                raise
        else:
            print(f"[QuickSell] full menu already exists in #{QUICKSELL_CHANNEL}")
        return channel

    async def remove_old_launchers(self, guild: discord.Guild):
        """Remove legacy QuickSell launcher messages outside #🛒-quicksell.

        Older builds used a one-button launcher in #rules. New builds put the
        complete QuickSell menu directly in the dedicated channel. We therefore
        remove only bot-authored messages that are clearly QuickSell launchers;
        ordinary user messages and unrelated bot messages are untouched.
        """
        target_name = QUICKSELL_CHANNEL
        for channel in guild.text_channels:
            if channel.name == target_name and channel.category and channel.category.name == QUICKSELL_CATEGORY:
                continue
            try:
                async for message in channel.history(limit=200):
                    if self.user is None or message.author.id != self.user.id:
                        continue

                    component_ids = [
                        getattr(c, "custom_id", "")
                        for r in message.components
                        for c in r.children
                    ]
                    content = (message.content or "").casefold()
                    embed_text = " ".join(
                        [
                            (e.title or ""),
                            (e.description or ""),
                        ]
                        + [f.field.name + " " + f.field.value for e in message.embeds for f in e.fields]
                    ).casefold()

                    has_legacy_button = "quicksell:launch" in component_ids
                    looks_like_quicksell_launcher = (
                        "quicksell" in content or "quicksell" in embed_text
                    ) and any(
                        x in component_ids
                        for x in {"quicksell:launch", "quicksell:buy", "quicksell:sell"}
                    )

                    if not (has_legacy_button or looks_like_quicksell_launcher):
                        continue

                    try:
                        await message.delete(reason="Remove obsolete QuickSell launcher outside dedicated channel")
                        print(f"[QuickSell] removed legacy QuickSell message from #{channel.name}")
                    except discord.Forbidden:
                        print(f"[QuickSell] CANNOT REMOVE legacy QuickSell message from #{channel.name}: missing Manage Messages")
                    except discord.HTTPException as exc:
                        print(f"[QuickSell] legacy QuickSell deletion failed in #{channel.name}: {exc}")
            except discord.Forbidden:
                print(f"[QuickSell] cannot inspect #{channel.name}: missing Read Message History")
            except discord.HTTPException as exc:
                print(f"[QuickSell] cannot inspect #{channel.name}: {exc}")

    async def ensure_quicksell_invite(self, channel: discord.TextChannel):
        try:
            invites = await channel.invites()
            for invite in invites:
                if invite.max_age == 0 and invite.max_uses == 0 and not invite.revoked:
                    print(f"[QuickSell] permanent invite -> {invite.url}")
                    return invite.url
            invite = await channel.create_invite(max_age=0, max_uses=0, unique=False, reason="QuickSell direct channel invite")
            print(f"[QuickSell] permanent invite -> {invite.url}")
            return invite.url
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[QuickSell] invite creation failed: {exc}")
            return None

    async def on_message(self, message: discord.Message):
        if message.author.bot or not isinstance(message.channel, discord.TextChannel):
            return
        con = db()
        match = con.execute(
            "SELECT id,buyer_confirmed,seller_confirmed,status FROM matches WHERE channel_id=?",
            (message.channel.id,),
        ).fetchone()
        if match and match["status"] not in {DealStatus.DEAL_CONFIRMED.value, DealStatus.DECLINED.value, DealStatus.CLOSED.value}:
            if match["buyer_confirmed"] or match["seller_confirmed"]:
                con.execute("UPDATE matches SET buyer_confirmed=0,seller_confirmed=0,status=? WHERE id=?", (DealStatus.WAITING_CONFIRMATION.value, match["id"]))
                con.commit()
                prompt_id = FINAL_CONFIRMATION_PROMPTS.pop(match["id"], None)
                con.close()
                if prompt_id:
                    try:
                        prompt = await message.channel.fetch_message(prompt_id)
                        await prompt.edit(view=DealConfirmView(match["id"], disabled=True))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                await message.channel.send(
                    "💬 **NEGOTIATION RESUMED** — the final confirmation was cancelled because a new message was sent.\n"
                    "When you are finished again, use `/confirm`."
                )
            else:
                con.close()
        else:
            con.close()

    async def on_ready(self):
        print(f"[QuickSell] connected as {self.user} (id={self.user.id})")
        print("[QuickSell] matching engine running every 1 second")
        for guild in self.guilds:
            try:
                channel = await self.ensure_public_quicksell(guild)
                if channel:
                    await self.ensure_quicksell_invite(channel)
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"[QuickSell] setup failed in {guild.name}: {exc}")


bot = QuickSellBot()

# /confirm is registered after the bot instance exists.
bot.tree.command(name="confirm", description="Finish negotiation and open the final deal confirmation")(confirm_command)


@bot.tree.command(name="quicksell", description="Open the QuickSell marketplace")
async def quicksell(interaction: discord.Interaction):
    await interaction.response.send_message(
        "👻 **QUICKSELL**\n\nSearch/matching is free. Contact access is paid only after a real potential match.",
        view=MainView(), ephemeral=True
    )


@bot.tree.command(name="quicksell_invite", description="Get the direct QuickSell channel invite")
async def quicksell_invite(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this command inside the QuickSell server.", ephemeral=True)
        return
    channel = discord.utils.get(interaction.guild.text_channels, name=QUICKSELL_CHANNEL)
    if channel is None:
        await interaction.response.send_message("❌ QuickSell channel is not ready.", ephemeral=True)
        return
    url = await bot.ensure_quicksell_invite(channel)
    if not url:
        await interaction.response.send_message("❌ Could not create the invite. Check Create Invite permission.", ephemeral=True)
        return
    await interaction.response.send_message(f"🛒 **DIRECT QUICKSELL INVITE**\n\n{url}", ephemeral=True)


@bot.tree.command(name="help", description="How QuickSell works")
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "ℹ️ **QUICKSELL**\n\nBUY/SELL is free → real matching → contact pass → on-chain Solana verification → private negotiation → both sides confirm → DEAL_CONFIRMED.\n\n"
        "QuickSell does not guarantee the NFT transfer or payment between traders.", ephemeral=True
    )


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
    bot.run(token)
