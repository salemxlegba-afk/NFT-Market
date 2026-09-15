"""Supabase storage adapter for NFT Market Bot.

Implements the exact same interface as InMemoryStore and WalletStore in main.py,
using Supabase PostgreSQL via direct PostgREST calls with aiohttp.
Falls back seamlessly to in-memory storage if Supabase credentials are not provided.
"""
from __future__ import annotations

import logging
import base64
import binascii
import os
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("nft-market-bot.supabase")


class SupabaseClient:
    """Lightweight asynchronous client for Supabase REST API (PostgREST)."""

    def __init__(self, url: str, service_key: str):
        self.url = url.rstrip("/")
        self.service_key = service_key
        self.rest_url = f"{self.url}/rest/v1"

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    async def get(self, table: str, params: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.rest_url}/{table}",
                headers=self.headers,
                params=params or {},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"Supabase GET {table} failed ({resp.status}): {text}")
                return await resp.json()

    async def post(self, table: str, data: Dict[str, Any], on_conflict: Optional[str] = None) -> List[Dict[str, Any]]:
        headers = dict(self.headers)
        params = {}
        if on_conflict:
            headers["Prefer"] = f"resolution=merge-duplicates,return=representation"
            params["on_conflict"] = on_conflict

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.rest_url}/{table}",
                headers=headers,
                params=params,
                json=data,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"Supabase POST {table} failed ({resp.status}): {text}")
                return await resp.json()

    async def patch(self, table: str, data: Dict[str, Any], params: Dict[str, str]) -> List[Dict[str, Any]]:
        async with aiohttp.ClientSession() as session:
            async with session.patch(
                f"{self.rest_url}/{table}",
                headers=self.headers,
                params=params,
                json=data,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"Supabase PATCH {table} failed ({resp.status}): {text}")
                return await resp.json()

    async def delete(self, table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{self.rest_url}/{table}",
                headers=self.headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"Supabase DELETE {table} failed ({resp.status}): {text}")
                return await resp.json()


