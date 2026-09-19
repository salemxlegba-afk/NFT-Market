"""Startup hooks for NFT Market.

Keeps the paid-access extension loaded and makes the public menu self-cleaning:
one channel should not accumulate old duplicate NFT Market menus.
"""

import os

from discord.ext import commands

from supabase_store import SupabaseClient


_original_bot_init = commands.Bot.__init__


def _is_market_menu(message) -> bool:
    if getattr(message.author, "bot", False) is False:
        return False
    for embed in getattr(message, "embeds", []):
        if getattr(embed, "title", None) == "🖼️ NFT Market":
            return True
    return False


async def _cleanup_market_menus(channel, keep_message_id=None, limit=100):
    """Delete only bot-authored NFT Market menu duplicates in this channel."""
    try:
        messages = [message async for message in channel.history(limit=limit)]
    except Exception:
        return

    menu_messages = [
        message
        for message in messages
        if _is_market_menu(message)
        and (keep_message_id is None or message.id != keep_message_id)
    ]

    for message in menu_messages:
        try:
            await message.delete(
                reason="NFT Market UI cleanup: remove stale duplicate menu"
            )
        except Exception:
            pass


def _install_ui_cleanup(bot):
    async def cleanup_on_ready():
        if getattr(bot, "_nftmarket_ui_cleaned", False):
            return
        bot._nftmarket_ui_cleaned = True

        # Only remove old NFT Market menu embeds. User requests, deals,
        # wallet messages and normal channel messages are never touched.
        for guild in bot.guilds:
            for channel in guild.text_channels:
                try:
                    await _cleanup_market_menus(channel, limit=100)
                except Exception:
                    pass

    bot.add_listener(cleanup_on_ready, "on_ready")

    async def on_message_cleanup(message):
        if not _is_market_menu(message):
            return

        # Whenever a fresh menu is posted, remove older duplicate menus
        # from the same channel. The new menu is always preserved.
        await _cleanup_market_menus(
            message.channel,
            keep_message_id=message.id,
            limit=100,
        )

    bot.add_listener(on_message_cleanup, "on_message")


def _bot_init(self, *args, **kwargs):
    _original_bot_init(self, *args, **kwargs)

    url = os.getenv("SUPABASE_URL", "").strip()
    key = (
        os.getenv("SUPABASE_SECRET_KEY", "").strip()
        or os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    )
    if url and key:
        self.paid_access_client = SupabaseClient(url, key)

    _install_ui_cleanup(self)


commands.Bot.__init__ = _bot_init

import payment_extension  # noqa: E402,F401
import payment_ui_patch  # noqa: E402,F401
