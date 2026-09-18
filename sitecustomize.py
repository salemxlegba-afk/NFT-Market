"""Startup hooks for NFT Market.

Python imports sitecustomize before executing main.py. We use that startup point
to load the paid-access extension without touching the existing wallet or
marketplace implementation.
"""
import os

from discord.ext import commands

from supabase_store import SupabaseClient

_original_bot_init = commands.Bot.__init__


def _bot_init(self, *args, **kwargs):
    _original_bot_init(self, *args, **kwargs)
    url = os.getenv("SUPABASE_URL", "").strip()
    key = (
        os.getenv("SUPABASE_SECRET_KEY", "").strip()
        or os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    )
    if url and key:
        self.paid_access_client = SupabaseClient(url, key)


commands.Bot.__init__ = _bot_init

import payment_extension  # noqa: E402,F401
