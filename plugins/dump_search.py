import logging
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, ChannelInvalid, ChatAdminRequired
from database.ia_filterdb import save_file, get_search_results
from info import DUMP_CHANNEL, CHANNELS, LOG_CHANNEL, ADMINS
from database.users_chats_db import db

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ─── Core: search dump channel and import to DB ──────────────────────────────

async def search_and_import_from_dump(client, query: str, limit: int = 10):
    """
    Search DUMP_CHANNEL for files matching query.
    Saves found files to DB and copies them to the main index channel.
    Returns a list of Media DB objects ready to show to the user.
    """
    if not DUMP_CHANNEL:
        return []

    total_saved = 0

    for msg_filter, ftype in [
        (enums.MessagesFilter.VIDEO, "video"),
        (enums.MessagesFilter.DOCUMENT, "document"),
        (enums.MessagesFilter.AUDIO, "audio"),
    ]:
        try:
            async for message in client.search_messages(
                chat_id=DUMP_CHANNEL,
                query=query,
                filter=msg_filter,
                limit=limit,
            ):
                media = getattr(message, ftype, None)
                if not media:
                    continue

                media.file_type = ftype
                media.caption = message.caption

                saved, status = await save_file(media)
                if saved:
                    total_saved += 1
                    if CHANNELS:
                        try:
                            await client.copy_message(
                                chat_id=CHANNELS[0],
                                from_chat_id=DUMP_CHANNEL,
                                message_id=message.id,
                            )
                        except Exception as copy_err:
                            logger.warning(f"Dump→channel copy failed: {copy_err}")
                elif status == 0:
                    total_saved += 1

        except FloodWait as e:
            logger.warning(f"FloodWait {e.value}s during dump search")
            await asyncio.sleep(e.value)
        except (ChannelInvalid, ChatAdminRequired) as e:
            logger.error(f"Dump channel access error: {e}")
            break
        except Exception as e:
            logger.exception(f"Dump search error ({ftype}): {e}")

    if total_saved == 0:
        return []

    try:
        results, _, _ = await get_search_results(
            chat_id=None,
            query=query,
            max_results=limit,
            offset=0,
        )
        return results
    except Exception as e:
        logger.exception(f"DB fetch after dump search: {e}")
        return []


async def notify_log_channel(client, user, query):
    """Log a dump-channel fetch event to the admin log channel."""
    try:
        await client.send_message(
            chat_id=LOG_CHANNEL,
            text=(
                f"#DumpFetch\n\n"
                f"User: {user.mention} (<code>{user.id}</code>)\n"
                f"Query: <code>{query}</code>\n"
                f"Status: Found in dump channel ✅"
            ),
        )
    except Exception:
        pass


# ─── /finddump — auto-detect the best dump channel ───────────────────────────