class SupabaseMarketStore:
    """Supabase-backed Market Store for listings and matches."""

    def __init__(self, client: SupabaseClient):
        self.client = client

    async def add_listing(self, listing: Any) -> None:
        payload = {
            "listing_id": listing.listing_id,
            "kind": listing.kind,
            "guild_id": listing.guild_id,
            "user_id": listing.user_id,
            "user_name": listing.user_name,
            "collection": listing.collection,
            "currency": listing.currency,
            "amount": float(listing.amount),
            "details": listing.details,
            "mint_address": listing.mint_address,
            "active": listing.active,
            "created_at": listing.created_at.isoformat(),
        }
        await self.client.post("market_listings", payload)

    async def get_active_listings(self, guild_id: int) -> List[Any]:
        from main import Listing
        records = await self.client.get(
            "market_listings",
            params={"guild_id": f"eq.{guild_id}", "active": "eq.true", "order": "created_at.desc"},
        )
        return [
            Listing(
                listing_id=r["listing_id"],
                kind=r["kind"],
                guild_id=r["guild_id"],
                user_id=r["user_id"],
                user_name=r["user_name"],
                collection=r["collection"],
                currency=r["currency"],
                amount=Decimal(str(r["amount"])),
                details=r["details"],
                mint_address=r.get("mint_address"),
                active=r["active"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in records
        ]

    async def get_user_listings(self, guild_id: int, user_id: int) -> List[Any]:
        from main import Listing
        records = await self.client.get(
            "market_listings",
            params={
                "guild_id": f"eq.{guild_id}",
                "user_id": f"eq.{user_id}",
                "active": "eq.true",
                "order": "created_at.desc",
            },
        )
        return [
            Listing(
                listing_id=r["listing_id"],
                kind=r["kind"],
                guild_id=r["guild_id"],
                user_id=r["user_id"],
                user_name=r["user_name"],
                collection=r["collection"],
                currency=r["currency"],
                amount=Decimal(str(r["amount"])),
                details=r["details"],
                mint_address=r.get("mint_address"),
                active=r["active"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in records
        ]

    async def deactivate_listing(self, listing_id: str) -> None:
        await self.client.patch(
            "market_listings",
            {"active": False},
            params={"listing_id": f"eq.{listing_id}"},
        )

    async def add_match(self, match: Any) -> None:
        payload = {
            "match_id": match.match_id,
            "guild_id": match.guild_id,
            "seller_listing_id": match.seller_listing_id,
            "buyer_listing_id": match.buyer_listing_id,
            "channel_id": match.channel_id,
            "channel_name": match.channel_name,
            "created_at": match.created_at.isoformat(),
            "closed_at": match.closed_at.isoformat() if match.closed_at else None,
        }
        await self.client.post("market_matches", payload)

    async def get_active_matches(self, guild_id: int) -> List[Any]:
        from main import Match, Listing
        match_records = await self.client.get(
            "market_matches",
            params={"guild_id": f"eq.{guild_id}", "closed_at": "is.null", "order": "created_at.desc"},
        )
        matches: List[Any] = []
        for mr in match_records:
            s_res = await self.client.get("market_listings", params={"listing_id": f"eq.{mr['seller_listing_id']}"})
            b_res = await self.client.get("market_listings", params={"listing_id": f"eq.{mr['buyer_listing_id']}"})
            if not s_res or not b_res:
                continue
            sr = s_res[0]
            br = b_res[0]
            seller = Listing(
                listing_id=sr["listing_id"], kind=sr["kind"], guild_id=sr["guild_id"],
                user_id=sr["user_id"], user_name=sr["user_name"], collection=sr["collection"],
                currency=sr["currency"], amount=Decimal(str(sr["amount"])), details=sr["details"],
                mint_address=sr.get("mint_address"), active=sr["active"],
                created_at=datetime.fromisoformat(sr["created_at"]),
            )
            buyer = Listing(
                listing_id=br["listing_id"], kind=br["kind"], guild_id=br["guild_id"],
                user_id=br["user_id"], user_name=br["user_name"], collection=br["collection"],
                currency=br["currency"], amount=Decimal(str(br["amount"])), details=br["details"],
                mint_address=br.get("mint_address"), active=br["active"],
                created_at=datetime.fromisoformat(br["created_at"]),
            )
            matches.append(
                Match(
                    match_id=mr["match_id"], guild_id=mr["guild_id"], seller=seller, buyer=buyer,
                    channel_id=mr.get("channel_id"), channel_name=mr.get("channel_name"),
                    created_at=datetime.fromisoformat(mr["created_at"]),
                    closed_at=datetime.fromisoformat(mr["closed_at"]) if mr.get("closed_at") else None,
                )
            )
        return matches

    async def update_match_channel(self, match_id: str, channel_id: int, channel_name: str) -> None:
        await self.client.patch(
            "market_matches",
            {"channel_id": channel_id, "channel_name": channel_name},
            params={"match_id": f"eq.{match_id}"},
        )


    @staticmethod
    def _compatible(seller: Any, buyer: Any) -> bool:
        from main import compatible_collection
        return (
            seller.active
            and buyer.active
            and seller.guild_id == buyer.guild_id
            and seller.currency == buyer.currency
            and buyer.amount >= seller.amount
            and compatible_collection(buyer.collection, seller.collection)
        )

    @staticmethod
    def _listing_from_row(r: Dict[str, Any]) -> Any:
        from main import Listing
        return Listing(
            listing_id=r["listing_id"],
            kind=r["kind"],
            guild_id=int(r["guild_id"]),
            user_id=int(r["user_id"]),
            user_name=r["user_name"],
            collection=r["collection"],
            currency=r["currency"],
            amount=Decimal(str(r["amount"])),
            details=r.get("details") or "",
            mint_address=r.get("mint_address") or "",
            active=bool(r["active"]),
            created_at=datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")),
        )

    async def _find_compatible(self, listing: Any, opposite_kind: str) -> Optional[Any]:
        records = await self.client.get(
            "market_listings",
            params={
                "guild_id": f"eq.{listing.guild_id}",
                "kind": f"eq.{opposite_kind}",
                "active": "eq.true",
                "order": "created_at.asc",
            },
        )
        for row in records:
            candidate = self._listing_from_row(row)
            seller, buyer = (listing, candidate) if listing.kind == "sell" else (candidate, listing)
            if self._compatible(seller, buyer):
                return candidate
        return None

    async def _create_match(self, seller: Any, buyer: Any) -> Any:
        from main import Match
        seller.active = False
        buyer.active = False
        await self.deactivate_listing(seller.listing_id)
        await self.deactivate_listing(buyer.listing_id)
        match = Match(
            match_id=__import__("uuid").uuid4().hex[:8],
            guild_id=seller.guild_id,
            seller=seller,
            buyer=buyer,
        )
        await self.add_match(match)
        return match

    async def add_sell_request(self, listing: Any) -> Optional[Any]:
        await self.add_listing(listing)
        buyer = await self._find_compatible(listing, "buy")
        if buyer is None:
            return None
        return await self._create_match(listing, buyer)

    async def add_buy_request(self, listing: Any) -> Optional[Any]:
        await self.add_listing(listing)
        seller = await self._find_compatible(listing, "sell")
        if seller is None:
            return None
        return await self._create_match(seller, listing)

    async def reconcile(self, guild_id: int) -> List[Any]:
        from main import Match
        listings = await self.get_active_listings(guild_id)
        sellers = [x for x in listings if x.kind == "sell" and x.active]
        buyers = [x for x in listings if x.kind == "buy" and x.active]
        created: List[Any] = []
        for seller in sellers:
            if not seller.active:
                continue
            buyer = next((b for b in buyers if b.active and self._compatible(seller, b)), None)
            if buyer is None:
                continue
            match = await self._create_match(seller, buyer)
            created.append(match)
        return created

    async def cancel_latest(self, user_id: int, request_type: str) -> List[Any]:
        records = await self.client.get(
            "market_listings",
            params={
                "user_id": f"eq.{user_id}",
                "active": "eq.true",
                "order": "created_at.desc",
            },
        )
        cancelled: List[Any] = []
        for row in records:
            if request_type != "both" and row["kind"] != request_type:
                continue
            listing = self._listing_from_row(row)
            await self.deactivate_listing(listing.listing_id)
            listing.active = False
            cancelled.append(listing)
            if request_type != "both":
                break
        return cancelled

    async def active_counts(self, guild_id: int) -> tuple[int, int]:
        records = await self.client.get(
            "market_listings",
            params={"guild_id": f"eq.{guild_id}", "active": "eq.true"},
        )
        buyers = {int(r["user_id"]) for r in records if r["kind"] == "buy"}
        sellers = {int(r["user_id"]) for r in records if r["kind"] == "sell"}
        return len(buyers), len(sellers)

    async def active_requests_for_user(self, guild_id: int, user_id: int) -> List[Any]:
        records = await self.client.get(
            "market_listings",
            params={
                "guild_id": f"eq.{guild_id}",
                "user_id": f"eq.{user_id}",
                "active": "eq.true",
                "order": "created_at.desc",
            },
        )
        return [self._listing_from_row(r) for r in records]

    async def matches_for_user(self, user_id: int) -> List[Any]:
        from main import Match
        records = await self.client.get(
            "market_matches",
            params={"order": "created_at.desc"},
        )
        matches: List[Any] = []
        for mr in records:
            s_res = await self.client.get("market_listings", params={"listing_id": f"eq.{mr['seller_listing_id']}"})
            b_res = await self.client.get("market_listings", params={"listing_id": f"eq.{mr['buyer_listing_id']}"})
            if not s_res or not b_res:
                continue
            seller = self._listing_from_row(s_res[0])
            buyer = self._listing_from_row(b_res[0])
            if seller.user_id != user_id and buyer.user_id != user_id:
                continue
            matches.append(Match(
                match_id=mr["match_id"],
                guild_id=int(mr["guild_id"]),
                seller=seller,
                buyer=buyer,
                channel_id=int(mr["channel_id"]) if mr.get("channel_id") else None,
                channel_name=mr.get("channel_name"),
                created_at=datetime.fromisoformat(mr["created_at"].replace("Z", "+00:00")),
                closed_at=datetime.fromisoformat(mr["closed_at"].replace("Z", "+00:00")) if mr.get("closed_at") else None,
            ))
        return matches


class SupabaseWalletStore:
    """Supabase-backed Wallet Store for verification sessions and linked wallets."""

    def __init__(self, client: SupabaseClient):
        self.client = client

    async def create_session(self, discord_user_id: int, ttl_seconds: int = 300) -> Any:
        from main import WalletSession, WALLET_NONCE_TTL_SECONDS
        import secrets
        from datetime import timedelta

        ttl_seconds = WALLET_NONCE_TTL_SECONDS
        token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        payload = {
            "token": token,
            "discord_user_id": discord_user_id,
            "nonce": nonce,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "attempts": 0,
            "used": False,
        }
        await self.client.post("wallet_sessions", payload)
        logger.info("Wallet verification requested for Discord user=%s, session=%s.", discord_user_id, token[:8])
        return WalletSession(token=token, discord_user_id=discord_user_id, nonce=nonce, created_at=now)

    async def get_session(self, token: str) -> Optional[Any]:
        from main import WalletSession
        res = await self.client.get("wallet_sessions", params={"token": f"eq.{token}"})
        if not res:
            return None
        r = res[0]
        return WalletSession(
            token=r["token"], discord_user_id=int(r["discord_user_id"]), nonce=r["nonce"],
            created_at=datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")),
            attempts=int(r.get("attempts", 0)), used=bool(r.get("used", False)),
        )

    @staticmethod
    def verification_message(session: Any) -> str:
        return (
            "NFT Market Wallet Verification\n"
            f"Discord User ID: {session.discord_user_id}\n"
            f"Nonce: {session.nonce}"
        )

    async def get_message(self, token: str) -> Optional[tuple[str, datetime]]:
        from main import WALLET_MAX_SIGNATURE_ATTEMPTS, utc_now
        session = await self.get_session(token)
        if (
            session is None or session.used or session.expires_at <= utc_now()
            or session.attempts >= WALLET_MAX_SIGNATURE_ATTEMPTS
        ):
            return None
        return self.verification_message(session), session.expires_at

    async def verify(self, token: str, public_key: str, encoded_signature: str) -> tuple[bool, str, Optional[Any]]:
        from main import (
            BadSignatureError, VerifyKey, WALLET_MAX_SIGNATURE_ATTEMPTS,
            WalletAssociation, decode_base58, utc_now, short_public_key,
        )
        session = await self.get_session(token)
        if session is None or session.used:
            return False, "This verification link is invalid or already used.", None
        if session.expires_at <= utc_now():
            await self.mark_session_used(token)
            return False, "This verification link has expired. Run /wallet again.", None
        if session.attempts >= WALLET_MAX_SIGNATURE_ATTEMPTS:
            await self.mark_session_used(token)
            return False, "Too many signature attempts. Run /wallet again.", None

        new_attempts = session.attempts + 1
        await self.client.patch("wallet_sessions", {"attempts": new_attempts}, params={"token": f"eq.{token}"})
        session.attempts = new_attempts

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
            VerifyKey(public_key_bytes).verify(self.verification_message(session).encode("utf-8"), signature_bytes)
        except (BadSignatureError, ValueError, binascii.Error, TypeError):
            if new_attempts >= WALLET_MAX_SIGNATURE_ATTEMPTS:
                await self.mark_session_used(token)
            return False, "The wallet signature is invalid.", None

        existing_user = await self.client.get("wallet_associations", params={"public_key": f"eq.{public_key}"})
        if existing_user and int(existing_user[0]["discord_user_id"]) != session.discord_user_id:
            await self.mark_session_used(token)
            return False, "This wallet is already associated with another Discord account.", None

        existing_association = await self.get_wallet(session.discord_user_id)
        if existing_association is not None and existing_association.public_key != public_key:
            await self.mark_session_used(token)
            return False, "A different wallet is already associated. Use /wallet-remove first.", None

        association = await self.associate_wallet(session.discord_user_id, public_key)
        await self.mark_session_used(token)
        logger.info("Wallet associated with Discord user=%s, wallet=%s.", session.discord_user_id, short_public_key(public_key))
        return True, "Wallet verified successfully.", association

    async def mark_session_used(self, token: str) -> None:
        await self.client.patch("wallet_sessions", {"used": True}, params={"token": f"eq.{token}"})

    async def associate_wallet(self, discord_user_id: int, public_key: str) -> Any:
        from main import WalletAssociation
        now = datetime.now(timezone.utc)
        payload = {"discord_user_id": discord_user_id, "public_key": public_key, "verified_at": now.isoformat()}
        await self.client.post("wallet_associations", payload, on_conflict="discord_user_id")
        return WalletAssociation(discord_user_id=discord_user_id, public_key=public_key, verified_at=now)

    async def get_wallet(self, discord_user_id: int) -> Optional[Any]:
        from main import WalletAssociation
        res = await self.client.get("wallet_associations", params={"discord_user_id": f"eq.{discord_user_id}"})
        if not res:
            return None
        r = res[0]
        return WalletAssociation(
            discord_user_id=int(r["discord_user_id"]), public_key=r["public_key"],
            verified_at=datetime.fromisoformat(r["verified_at"].replace("Z", "+00:00")),
        )

    async def get_association(self, discord_user_id: int) -> Optional[Any]:
        return await self.get_wallet(discord_user_id)

    async def remove_wallet(self, discord_user_id: int) -> bool:
        res = await self.client.delete("wallet_associations", params={"discord_user_id": f"eq.{discord_user_id}"})
        return len(res) > 0

    async def remove_association(self, discord_user_id: int) -> Optional[Any]:
        existing = await self.get_wallet(discord_user_id)
        if existing is None:
            return None
        await self.remove_wallet(discord_user_id)
        return existing


def get_stores():
    """Factory creating stores based on environment variables."""
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_key = (
        os.getenv("SUPABASE_SECRET_KEY", "").strip()
        or os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    )

    if supabase_url and supabase_key:
        logger.info("⚡ Supabase credentials detected -> Using SupabaseStore")
        client = SupabaseClient(supabase_url, supabase_key)
        return SupabaseMarketStore(client), SupabaseWalletStore(client)
    else:
        logger.info("ℹ️ Supabase not configured -> Using default InMemoryStore fallback")
        from main import InMemoryStore, WalletStore
        return InMemoryStore(), WalletStore()
