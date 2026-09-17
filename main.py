"""NFT Market Bot - Discord buyer/seller matching and Solana cryptographic wallet verification.

This bot manages:
- NFT buy/sell registration (/sell, /buy)
- Matching compatible collections & budgets (/matches)
- Private temporary deal channels (deal-XXXXXXXX) under "🔐 PRIVATE MARKET"
- Real Solana cryptographic wallet verification via signature (/wallet, /wallet-status, /wallet-remove)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import secrets
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import quote

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


PRIVATE_MARKET_CATEGORY = "🔐 PRIVATE MARKET"
DEAL_CHANNEL_TTL_HOURS = 24
WALLET_NONCE_TTL_SECONDS = 5 * 60
WALLET_MAX_SIGNATURE_ATTEMPTS = 3
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class PrivateDealSetupError(RuntimeError):
    """Raised when Discord cannot create a private deal channel safely."""


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

log_formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(log_formatter)
file_handler = logging.FileHandler("bot.log", mode="a", encoding="utf-8")
file_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[stdout_handler, file_handler],
)
logger = logging.getLogger("nft-market-bot")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(value: str) -> str:
    """Collapse whitespace and keep user-entered values easy to read."""
    return " ".join(value.strip().split())


def parse_amount(value: str) -> Optional[Decimal]:
    """Parse a positive amount without accepting malformed numeric input."""
    try:
        amount = Decimal(clean_text(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


def format_amount(amount: Decimal, currency: str) -> str:
    formatted = f"{amount:,.4f}".rstrip("0").rstrip(".")
    return f"{formatted} {currency}"


def compatible_collection(left: str, right: str) -> bool:
    """Allow exact names and simple name variations such as 'Cool Cats NFT'."""
    first = clean_text(left).casefold()
    second = clean_text(right).casefold()
    return first == second or first in second or second in first


@dataclass
class Listing:
    listing_id: str
    kind: str
    guild_id: int
    user_id: int
    user_name: str
    collection: str
    currency: str
    amount: Decimal
    details: str
    mint_address: str = ""
    active: bool = True
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class Match:
    match_id: str
    guild_id: int
    seller: Listing
    buyer: Listing
    channel_id: Optional[int] = None
    channel_name: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    closed_at: Optional[datetime] = None
    status: str = "MATCHED"


@dataclass
class WalletSession:
    token: str
    discord_user_id: int
    nonce: str
    created_at: datetime = field(default_factory=utc_now)
    attempts: int = 0
    used: bool = False

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(
            self.created_at.timestamp() + WALLET_NONCE_TTL_SECONDS,
            tz=timezone.utc,
        )


@dataclass
class WalletAssociation:
    discord_user_id: int
    public_key: str
    verified_at: datetime = field(default_factory=utc_now)


class WalletStore:
    """Short-lived nonce sessions and one-to-one verified wallet associations."""

    def __init__(self) -> None:
        self.sessions: dict[str, WalletSession] = {}
        self.by_user: dict[int, WalletAssociation] = {}
        self.user_by_wallet: dict[str, int] = {}

    def create_session(self, discord_user_id: int) -> WalletSession:
        now = utc_now()
        self.sessions = {
            token: session
            for token, session in self.sessions.items()
            if not session.used and session.expires_at > now
        }
        session = WalletSession(
            token=secrets.token_urlsafe(32),
            discord_user_id=discord_user_id,
            nonce=secrets.token_urlsafe(24),
            created_at=now,
        )
        self.sessions[session.token] = session
        logger.info(
            "Wallet verification requested for Discord user=%s, session=%s.",
            discord_user_id,
            session.token[:8],
        )
        return session

    def get_session(self, token: str) -> Optional[WalletSession]:
        return self.sessions.get(token)

    @staticmethod
    def verification_message(session: WalletSession) -> str:
        return (
            "NFT Market Wallet Verification\n"
            f"Discord User ID: {session.discord_user_id}\n"
            f"Nonce: {session.nonce}"
        )

    def get_message(self, token: str) -> Optional[tuple[str, datetime]]:
        session = self.get_session(token)
        if (
            session is None
            or session.used
            or session.expires_at <= utc_now()
            or session.attempts >= WALLET_MAX_SIGNATURE_ATTEMPTS
        ):
            return None
        return self.verification_message(session), session.expires_at

    def verify(
        self, token: str, public_key: str, encoded_signature: str
    ) -> tuple[bool, str, Optional[WalletAssociation]]:
        session = self.get_session(token)
        if session is None or session.used:
            return False, "This verification link is invalid or already used.", None
        if session.expires_at <= utc_now():
            session.used = True
            return False, "This verification link has expired. Run /wallet again.", None
        if session.attempts >= WALLET_MAX_SIGNATURE_ATTEMPTS:
            session.used = True
            return False, "Too many signature attempts. Run /wallet again.", None

        session.attempts += 1
        try:
            public_key_bytes = decode_base58(public_key)
            try:
                signature_bytes = base64.b64decode(encoded_signature, validate=True)
                if len(signature_bytes) != 64:
                    signature_bytes = decode_base58(encoded_signature)
            except (binascii.Error, ValueError):
                signature_bytes = decode_base58(encoded_signature)

            if len(public_key_bytes) != 32 or len(signature_bytes) != 64:
                raise ValueError("Unexpected Solana key or signature length.")
            VerifyKey(public_key_bytes).verify(
                self.verification_message(session).encode("utf-8"),
                signature_bytes,
            )
        except (BadSignatureError, ValueError, binascii.Error, TypeError):
            if session.attempts >= WALLET_MAX_SIGNATURE_ATTEMPTS:
                session.used = True
            return False, "The wallet signature is invalid.", None

        existing_user_id = self.user_by_wallet.get(public_key)
        if existing_user_id is not None and existing_user_id != session.discord_user_id:
            session.used = True
            return (
                False,
                "This wallet is already associated with another Discord account.",
                None,
            )

        existing_association = self.by_user.get(session.discord_user_id)
        if (
            existing_association is not None
            and existing_association.public_key != public_key
        ):
            session.used = True
            return (
                False,
                "A different wallet is already associated. Use /wallet-remove first.",
                None,
            )

        association = WalletAssociation(
            discord_user_id=session.discord_user_id,
            public_key=public_key,
        )
        self.by_user[session.discord_user_id] = association
        self.user_by_wallet[public_key] = session.discord_user_id
        session.used = True
        return True, "Wallet verified successfully.", association

    def get_association(self, discord_user_id: int) -> Optional[WalletAssociation]:
        return self.by_user.get(discord_user_id)

    def remove_association(self, discord_user_id: int) -> Optional[WalletAssociation]:
        association = self.by_user.pop(discord_user_id, None)
        if association is not None:
            self.user_by_wallet.pop(association.public_key, None)
            logger.info(
                "Wallet removed for Discord user=%s, wallet=%s.",
                discord_user_id,
                short_public_key(association.public_key),
            )
        return association


def decode_base58(value: str) -> bytes:
    """Decode a Solana public key without contacting a blockchain or RPC."""
    if not value or any(character not in BASE58_ALPHABET for character in value):
        raise ValueError("Invalid base58 value.")
    number = 0
    for character in value:
        number = number * 58 + BASE58_ALPHABET.index(character)
    decoded = (
        number.to_bytes((number.bit_length() + 7) // 8, "big")
        if number
        else b""
    )
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + decoded


def encode_base58(raw: bytes) -> str:
    """Encode bytes into base58 string without external RPC dependencies."""
    if not raw:
        return ""
    number = int.from_bytes(raw, "big")
    chars = []
    while number > 0:
        number, remainder = divmod(number, 58)
        chars.append(BASE58_ALPHABET[remainder])
    leading_zeroes = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading_zeroes + "".join(reversed(chars))


def short_public_key(public_key: str) -> str:
    return f"{public_key[:4]}...{public_key[-4:]}"


# Persistent wallet storage: use Supabase when configured, otherwise keep the
# in-memory fallback for local development.
_SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
_SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
if _SUPABASE_URL and _SUPABASE_SERVICE_KEY:
    from supabase_store import SupabaseClient, SupabaseWalletStore
    WALLET_STORE = SupabaseWalletStore(
        SupabaseClient(_SUPABASE_URL, _SUPABASE_SERVICE_KEY)
    )
    logger.info("⚡ Supabase wallet storage enabled.")
else:
    WALLET_STORE = WalletStore()
    logger.info("ℹ️ Supabase wallet storage disabled; using in-memory wallet storage.")


WALLET_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NFT Market Wallet Verification</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, system-ui, sans-serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center;
      background: #0b1020; color: #f8fafc; }
    main { width: min(92vw, 520px); box-sizing: border-box; padding: 32px;
      border: 1px solid #27345c; border-radius: 18px; background: #121a32;
      box-shadow: 0 20px 60px #0008; }
    h1 { margin-top: 0; font-size: 1.5rem; }
    p { line-height: 1.55; color: #cbd5e1; }
    pre { white-space: pre-wrap; padding: 16px; border-radius: 10px;
      background: #0b1020; color: #dbeafe; font-size: .9rem; }
    button { width: 100%; border: 0; border-radius: 10px; padding: 13px 16px;
      color: #fff; background: #635bff; font-weight: 700; cursor: pointer; }
    button:disabled { cursor: wait; opacity: .65; }
    #status { min-height: 24px; margin-top: 16px; font-weight: 600; }
    .warning { color: #fbbf24; font-size: .9rem; }
    .success { color: #86efac; }
    .error { color: #fca5a5; }
  </style>
</head>
<body>
  <main>
    <h1>🔐 Verify Solana Wallet</h1>
    <p>This page verifies that you control a Solana public address by asking your wallet to sign a one-time message.</p>
    <p class="warning">Never enter a seed phrase, private key, or Phantom password here. NFT Market never asks for them.</p>
    <h2>Message to sign</h2>
    <pre id="message">Loading secure message…</pre>
    <button id="verify" type="button" disabled>Connect and sign with Phantom</button>
    <div id="status" role="status" aria-live="polite"></div>
  </main>
  <script>
    const session = new URLSearchParams(window.location.search).get("session");
    const messageElement = document.getElementById("message");
    const statusElement = document.getElementById("status");
    const verifyButton = document.getElementById("verify");
    let verificationMessage = "";

    function setStatus(text, kind = "") {
      statusElement.textContent = text;
      statusElement.className = kind;
    }

    function getSolanaProvider() {
      return window.phantom?.solana || window.solana || null;
    }

    function bytesToBase64(bytes) {
      if (typeof bytes === "string") return bytes;
      const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
      let binary = "";
      for (let i = 0; i < arr.length; i++) binary += String.fromCharCode(arr[i]);
      return window.btoa(binary);
    }

    async function loadMessage() {
      if (!session) {
        setStatus("This verification link is invalid.", "error");
        return;
      }
      const response = await fetch("/api/wallet/message?session=" + encodeURIComponent(session));
      const data = await response.json();
      if (!response.ok) {
        setStatus(data.error || "This verification link is no longer valid.", "error");
        return;
      }
      verificationMessage = data.message;
      messageElement.textContent = verificationMessage;
      verifyButton.disabled = false;
      setStatus("The secure message is ready to sign.");
    }

    async function verifyWallet() {
      const provider = getSolanaProvider();
      if (!provider) {
        setStatus("Install Phantom or another Solana wallet extension, then reload this page.", "error");
        return;
      }
      verifyButton.disabled = true;
      try {
        setStatus("Connecting to your wallet…");
        const connection = await provider.connect();
        const publicKey = connection.publicKey?.toString() || provider.publicKey?.toString();
        if (!publicKey) throw new Error("The wallet did not return a public address.");
        setStatus("Wallet connected. Waiting for your signature…");
        const signed = await provider.signMessage(new TextEncoder().encode(verificationMessage), "utf8");
        const signature = signed.signature || signed;
        const response = await fetch("/api/wallet/verify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session,
            publicKey,
            signature: bytesToBase64(signature)
          })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Wallet verification failed.");
        setStatus("✅ Wallet verified successfully. You can return to Discord.", "success");
        messageElement.textContent = "Verified public address: " + data.publicKey;
      } catch (error) {
        setStatus(error.message || "Wallet verification was cancelled.", "error");
        verifyButton.disabled = false;
      }
    }

    verifyButton.addEventListener("click", verifyWallet);
    loadMessage().catch(() => setStatus("Could not load the secure verification message.", "error"));
  </script>
</body>
</html>"""


