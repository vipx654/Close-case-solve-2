import random
import asyncio
import logging
from pyrogram import Client, filters
from lazybot import LazyPrincessBot

logger = logging.getLogger(__name__)

# ONLY valid Telegram reaction emojis (not all emojis work as reactions)
REACTIONS = ["👍", "❤", "🔥", "🥰", "👏", "😁", "🤩", "🎉", "💯", "⚡"]

# Big animated emojis sent as a message (any emoji works here)
MOVIE_ANIMATIONS = ["🎬", "🎥", "🍿", "⭐", "🔥", "🎦"]


@LazyPrincessBot.on_message(
    filters.group & filters.incoming & ~filters.bot & ~filters.command([
        "start", "help", "settings", "filter", "filters", "connect",
        "disconnect", "id", "info", "stats", "ban", "unban",
        "broadcast", "grp_broadcast", "index", "deleteall"
    ])
)
async def auto_animate(client, message):
    # --- Reaction ---
    try:
        await client.send_reaction(
            chat_id=message.chat.id,
            message_id=message.id,
            emoji=random.choice(REACTIONS)
        )
    except Exception as e:
        logger.warning(f"Reaction failed: {e}")

    # --- Big animated emoji reply ---
    try:
        anim_msg = await message.reply(random.choice(MOVIE_ANIMATIONS))
        await asyncio.sleep(4)
        await anim_msg.delete()
    except Exception as e:
        logger.warning(f"Animation failed: {e}")