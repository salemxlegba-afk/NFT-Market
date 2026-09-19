"""Direct SOL paid-access integration for NFT Market."""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta, timezone, datetime
from decimal import Decimal, ROUND_UP

import aiohttp
import discord
from nacl.signing import SigningKey

from payments import ACCESS_PLANS, PAYMENT_WALLET

RPC_URL = "https://api.mainnet-beta.solana.com"
PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
LAMPORTS_PER_SOL = Decimal("1000000000")
TEST_PAYMENT_MODE = os.getenv("NFTMARKET_TEST_PAYMENT", "").strip().lower() in {"1", "true", "yes", "on"}


def encode_base58(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(raw, "big")
    out = []
    while n:
        n, rem = divmod(n, 58)
        out.append(alphabet[rem])
    zeros = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * zeros + "".join(reversed(out))


def new_reference() -> str:
    return encode_base58(SigningKey.generate().verify_key.encode())


async def sol_usd() -> Decimal:
    """Get a live SOL/USD quote with a public fallback source."""
    urls = (
        PRICE_URL,
        "https://api.coinbase.com/v2/prices/SOL-USD/spot",
    )
    last_error: Exception | None = None
    async with aiohttp.ClientSession() as session:
        for url in urls:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"quote HTTP {resp.status}")
                    data = await resp.json()
                    if "coingecko" in url:
                        price = Decimal(str(data["solana"]["usd"]))
                    else:
                        price = Decimal(str(data["data"]["amount"]))
                    if price <= 0:
                        raise RuntimeError("invalid quote")
                    return price
            except Exception as exc:
                last_error = exc
    raise RuntimeError("SOL/USD quote unavailable") from last_error


async def rpc(method: str, params: list):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Solana RPC HTTP {resp.status}")
            data = await resp.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data.get("result")


async def find_payment(
    reference: str,
    expected_lamports: int,
    created_at: datetime | None = None,
) -> str | None:
    """Find an exact SOL payment.

    We first use the Solana Pay reference when present. We also scan the
    merchant wallet as a fallback because a user can manually send SOL to the
    displayed wallet without using a Solana Pay URI.
    """
    candidate_rows = await rpc("getSignaturesForAddress", [
        reference, {"commitment": "finalized", "limit": 20}
    ])

    # Manual wallet transfers do not contain our reference. Scan the merchant
    # wallet as a fallback so a normal Phantom "Send SOL" payment still works.
    wallet_rows = await rpc("getSignaturesForAddress", [
        PAYMENT_WALLET, {"commitment": "finalized", "limit": 50}
    ])

    seen: set[str] = set()
    rows = []
    for row in (candidate_rows or []) + (wallet_rows or []):
        signature = row.get("signature")
        if not signature or signature in seen or row.get("err") is not None:
            continue
        seen.add(signature)

        # Do not accept an old payment from before this checkout.
        block_time = row.get("blockTime")
        if created_at is not None and block_time is not None:
            if block_time < int(created_at.timestamp()) - 120:
                continue
        rows.append(row)

    for row in rows:
        signature = row.get("signature")
        tx = await rpc("getTransaction", [
            signature,
            {
                "commitment": "finalized",
                "maxSupportedTransactionVersion": 0,
                "encoding": "jsonParsed",
            },
        ])
        if not tx or not tx.get("meta"):
            continue

        message = (tx.get("transaction") or {}).get("message") or {}
        keys = message.get("accountKeys") or []
        key_strings = [
            item.get("pubkey") if isinstance(item, dict) else item
            for item in keys
        ]

        # If a Solana Pay reference was used, require it. For manual sends,
        # the reference is absent and the exact amount + recipient is enough.
        if reference not in key_strings and PAYMENT_WALLET not in key_strings:
            continue

        # Phantom/manual SOL sends can appear either as top-level parsed
        # instructions or as inner instructions. Inspect both locations.
        instructions = list(message.get("instructions") or [])
        meta = tx.get("meta") or {}
        for group in meta.get("innerInstructions") or []:
            instructions.extend(group.get("instructions") or [])

        for instruction in instructions:
            parsed = instruction.get("parsed") if isinstance(instruction, dict) else None
            if not isinstance(parsed, dict) or parsed.get("type") != "transfer":
                continue
            info = parsed.get("info") or {}
            destination = info.get("destination")
            lamports_raw = info.get("lamports")
            try:
                lamports = int(lamports_raw)
            except (TypeError, ValueError):
                continue
            if destination == PAYMENT_WALLET and lamports == expected_lamports:
                return signature

    return None


