"""Persistent paid-access storage for NFT Market."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


class AccessStore:
    def __init__(self, client: Any):
        self.client = client

    async def grant(self, discord_user_id: int, plan_id: str, hours: int, tx_signature: str, amount_sol: str) -> None:
        await self.client.post("paid_access", {
            "discord_user_id": discord_user_id,
            "plan_id": plan_id,
            "hours": hours,
            "tx_signature": tx_signature,
            "amount_sol": amount_sol,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        })

    async def active(self, discord_user_id: int) -> Optional[dict[str, Any]]:
        rows = await self.client.get(
            "paid_access",
            params={"discord_user_id": f"eq.{discord_user_id}", "order": "expires_at.desc", "limit": "1"},
        )
        if not rows:
            return None
        row = rows[0]
        expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if expires <= datetime.now(timezone.utc):
            return None
        return row
