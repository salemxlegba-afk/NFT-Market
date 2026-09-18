"""Direct Solana payment helpers for NFT Market paid access.

Payments go directly to the merchant's Solana wallet. This module only
constructs payment intents/URIs; access must not be granted until the
transaction is independently verified on-chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from urllib.parse import quote


PAYMENT_WALLET = "4d6ysJay9pUvn6ti6wMShzMCzPkkKCQVXnkNpbHmUdaF"

# Display prices remain in USD. The actual SOL amount is supplied at checkout
# after a live SOL/USD quote is obtained.
ACCESS_PLANS: dict[str, dict[str, object]] = {
    "24h": {"label": "24H Access", "usd": Decimal("8"), "hours": 24},
    "48h": {"label": "48H Access", "usd": Decimal("15"), "hours": 48},
}


@dataclass(frozen=True)
class PaymentIntent:
    plan_id: str
    reference: str
    usd_amount: Decimal
    sol_amount: Decimal
    expires_at: str

    @property
    def lamports(self) -> int:
        return int(
            (self.sol_amount * Decimal("1000000000"))
            .to_integral_value(rounding=ROUND_DOWN)
        )


def get_plan(plan_id: str) -> dict[str, object]:
    try:
        return ACCESS_PLANS[plan_id]
    except KeyError as exc:
        raise ValueError("Unknown access plan.") from exc


def build_solana_pay_uri(
    *,
    recipient: str,
    sol_amount: Decimal,
    reference: str,
    label: str = "NFT Market Access",
    message: str = "NFT Market paid access",
) -> str:
    """Build a Solana Pay transfer URI for a direct SOL payment."""
    if sol_amount <= 0:
        raise ValueError("SOL amount must be positive.")
    if not reference:
        raise ValueError("A unique payment reference is required.")

    return (
        f"solana:{recipient}"
        f"?amount={quote(format(sol_amount, 'f'))}"
        f"&reference={quote(reference)}"
        f"&label={quote(label)}"
        f"&message={quote(message)}"
    )
