import random
from pyrogram import Client, filters
from pyrogram.types import ReactionTypeEmoji
from lazybot import LazyPrincessBot

# Big animated emojis sent as standalone messages
MOVIE_ANIMATIONS = ["🎬", "🎥", "🍿", "⭐", "🔥", "🎞️", "🎦"]

# Emoji reactions added to the message
REACTIONS = ["🔥", "❤️", "👍", "🎉", "⚡", "👏", "😍", "💯"]


@LazyPrincessBot.on_message(
    filters.group & filters.incoming & ~filters.bot & ~filters.command([
        "start", "help", "settings", "filter", "filters", "connect",
        "disconnect", "id", "info", "stats", "ban", "unban",
        "broadcast", "grp_broadcast", "index", "deleteall"
    ])
)
async def auto_animate(client, message):
    try:
        # React to the message with an animated emoji reaction
        await client.send_reaction(
            chat_id=message.chat.id,
            message_id=message.id,
            reaction=[ReactionTypeEmoji(emoji=random.choice(REACTIONS))]
        )
    except Exception:
        pass

    try:
        # Send a big animated emoji as a reply (plays large animation in Telegram)
        emoji = random.choice(MOVIE_ANIMATIONS)
        anim_msg = await message.reply(emoji)

        # Auto-delete the animation after 4 seconds so it doesn't clutter the chat
        import asyncio
        await asyncio.sleep(4)
        await anim_msg.delete()
    except Exception:
        pass