@Client.on_message(filters.command("finddump") & filters.user(ADMINS))
async def find_best_dump_channel(client, message):
    """
    Scans every channel the bot is an admin of, counts media files,
    and recommends the one with the most files as DUMP_CHANNEL.
    Usage: /finddump
    """
    status = await message.reply_text(
        "🔍 <b>Scanning all channels for media count…</b>\n"
        "<i>This may take a moment.</i>"
    )

    results = []
    checked = 0

    try:
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            if chat.type not in (enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP):
                continue

            # Skip channels already set as index channels
            if chat.id in CHANNELS:
                continue

            try:
                member = await client.get_chat_member(chat.id, "me")
                if member.status not in (
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ):
                    continue
            except Exception:
                continue

            # Count media messages (videos + documents)
            video_count = 0
            doc_count = 0
            try:
                async for _ in client.search_messages(
                    chat.id, filter=enums.MessagesFilter.VIDEO, limit=1
                ):
                    pass
                # Use get_chat for member count as a proxy for content size
                full = await client.get_chat(chat.id)
                video_count = getattr(full, "members_count", 0) or 0
            except Exception:
                pass

            try:
                count = 0
                async for _ in client.search_messages(
                    chat.id, filter=enums.MessagesFilter.VIDEO, limit=200
                ):
                    count += 1
                async for _ in client.search_messages(
                    chat.id, filter=enums.MessagesFilter.DOCUMENT, limit=200
                ):
                    count += 1
                video_count = count
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass

            results.append((chat.id, chat.title or str(chat.id), video_count))
            checked += 1

            if checked % 5 == 0:
                await status.edit_text(
                    f"🔍 <b>Scanning channels…</b> ({checked} checked so far)"
                )

    except Exception as e:
        logger.exception(f"finddump scan error: {e}")

    if not results:
        return await status.edit_text(
            "❌ No eligible channels found.\n\n"
            "Make sure the bot is an <b>admin</b> in your dump channel."
        )

    # Sort by media count descending
    results.sort(key=lambda x: x[2], reverse=True)
    top = results[:5]

    lines = []
    for i, (cid, title, count) in enumerate(top, 1):
        star = " ⭐ <b>RECOMMENDED</b>" if i == 1 else ""
        lines.append(
            f"{i}. <b>{title}</b>{star}\n"
            f"   ID: <code>{cid}</code>\n"
            f"   Media found: <b>{count}</b> files"
        )

    best_id = top[0][0]
    best_title = top[0][1]

    await status.edit_text(
        f"📊 <b>Top channels by media count:</b>\n\n"
        + "\n\n".join(lines)
        + f"\n\n─────────────────\n"
        f"✅ <b>Best choice:</b> <code>{best_id}</code> ({best_title})\n\n"
        f"Set this as your dump channel:\n"
        f"<code>DUMP_CHANNEL={best_id}</code>\n\n"
        f"Then run /testdump &lt;movie name&gt; to verify it works."
    )


# ─── /testdump — manually test a dump search ─────────────────────────────────

@Client.on_message(filters.command("testdump") & filters.user(ADMINS))
async def test_dump_search(client, message):
    """
    Admin command: /testdump <movie name>
    Manually trigger a dump channel search to test it.
    """
    if not DUMP_CHANNEL:
        return await message.reply_text(
            "❌ <b>DUMP_CHANNEL</b> is not configured.\n\n"
            "Run /finddump to find your best channel, then set:\n"
            "<code>DUMP_CHANNEL=&lt;channel_id&gt;</code>"
        )

    if len(message.command) < 2:
        return await message.reply_text(
            "Usage: <code>/testdump movie name</code>\n\n"
            f"Current dump channel: <code>{DUMP_CHANNEL}</code>"
        )

    query = " ".join(message.command[1:])
    status_msg = await message.reply_text(
        f"🔍 Searching dump channel for: <code>{query}</code>"
    )

    results = await search_and_import_from_dump(client, query, limit=5)

    if results:
        names = "\n".join([f"• {f.file_name}" for f in results[:5]])
        await status_msg.edit_text(
            f"✅ <b>Found {len(results)} file(s):</b>\n\n{names}\n\n"
            f"Files saved to DB and copied to main channel. ✅"
        )
    else:
        await status_msg.edit_text(
            f"❌ Nothing found in dump channel for: <code>{query}</code>\n\n"
            f"• Make sure bot is admin in <code>{DUMP_CHANNEL}</code>\n"
            f"• Try a shorter search term"
        )


# ─── /setdump — show config instructions ─────────────────────────────────────

@Client.on_message(filters.command("setdump") & filters.user(ADMINS))
async def set_dump_channel(client, message):
    current = f"<code>{DUMP_CHANNEL}</code>" if DUMP_CHANNEL else "❌ Not set"
    await message.reply_text(
        f"<b>📦 Dump Channel Config</b>\n\n"
        f"Current: {current}\n\n"
        f"<b>Steps:</b>\n"
        f"1. Run /finddump — bot scans and recommends the best channel\n"
        f"2. Copy the channel ID shown\n"
        f"3. Set env var: <code>DUMP_CHANNEL=&lt;id&gt;</code>\n"
        f"4. Restart the bot\n"
        f"5. Run /testdump &lt;movie name&gt; to confirm\n\n"
        f"<b>How it works:</b>\n"
        f"When a user requests a movie not in the DB, the bot automatically "
        f"searches this dump channel, imports the file, and delivers it to the user."
    )
