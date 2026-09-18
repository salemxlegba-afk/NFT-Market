"""Patch the paid-access embed with a copyable Solana Pay URI."""
import discord

import payment_extension
from payments import PAYMENT_WALLET, build_solana_pay_uri


def access_embed_with_uri(plan_id, sol_amount, reference):
    plan = payment_extension.ACCESS_PLANS[plan_id]
    uri = build_solana_pay_uri(
        recipient=PAYMENT_WALLET,
        sol_amount=sol_amount,
        reference=reference,
        label="NFT Market Access",
        message=str(plan["label"]),
    )
    embed = payment_extension.discord.Embed(
        title=f"💳 {plan['label']}",
        description=(
            f"**Price:** USD {plan['usd']}\n"
            f"**Send:** {sol_amount:.6f} SOL\n\n"
            "Copy the Solana Pay link below and open it with Phantom/Solflare.\n"
            "After sending, press **Check Payment**.\n\n"
            "⚠️ Paid access gives you time to search for a match; "
            "it does **not** guarantee a sale or match."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Payment wallet", value=PAYMENT_WALLET, inline=False)
    embed.add_field(name="Solana Pay", value=uri, inline=False)
    embed.add_field(name="Duration", value=f"{plan['hours']} hours", inline=True)
    embed.set_footer(text=f"Reference: {reference}")
    return embed


payment_extension.access_embed = access_embed_with_uri