def render_wallet_page(session_token: str) -> str:
    return WALLET_PAGE_TEMPLATE


async def wallet_verify_page(request: web.Request) -> web.Response:
    session_token = request.query.get("session", "")
    return web.Response(
        text=render_wallet_page(session_token),
        content_type="text/html",
        charset="utf-8",
        headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


async def wallet_message(request: web.Request) -> web.Response:
    session_token = request.query.get("session", "")
    result = await WALLET_STORE.get_message(session_token)
    if result is None:
        return web.json_response(
            {"ok": False, "error": "This verification link is invalid or expired."},
            status=410,
        )
    message, expires_at = result
    return web.json_response(
        {
            "ok": True,
            "message": message,
            "expiresAt": expires_at.isoformat(),
        }
    )


async def wallet_verify(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response(
            {"ok": False, "error": "Invalid verification request."},
            status=400,
        )

    if not isinstance(body, dict):
        return web.json_response(
            {"ok": False, "error": "Invalid verification request."},
            status=400,
        )

    session_token = body.get("session")
    public_key = body.get("publicKey")
    signature = body.get("signature")
    if not all(
        isinstance(value, str) for value in (session_token, public_key, signature)
    ):
        return web.json_response(
            {"ok": False, "error": "Wallet address and signature are required."},
            status=400,
        )

    session = await WALLET_STORE.get_session(session_token)
    if session is not None:
        logger.info(
            "Wallet connected for Discord user=%s, wallet=%s.",
            session.discord_user_id,
            short_public_key(public_key),
        )

    verified, message, association = await WALLET_STORE.verify(
        session_token,
        public_key,
        signature,
    )
    if not verified or association is None:
        user_id = session.discord_user_id if session is not None else "unknown"
        logger.warning(
            "Wallet signature refused for Discord user=%s, wallet=%s: %s",
            user_id,
            short_public_key(public_key),
            message,
        )
        return web.json_response({"ok": False, "error": message}, status=400)

    logger.info(
        "Wallet signature validated for Discord user=%s, wallet=%s.",
        association.discord_user_id,
        short_public_key(association.public_key),
    )
    logger.info(
        "Wallet associated with Discord user=%s, wallet=%s.",
        association.discord_user_id,
        short_public_key(association.public_key),
    )
    try:
        await bot.notify_wallet_verified(association)
    except Exception as exc:
        logger.warning(
            "Could not dispatch Discord DM notification for user=%s: %s",
            association.discord_user_id,
            exc,
        )

    return web.json_response(
        {
            "ok": True,
            "message": "Wallet verified successfully.",
            "publicKey": association.public_key,
            "verifiedAt": association.verified_at.isoformat(),
        }
    )


class WalletVerifyView(discord.ui.View):
    def __init__(self, url: str) -> None:
        super().__init__(timeout=10 * 60)
        self.add_item(
            discord.ui.Button(
                label="🔐 Verify Solana Wallet",
                style=discord.ButtonStyle.link,
                url=url,
            )
        )


class WalletVerifyPromptView(discord.ui.View):
    """Prompt shown when a user tries to buy/sell without a verified wallet."""

    def __init__(self) -> None:
        super().__init__(timeout=10 * 60)

    @discord.ui.button(
        label="Verify My Wallet",
        emoji="🔐",
        style=discord.ButtonStyle.primary,
        custom_id="nftmarket:wallet:verify_prompt",
    )
    async def verify_my_wallet(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await send_wallet_verification_link(interaction)


class InMemoryStore:
    """In-memory fallback store with the same async interface as SupabaseMarketStore."""

    def __init__(self) -> None:
        self.sell_requests: list[Listing] = []
        self.buy_requests: list[Listing] = []
        self.matches: list[Match] = []

    @staticmethod
    def _compatible(seller: Listing, buyer: Listing) -> bool:
        return (
            seller.active
            and buyer.active
            and seller.guild_id == buyer.guild_id
            and seller.currency == buyer.currency
            and buyer.amount >= seller.amount
            and compatible_collection(buyer.collection, seller.collection)
        )

    def _create_match(self, seller: Listing, buyer: Listing) -> Match:
        seller.active = False
        buyer.active = False
        match = Match(
            match_id=uuid.uuid4().hex[:8],
            guild_id=seller.guild_id,
            seller=seller,
            buyer=buyer,
        )
        self.matches.append(match)
        return match

    async def add_sell_request(self, listing: Listing) -> Optional[Match]:
        self.sell_requests.append(listing)
        for buyer in self.buy_requests:
            if self._compatible(listing, buyer):
                return self._create_match(listing, buyer)
        return None

    async def add_buy_request(self, listing: Listing) -> Optional[Match]:
        self.buy_requests.append(listing)
        for seller in self.sell_requests:
            if self._compatible(seller, listing):
                return self._create_match(seller, listing)
        return None

    async def reconcile(self, guild_id: int) -> list[Match]:
        created_matches: list[Match] = []
        while True:
            pair: Optional[tuple[Listing, Listing]] = None
            for seller in self.sell_requests:
                if not seller.active or seller.guild_id != guild_id:
                    continue
                buyer = next(
                    (
                        candidate
                        for candidate in self.buy_requests
                        if candidate.guild_id == guild_id
                        and self._compatible(seller, candidate)
                    ),
                    None,
                )
                if buyer is not None:
                    pair = (seller, buyer)
                    break
            if pair is None:
                return created_matches
            created_matches.append(self._create_match(*pair))

    async def cancel_latest(self, user_id: int, request_type: str) -> list[Listing]:
        collections = {
            "buy": self.buy_requests,
            "sell": self.sell_requests,
            "both": self.buy_requests + self.sell_requests,
        }
        cancelled: list[Listing] = []
        for listing in sorted(
            collections[request_type],
            key=lambda item: item.created_at,
            reverse=True,
        ):
            if listing.user_id == user_id and listing.active:
                listing.active = False
                cancelled.append(listing)
                if request_type != "both":
                    break
        return cancelled

    async def matches_for_user(self, user_id: int) -> list[Match]:
        return [
            match
            for match in self.matches
            if match.seller.user_id == user_id or match.buyer.user_id == user_id
        ]

    async def active_counts(self, guild_id: int) -> tuple[int, int]:
        buyers = {x.user_id for x in self.buy_requests if x.guild_id == guild_id and x.active}
        sellers = {x.user_id for x in self.sell_requests if x.guild_id == guild_id and x.active}
        return len(buyers), len(sellers)

    async def active_requests_for_user(self, guild_id: int, user_id: int) -> list[Listing]:
        return sorted(
            [
                listing
                for listing in self.sell_requests + self.buy_requests
                if listing.guild_id == guild_id
                and listing.user_id == user_id
                and listing.active
            ],
            key=lambda item: item.created_at,
            reverse=True,
        )


if _SUPABASE_URL and _SUPABASE_SERVICE_KEY:
    from supabase_store import SupabaseMarketStore
    STORE = SupabaseMarketStore(SupabaseClient(_SUPABASE_URL, _SUPABASE_SERVICE_KEY))
    logger.info("⚡ Supabase market storage enabled.")
else:
    STORE = InMemoryStore()
    logger.info("ℹ️ Supabase market storage disabled; using in-memory market storage.")


class SellModal(discord.ui.Modal, title="Register an NFT to sell"):
    collection = discord.ui.TextInput(
        label="NFT collection or name",
        placeholder="Example: Solana Monkey Business",
        max_length=100,
    )
    mint_address = discord.ui.TextInput(
        label="NFT mint address (optional)",
        placeholder="Paste the NFT mint address if available",
        required=False,
        max_length=150,
    )
    price = discord.ui.TextInput(
        label="Asking price in SOL",
        placeholder="Example: 2.5 SOL",
        max_length=30,
    )
    details = discord.ui.TextInput(
        label="Traits or other details (optional)",
        placeholder="Example: Gold fur, rare background",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if isinstance(bot, NFTMarketBot):
            await bot.handle_sell(interaction, self)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await report_modal_error(interaction, error)


class BuyModal(discord.ui.Modal, title="Register an NFT to buy"):
    collection = discord.ui.TextInput(
        label="NFT collection or name",
        placeholder="Example: Solana Monkey Business",
        max_length=100,
    )
    mint_address = discord.ui.TextInput(
        label="NFT mint address (optional)",
        placeholder="Paste the desired NFT mint address if available",
        required=False,
        max_length=150,
    )
    budget = discord.ui.TextInput(
        label="Maximum budget in SOL",
        placeholder="Example: 3 SOL",
        max_length=30,
    )
    details = discord.ui.TextInput(
        label="Traits or other details (optional)",
        placeholder="Example: Prefer blue or gold traits",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if isinstance(bot, NFTMarketBot):
            await bot.handle_buy(interaction, self)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await report_modal_error(interaction, error)


async def report_modal_error(
    interaction: discord.Interaction, error: Exception
) -> None:
    logger.exception("Unhandled modal submission error", exc_info=error)
    message = "Something went wrong while saving that request. Please try again."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class NFTMarketBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=None, intents=intents, help_command=None)
        self.ready_message_sent = False
        self.wallet_web_runner: Optional[web.AppRunner] = None
        self.wallet_web_port = self._get_wallet_web_port()

    async def setup_hook(self) -> None:
        await self.start_wallet_web_server()
        self.add_view(MainMenuView())
        synced = await self.tree.sync()
        logger.info("Synced %d slash commands.", len(synced))

    @staticmethod
    def _get_wallet_web_port() -> int:
        configured_port = os.getenv("WALLET_WEB_PORT")
        # In this container environment, port 8000 is reserved by control-plane-a.
        # Fall back to 5000 if WALLET_WEB_PORT is unset, invalid, or set to 8000.
        if configured_port and configured_port.strip() != "8000":
            try:
                return int(configured_port.strip())
            except ValueError:
                logger.warning("Invalid wallet web port %r; using 5000.", configured_port)
        return 5000

    @staticmethod
    def wallet_base_url() -> Optional[str]:
        configured_url = (os.getenv("WALLET_VERIFY_BASE_URL") or os.getenv("APP_URL") or "").strip()
        if configured_url:
            cleaned = configured_url.rstrip("/")
            return cleaned if cleaned.startswith("http://") or cleaned.startswith("https://") else f"https://{cleaned}"

        domains = [
            domain.strip()
            for domain in os.getenv("REPLIT_DOMAINS", "").split(",")
            if domain.strip()
        ]
        development_domain = os.getenv("REPLIT_DEV_DOMAIN", "").strip()
        domain = domains[0] if domains else development_domain
        if not domain:
            return None
        return domain if domain.startswith("http://") or domain.startswith("https://") else f"https://{domain}"

    def wallet_url(self, session_token: str) -> Optional[str]:
        base_url = self.wallet_base_url()
        if base_url is None:
            return None
        return f"{base_url}/wallet/verify?session={quote(session_token, safe='')}"

    async def start_wallet_web_server(self) -> None:
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_get("/wallet/verify", wallet_verify_page)
        app.router.add_get("/api/wallet/message", wallet_message)
        app.router.add_post("/api/wallet/verify", wallet_verify)
        self.wallet_web_runner = web.AppRunner(app)
        await self.wallet_web_runner.setup()

        # Try designated port, fallback to port 5000 or dynamic if already bound
        ports_to_try = [self.wallet_web_port]
        if 5000 not in ports_to_try:
            ports_to_try.append(5000)

        started = False
        for port in ports_to_try:
            try:
                site = web.TCPSite(self.wallet_web_runner, "0.0.0.0", port)
                await site.start()
                self.wallet_web_port = port
                started = True
                logger.info(
                    "Wallet verification page listening on 0.0.0.0:%s.",
                    port,
                )
                break
            except OSError as err:
                logger.warning("Could not bind wallet server to port %s: %s", port, err)

        if not started:
            logger.error("Failed to bind wallet verification web server on ports %s", ports_to_try)

    async def close(self) -> None:
        if self.wallet_web_runner is not None:
            await self.wallet_web_runner.cleanup()
            self.wallet_web_runner = None
        await super().close()

    VERIFIED_ROLE_NAME = "NFT Market Verified"

    async def grant_verified_access(self, association: WalletAssociation) -> list[str]:
        """Give a verified wallet the Discord role that unlocks the private market.

        The role is created once per guild and is also granted visibility on the
        existing private-market category. The bot never grants administrator or
        moderation permissions through this role.
        """
        if not self.is_ready():
            return []

        granted: list[str] = []
        for guild in self.guilds:
            try:
                member = guild.get_member(association.discord_user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(association.discord_user_id)
                    except discord.NotFound:
                        continue

                role = discord.utils.get(guild.roles, name=self.VERIFIED_ROLE_NAME)
                if role is None:
                    if not guild.me or not guild.me.guild_permissions.manage_roles:
                        logger.warning("Cannot create verified role in guild=%s: Manage Roles missing.", guild.id)
                        continue
                    role = await guild.create_role(
                        name=self.VERIFIED_ROLE_NAME,
                        mentionable=False,
                        hoist=False,
                        reason="NFT Market verified Solana wallet access",
                    )
                    logger.info("Created verified role '%s' in guild=%s.", role.name, guild.id)

                bot_member = guild.me
                if bot_member is None or role >= bot_member.top_role:
                    logger.warning(
                        "Cannot assign verified role in guild=%s: role hierarchy is too high.", guild.id
                    )
                    continue

                if role not in member.roles:
                    await member.add_roles(role, reason="Solana wallet cryptographically verified")

                category = discord.utils.find(
                    lambda c: c.name.strip() == PRIVATE_MARKET_CATEGORY, guild.categories
                )
                if category is not None:
                    await category.set_permissions(
                        role,
                        view_channel=True,
                        reason="Verified wallet access to private NFT Market",
                    )

                granted.append(guild.name)
                logger.info(
                    "Granted verified NFT Market access to user=%s in guild=%s",
                    association.discord_user_id, guild.id
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Could not grant verified access for user=%s in guild=%s",
                    association.discord_user_id, guild.id, exc_info=True
                )

        return granted

    async def notify_wallet_verified(self, association: WalletAssociation) -> None:
        if not self.is_ready():
            return

        granted_guilds = await self.grant_verified_access(association)
        user = await self.fetch_user_safely(association.discord_user_id)
        if user is None:
            return
        try:
            access_text = (
                "\n🔓 **NFT Market access enabled.**"
                if granted_guilds
                else "\n⚠️ Wallet verified, but private network access could not be granted automatically. Please contact an administrator."
            )
            await user.send(
                "✅ Wallet verified successfully.\n"
                f"Public address: `{short_public_key(association.public_key)}`\n"
                f"Verified at: {association.verified_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
                "This confirms control of the Solana address only. It does not verify ownership of an NFT Pass."
                + access_text,
                view=MainMenuView(),
            )
        except (discord.Forbidden, discord.HTTPException, Exception):
            logger.warning(
                "Wallet verified, but the confirmation DM could not be sent to Discord user=%s.",
                association.discord_user_id,
            )

    async def handle_sell(
        self, interaction: discord.Interaction, modal: SellModal
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        amount = parse_amount(str(modal.price.value))
        collection = clean_text(str(modal.collection.value))
        if not collection:
            await interaction.followup.send(
                "Please provide an NFT collection name or address.",
                ephemeral=True,
            )
            return
        if amount is None:
            await interaction.followup.send(
                "Please enter a positive asking price in SOL.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        listing = Listing(
            listing_id=uuid.uuid4().hex[:8],
            kind="sell",
            guild_id=guild.id,
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
            collection=collection,
            mint_address=clean_text(str(modal.mint_address.value)),
            amount=amount,
            currency="SOL",
            details=clean_text(str(modal.details.value)) or "None provided",
        )
        match = await STORE.add_sell_request(listing)
        if match is None:
            await interaction.followup.send(
                f"Your sell request for **{listing.collection}** is active. "
                "I will notify you privately if a compatible buyer appears.",
                ephemeral=True,
            )
            return

        await self.complete_match(guild, match)
        await interaction.followup.send(
            self.match_summary(match, "seller"),
            ephemeral=True,
        )

    async def handle_buy(
        self, interaction: discord.Interaction, modal: BuyModal
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        amount = parse_amount(str(modal.budget.value))
        collection = clean_text(str(modal.collection.value))
        if not collection:
            await interaction.followup.send(
                "Please provide an NFT collection name or address.",
                ephemeral=True,
            )
            return
        if amount is None:
            await interaction.followup.send(
                "Please enter a positive maximum budget in SOL.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        listing = Listing(
            listing_id=uuid.uuid4().hex[:8],
            kind="buy",
            guild_id=guild.id,
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
            collection=collection,
            mint_address=clean_text(str(modal.mint_address.value)),
            amount=amount,
            currency="SOL",
            details=clean_text(str(modal.details.value)) or "None provided",
        )
        match = await STORE.add_buy_request(listing)
        if match is None:
            await interaction.followup.send(
                f"Your buy request for **{listing.collection}** is active. "
                "I will notify you privately if a compatible seller appears.",
                ephemeral=True,
            )
            return

        await self.complete_match(guild, match)
        await interaction.followup.send(
            self.match_summary(match, "buyer"),
            ephemeral=True,
        )

    @staticmethod
    def match_summary(match: Match, role: str) -> str:
        other_person = (
            match.buyer.user_name if role == "seller" else match.seller.user_name
        )
        channel_text = (
            f" Your private deal channel is {match.channel_name}."
            if match.channel_name
            else " I could not create the private channel, so please check the bot's permissions."
        )
        return (
            f"Match found with **{other_person}** for **{match.seller.collection}**. "
            f"Seller price: **{format_amount(match.seller.amount, match.seller.currency)}**."
            f"{channel_text}"
        )

    async def complete_match(self, guild: discord.Guild, match: Match) -> None:
        channel: Optional[discord.TextChannel] = None
        try:
            channel = await self.create_private_deal_channel(guild, match)
            match.channel_id = channel.id
            match.channel_name = channel.mention
            await self.persist_match_state(match)
            asyncio.create_task(self.delete_deal_channel_later(channel, match))
        except PrivateDealSetupError as error:
            logger.error(
                "Private deal channel setup failed for guild=%s, match=%s: %s",
                guild.id,
                match.match_id,
                error,
            )
        except discord.Forbidden as error:
            logger.error(
                "Discord refused private deal channel creation: guild=%s, match=%s, "
                "status=%s, error_code=%s, message=%s. Check Manage Channels and "
                "Manage Roles for the bot.",
                guild.id,
                match.match_id,
                error.status,
                getattr(error, "code", "unknown"),
                getattr(error, "text", str(error)),
                exc_info=True,
            )
        except discord.NotFound as error:
            logger.error(
                "Discord could not find the guild/category while creating a private "
                "deal channel: guild=%s, match=%s, status=%s, message=%s",
                guild.id,
                match.match_id,
                error.status,
                getattr(error, "text", str(error)),
                exc_info=True,
            )
        except discord.HTTPException as error:
            logger.error(
                "Discord rejected private deal channel creation: guild=%s, match=%s, "
                "status=%s, error_code=%s, message=%s",
                guild.id,
                match.match_id,
                error.status,
                getattr(error, "code", "unknown"),
                getattr(error, "text", str(error)),
                exc_info=True,
            )

        embed = self.match_embed(match)
        buyer = await self.fetch_user_safely(match.buyer.user_id)
        seller = await self.fetch_user_safely(match.seller.user_id)
        for user in (buyer, seller):
            if user is None:
                continue
            try:
                await user.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                logger.info("Could not DM user %s about match %s.", user.id, match.match_id)

        if channel is not None:
            try:
                await channel.send(
                    content=f"<@{match.buyer.user_id}> <@{match.seller.user_id}>",
                    embed=embed,
                    view=DealActionView(match.match_id),
                )
            except discord.Forbidden as error:
                logger.error(
                    "Private deal channel exists but the bot cannot post in it: "
                    "channel=%s, match=%s, status=%s, error_code=%s, message=%s",
                    channel.id,
                    match.match_id,
                    error.status,
                    getattr(error, "code", "unknown"),
                    getattr(error, "text", str(error)),
                    exc_info=True,
                )
            except discord.HTTPException as error:
                logger.error(
                    "Private deal channel exists but Discord rejected the first "
                    "message: channel=%s, match=%s, status=%s, error_code=%s, message=%s",
                    channel.id,
                    match.match_id,
                    error.status,
                    getattr(error, "code", "unknown"),
                    getattr(error, "text", str(error)),
                    exc_info=True,
                )

    async def create_private_deal_channel(
        self, guild: discord.Guild, match: Match
    ) -> discord.TextChannel:
        bot_member = guild.me
        if bot_member is None:
            raise PrivateDealSetupError(
                "The bot member is not available in Discord's guild cache."
            )

        permissions = bot_member.guild_permissions
        required_permissions = {
            "View Channel": permissions.view_channel,
            "Send Messages": permissions.send_messages,
            "Embed Links": permissions.embed_links,
            "Read Message History": permissions.read_message_history,
            "Manage Channels": permissions.manage_channels,
            "Manage Roles": permissions.manage_roles,
        }
        missing_permissions = [
            name for name, granted in required_permissions.items() if not granted
        ]
        if missing_permissions:
            raise PrivateDealSetupError(
                "Missing bot permissions: "
                + ", ".join(missing_permissions)
                + ". Grant these permissions to the bot role."
            )

        category = next(
            (
                candidate
                for candidate in guild.categories
                if candidate.name.strip() == PRIVATE_MARKET_CATEGORY
            ),
            None,
        )

        if category is None:
            logger.info(
                "Creating private market category '%s' in guild=%s.",
                PRIVATE_MARKET_CATEGORY,
                guild.id,
            )
            verified_role = discord.utils.get(guild.roles, name=self.VERIFIED_ROLE_NAME)
            category_overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False)
            }
            if verified_role is not None:
                category_overwrites[verified_role] = discord.PermissionOverwrite(view_channel=True)
            category = await guild.create_category(
                PRIVATE_MARKET_CATEGORY,
                overwrites=category_overwrites,
                reason="NFT Market Bot private deal category",
            )
        else:
            logger.info(
                "Using existing private market category '%s' (id=%s) in guild=%s.",
                category.name,
                category.id,
                guild.id,
            )

        buyer = await guild.fetch_member(match.buyer.user_id)
        seller = await guild.fetch_member(match.seller.user_id)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            buyer: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
            seller: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
        }
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            manage_channels=True,
        )

        channel_name = f"deal-{match.match_id}"
        channel = await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites,
            topic="Temporary private NFT buyer/seller deal channel.",
            reason="NFT Market Bot matched buyer and seller",
        )
        logger.info(
            "Created private deal channel '%s' (id=%s) under category '%s' in guild=%s. "
            "Allowed members: seller=%s, buyer=%s, bot=%s.",
            channel.name,
            channel.id,
            category.name,
            guild.id,
            seller.id,
            buyer.id,
            bot_member.id,
        )
        return channel

    async def persist_match_state(self, match: Match) -> None:
        client = getattr(STORE, "client", None)
        if client is None:
            return
        data = {
            "channel_id": match.channel_id,
            "channel_name": match.channel_name,
            "status": getattr(match, "status", "MATCHED"),
            "closed_at": match.closed_at.isoformat() if match.closed_at else None,
        }
        try:
            await client.patch(
                "market_matches", data, params={"match_id": f"eq.{match.match_id}"}
            )
        except Exception:
            logger.exception("Could not persist lifecycle state for match=%s.", match.match_id)

    async def change_deal_status(self, interaction: discord.Interaction, match_id: str, status: str) -> None:
        matches = await STORE.matches_for_user(interaction.user.id)
        match = next((m for m in matches if m.match_id == match_id), None)
        if match is None or interaction.user.id not in (match.buyer.user_id, match.seller.user_id):
            await interaction.response.send_message("This match is not available to you.", ephemeral=True)
            return
        if getattr(match, "status", "MATCHED") != "MATCHED" or match.closed_at is not None:
            await interaction.response.send_message("This deal is already closed.", ephemeral=True)
            return
        if status not in {"COMPLETED", "CANCELLED"}:
            await interaction.response.send_message("Invalid deal status.", ephemeral=True)
            return
        match.status = status
        match.closed_at = utc_now()
        await self.persist_match_state(match)
        await interaction.response.send_message(f"Deal marked as **{status}**.", ephemeral=True)
        for user_id in (match.buyer.user_id, match.seller.user_id):
            user = await self.fetch_user_safely(user_id)
            if user is not None:
                try:
                    await user.send(f"NFT Market deal **{match.match_id}** is now **{status}**.")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(f"🔒 Deal closed: **{status}**.")
                await asyncio.sleep(2)
                await channel.delete(reason=f"NFT Market deal {status.lower()}")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    async def delete_deal_channel_later(
        self, channel: discord.TextChannel, match: Match
    ) -> None:
        await asyncio.sleep(DEAL_CHANNEL_TTL_HOURS * 60 * 60)
        try:
            match.status = "EXPIRED"
            match.closed_at = utc_now()
            await self.persist_match_state(match)
            await channel.delete(reason="NFT Market Bot temporary deal expired")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.info("Deal channel %s was already closed or could not be deleted.", channel.id)

    async def fetch_user_safely(self, user_id: int) -> Optional[discord.User]:
        if not self.is_ready():
            return None
        try:
            return await self.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException, Exception):
            logger.info("Could not fetch Discord user %s.", user_id)
            return None

    @staticmethod
    def match_embed(match: Match) -> discord.Embed:
        embed = discord.Embed(
            title="NFT Market match found",
            description=(
                f"A buyer and seller matched for **{match.seller.collection}**. "
                "Keep the conversation inside the private deal channel."
            ),
            color=discord.Color.green(),
            timestamp=match.created_at,
        )
        embed.add_field(
            name="Seller price",
            value=format_amount(match.seller.amount, match.seller.currency),
            inline=True,
        )
        embed.add_field(name="Seller details", value=match.seller.details, inline=False)
        embed.add_field(
            name="NFT mint address",
            value=match.seller.mint_address or "No mint address provided",
            inline=False,
        )
        embed.add_field(name="Match ID", value=match.match_id, inline=True)
        return embed


class DealActionView(discord.ui.View):
    def __init__(self, match_id: str) -> None:
        super().__init__(timeout=None)
        self.match_id = match_id

    @discord.ui.button(label="Complete Deal", emoji="✅", style=discord.ButtonStyle.success, custom_id="nftmarket:deal:complete")
    async def complete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        bot = interaction.client
        if isinstance(bot, NFTMarketBot):
            await bot.change_deal_status(interaction, self.match_id, "COMPLETED")

    @discord.ui.button(label="Cancel Deal", emoji="❌", style=discord.ButtonStyle.danger, custom_id="nftmarket:deal:cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        bot = interaction.client
        if isinstance(bot, NFTMarketBot):
            await bot.change_deal_status(interaction, self.match_id, "CANCELLED")


bot = NFTMarketBot()


async def require_verified_wallet(interaction: discord.Interaction) -> bool:
    association = await WALLET_STORE.get_association(interaction.user.id)
    if association is not None:
        return True

    message = (
        "🔐 **Wallet verification required.**\n"
        "You must verify your Solana wallet before using the private NFT Market.\n\n"
        "Click **Verify My Wallet** below to connect your wallet and sign the verification message."
    )

    view = WalletVerifyPromptView()

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            view=view,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            message,
            view=view,
            ephemeral=True,
        )

    return False


@bot.tree.command(name="sell", description="Register an NFT you want to sell.")
@app_commands.guild_only()
async def sell(interaction: discord.Interaction) -> None:
    if not await require_verified_wallet(interaction):
        return
    await interaction.response.send_modal(SellModal())


@bot.tree.command(name="buy", description="Register an NFT you want to buy.")
@app_commands.guild_only()
async def buy(interaction: discord.Interaction) -> None:
    if not await require_verified_wallet(interaction):
        return
    await interaction.response.send_modal(BuyModal())


@bot.tree.command(name="matches", description="Show your current private matches.")
@app_commands.guild_only()
async def matches(interaction: discord.Interaction) -> None:
    if not await require_verified_wallet(interaction):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    new_matches = await STORE.reconcile(guild.id)
    for match in new_matches:
        await bot.complete_match(guild, match)

    user_matches = await STORE.matches_for_user(interaction.user.id)
    if not user_matches:
        await interaction.followup.send(
            "You do not have any matches yet.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="Your NFT Market matches",
        description="Only you can see this response.",
        color=discord.Color.blurple(),
    )
    for match in user_matches[-10:]:
        other_person = (
            match.seller.user_name
            if match.buyer.user_id == interaction.user.id
            else match.buyer.user_name
        )
        channel_text = match.channel_name or f"deal-{match.match_id}"
        embed.add_field(
            name=f"{match.seller.collection} · {match.match_id}",
            value=(
                f"Other party: **{other_person}**\n"
                f"Seller price: **{format_amount(match.seller.amount, match.seller.currency)}**\n"
                f"Status: **{getattr(match, 'status', 'MATCHED')}**\n"
                f"Deal channel: {channel_text}"
            ),
            inline=False,
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="cancel", description="Cancel an active buy or sell request.")
@app_commands.guild_only()
@app_commands.describe(request_type="Which active request should be cancelled?")
@app_commands.choices(
    request_type=[
        app_commands.Choice(name="Buy request", value="buy"),
        app_commands.Choice(name="Sell request", value="sell"),
        app_commands.Choice(name="Both", value="both"),
    ]
)
async def cancel(
    interaction: discord.Interaction, request_type: app_commands.Choice[str]
) -> None:
    if not await require_verified_wallet(interaction):
        return
    cancelled = await STORE.cancel_latest(interaction.user.id, request_type.value)
    if not cancelled:
        await interaction.response.send_message(
            f"You have no active {request_type.name.lower()} to cancel.",
            ephemeral=True,
        )
        return

    names = ", ".join(f"{item.kind} for {item.collection}" for item in cancelled)
    await interaction.response.send_message(
        f"Cancelled: **{names}**.",
        ephemeral=True,
    )


@bot.tree.interaction_check
async def on_tree_interaction_check(interaction: discord.Interaction) -> bool:
    cmd = interaction.command.name if interaction.command else str(interaction.data)
    user_str = f"{interaction.user} (ID: {interaction.user.id})"
    channel_str = f"#{interaction.channel} (ID: {interaction.channel_id})" if interaction.channel else f"channel {interaction.channel_id}"
    guild_str = f"{interaction.guild.name} (ID: {interaction.guild.id})" if interaction.guild else "Direct Message"
    logger.info("⚡ Slash command /%s invoked by %s in %s on guild %s", cmd, user_str, channel_str, guild_str)
    return True


@bot.tree.command(name="wallet", description="Verify ownership of your Solana wallet via cryptographic signature.")
async def wallet(interaction: discord.Interaction) -> None:
    """Send a secure one-time verification link with button."""
    await send_wallet_verification_link(interaction)


async def send_wallet_verification_link(interaction: discord.Interaction) -> None:
    """Create a wallet verification session and send the secure link.

    Shared by the /wallet slash command and the 🔐 Verify Wallet button.
    """
    # Defer immediately: Discord requires an ack within 3s, and creating the
    # Supabase session below is a network call that can exceed that window.
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    try:
        session = await WALLET_STORE.create_session(interaction.user.id)
        url = bot.wallet_url(session.token)
    except Exception:
        logger.exception(
            "Wallet session creation failed for user=%s", interaction.user.id
        )
        await interaction.followup.send(
            "Could not start wallet verification right now. Please try again.",
            ephemeral=True,
        )
        return

    if not url:
        await interaction.followup.send(
            "Wallet verification is not configured on the bot server yet. Please contact an administrator.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        "🔐 **Verify Solana Wallet**\n"
        "Click the button below to connect your Solana wallet (Phantom, etc.) "
        "and sign a cryptographic proof of ownership.\n\n"
        "Never enter your seed phrase, private key, or wallet password.\n"
        "Link expires in 5 minutes.",
        view=WalletVerifyView(url),
        ephemeral=True,
    )


@bot.tree.command(name="wallet-status", description="Show your verified Solana wallet status.")
async def wallet_status(interaction: discord.Interaction) -> None:
    association = await WALLET_STORE.get_association(interaction.user.id)
    if association is None:
        await interaction.response.send_message(
            "No Solana wallet is currently verified for your Discord account.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "✅ **Wallet verified**\n"
        f"Public address: `{short_public_key(association.public_key)}`\n"
        f"Verified at: {association.verified_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        "This confirms control of the Solana address only. It does not verify ownership of an NFT Pass.",
        ephemeral=True,
    )


@bot.tree.command(name="wallet-remove", description="Remove your verified Solana wallet association.")
async def wallet_remove(interaction: discord.Interaction) -> None:
    association = await WALLET_STORE.remove_association(interaction.user.id)
    if association is None:
        await interaction.response.send_message(
            "No verified wallet is associated with your Discord account.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Wallet association removed. You must verify a wallet again before using the private NFT Market.",
        ephemeral=True,
    )


@bot.event
async def on_ready() -> None:
    logger.info("Connected to Discord as %s (ID: %s).", bot.user, bot.user.id if bot.user else "unknown")
    logger.info("NFT Market Bot is ready.")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not configured.")
    bot.run(token)