async def grant_access(client, user_id: int, plan_id: str, signature: str, sol_amount: Decimal) -> None:
    hours = int(ACCESS_PLANS[plan_id]["hours"])
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=hours)
    await client.post("paid_access", {
        "discord_user_id": user_id,
        "plan_id": plan_id,
        "hours": hours,
        "tx_signature": signature,
        "amount_sol": str(sol_amount),
        "granted_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    })


def access_embed(plan_id: str, sol_amount: Decimal, reference: str) -> discord.Embed:
    plan = ACCESS_PLANS[plan_id]
    usd_label = str(plan["usd"])
    hours = int(plan["hours"])
    mode_label = "\n\n🧪 **TEST MODE:** payment verification is simulated; no SOL is required." if TEST_PAYMENT_MODE else ""
    embed = discord.Embed(
        title=f"💳 {plan['label']}",
        description=(
            f"**Price:** USD {usd_label}\n"
            f"**Send:** {sol_amount:.6f} SOL\n\n"
            "Send the exact amount to the payment wallet below.\n"
            "Then press **Check Payment**.\n\n"
            "⚠️ Paid access gives you time to search for a match; "
            "it does **not** guarantee a sale or match."
            f"{mode_label}"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Payment wallet", value=PAYMENT_WALLET, inline=False)
    embed.add_field(name="Duration", value=f"{hours} hours", inline=True)
    embed.set_footer(text=f"Reference: {reference}")
    return embed


class PaymentCheckView(discord.ui.View):
    def __init__(self, plan_id: str, sol_amount: Decimal, reference: str) -> None:
        super().__init__(timeout=1800)
        self.plan_id = plan_id
        self.sol_amount = sol_amount
        self.reference = reference
        self.created_at = datetime.now(timezone.utc) - timedelta(minutes=15)

    @discord.ui.button(label="🔎 Check Payment", style=discord.ButtonStyle.success)
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            lamports = int(
                (self.sol_amount * LAMPORTS_PER_SOL).to_integral_value(
                    rounding=ROUND_UP
                )
            )
            if TEST_PAYMENT_MODE:
                signature = f"TEST-{interaction.id}"
            else:
                signature = await find_payment(self.reference, lamports, self.created_at)
                if signature is None:
                    await interaction.followup.send(
                        "⏳ Payment not detected yet. Make sure the exact SOL amount was sent and wait for final confirmation.",
                        ephemeral=True,
                    )
                    return
            client = getattr(interaction.client, "paid_access_client", None)
            if client is None:
                await interaction.followup.send("⚠️ Payment storage is not configured.", ephemeral=True)
                return
            existing = await client.get("paid_access", params={"tx_signature": f"eq.{signature}"})
            if existing:
                await interaction.followup.send("ℹ️ This transaction has already been used.", ephemeral=True)
                return
            await grant_access(client, interaction.user.id, self.plan_id, signature, self.sol_amount)
            hours = int(ACCESS_PLANS[self.plan_id]["hours"])
            await interaction.followup.send(
                f"✅ **Payment confirmed.** Your **{hours}H access** is active.",
                ephemeral=True,
            )
            button.disabled = True
            await interaction.message.edit(view=self)
        except Exception:
            await interaction.followup.send(
                "⚠️ Payment verification failed temporarily. Please try again.",
                ephemeral=True,
            )


class AccessPlanView(discord.ui.View):
    """Persistent access-plan selector with a guarded checkout handler."""
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def choose(self, interaction: discord.Interaction, plan_id: str) -> None:
        await handle_access_plan_interaction(interaction, plan_id)

    @discord.ui.button(label="24H — $8", style=discord.ButtonStyle.primary, custom_id="nftmarket:access:24h")
    async def plan_24(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.choose(interaction, "24h")

    @discord.ui.button(label="48H — $15", style=discord.ButtonStyle.primary, custom_id="nftmarket:access:48h")
    async def plan_48(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.choose(interaction, "48h")


_access_interaction_lock = asyncio.Lock()
_access_interactions_handled: set[int] = set()


async def handle_access_plan_interaction(interaction: discord.Interaction, plan_id: str) -> None:
    """Handle checkout once, even if Discord delivers the component through two paths."""
    async with _access_interaction_lock:
        if interaction.id in _access_interactions_handled:
            return
        _access_interactions_handled.add(interaction.id)
        if len(_access_interactions_handled) > 2000:
            _access_interactions_handled.clear()
        if interaction.response.is_done():
            return
        await interaction.response.defer(ephemeral=True)

    try:
        usd = Decimal(str(ACCESS_PLANS[plan_id]["usd"]))
        quote_usd = await sol_usd()
        sol_amount = (usd / quote_usd).quantize(Decimal("0.000001"), rounding=ROUND_UP)
        reference = new_reference()
        await interaction.followup.send(
            embed=access_embed(plan_id, sol_amount, reference),
            view=PaymentCheckView(plan_id, sol_amount, reference),
            ephemeral=True,
        )
    except Exception:
        await interaction.followup.send(
            "⚠️ I could not calculate the current SOL amount. Please try again shortly.",
            ephemeral=True,
        )


async def access_callback(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
    await interaction.response.send_message(
        embed=discord.Embed(
            title="💳 Choose Your Access",
            description=(
                "**⚡ 24H Access — $8**\n"
                "Search for potential matches for 24 hours.\n\n"
                "**🔥 48H Access — $15**\n"
                "Search for potential matches for 48 hours.\n\n"
                "⚠️ Access does not guarantee a sale or match."
            ),
            color=discord.Color.blurple(),
        ),
        view=AccessPlanView(),
        ephemeral=True,
    )

