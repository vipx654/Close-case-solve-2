"""Auto web-fetch agent.

When a requested file is NOT found in the database and AUTO_FETCH is enabled,
the agent:
  1. searches the same sources as the torrent fallback (YTS + 1337x),
  2. picks the best match (most seeders under MAX_FETCH_SIZE_MB),
  3. downloads it with `aria2c` (handles magnet links + direct .torrent/http),
  4. processes the result (fast remux to streamable MP4 + thumbnail when ffmpeg
     is present),
  5. uploads the file into the LOG/channel and indexes it via save_file(),
  6. delivers it straight to the requesting user.

Heavy operations are background tasks; the user gets progress messages.
Everything degrades gracefully if aria2c/ffmpeg are missing or a download
fails — it never raises into the normal search flow.

Guardrails:
  * global concurrency semaphore (MAX_CONCURRENT_FETCH),
  * size cap (MAX_FETCH_SIZE_MB) — estimated from YTS size / 1337x title,
  * per-chat allow-list (FETCH_ALLOWED_CHATS),
  * one fetch per chat+query (dedupe), timeouts on every subprocess.
"""
import os
import re
import asyncio
import logging
import shutil
import subprocess

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from info import (
    ADMINS, LOG_CHANNEL, AUTO_FETCH, FETCH_ON_REQUEST, MAX_FETCH_SIZE_MB,
    MAX_CONCURRENT_FETCH, FETCH_TIMEOUT, DOWNLOAD_DIR, FETCH_ALLOWED_CHATS,
    AUTO_CONVERT, SUPPORT_CHAT,
)
from database.ia_filterdb import save_file
from database.users_chats_db import db
from util.media_processor import prepare_video, cleanup_files, is_video, is_audio, tools_available as ffmpeg_available

logger = logging.getLogger(__name__)

ARIA2C = shutil.which("aria2c")

# Dedupe: avoid the same query being fetched twice concurrently. Key = chatid:query
_INFLIGHT: set = set()
_SEM = asyncio.Semaphore(max(1, MAX_CONCURRENT_FETCH))

_VIDEO_AUDIO_EXT = (
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".flv", ".ts", ".wmv",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav",
)


def _size_mb_from_yts(item: dict) -> float:
    """YTS result carries size; parse '1.4 GB' / '700 MB'."""
    size = str(item.get("size", "") or item.get("size_bytes", 0))
    m = re.match(r"([\d.]+)\s*(GB|MB|KB)", size, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        unit = m.group(2).upper()
        return val * 1024 if unit == "GB" else (val / 1024 if unit == "KB" else val)
    # size_bytes
    try:
        return float(item.get("size_bytes", 0)) / (1024 * 1024)
    except (TypeError, ValueError):
        return 0.0


def _size_mb_from_text(text: str) -> float:
    m = re.search(r"([\d.]+)\s*(GB|MB)\b", text or "", re.IGNORECASE)
    if not m:
        return 0.0
    val = float(m.group(1))
    return val * 1024 if m.group(2).upper() == "GB" else val


def _pick_best(yts: list, l337x: list) -> dict | None:
    """Choose the highest-seeded candidate within the size cap. Returns dict."""
    candidates = []
    for t in yts or []:
        mb = _size_mb_from_yts(t)
        seeds = int(t.get("seeds", t.get("seeders", 0)) or 0)
        mag = t.get("magnet", "")
        if not mag:
            continue
        if mb and mb > MAX_FETCH_SIZE_MB:
            continue
        candidates.append({"title": t.get("title", "movie"), "magnet": mag,
                           "seeds": seeds, "mb": mb, "source": "YTS"})
    for t in l337x or []:
        # Prefer the parsed size column; fall back to anything in the title.
        mb = _size_mb_from_text(t.get("size", "")) or _size_mb_from_text(t.get("title", ""))
        seeds = int(t.get("seeds", t.get("seeders", 0)) or 0)
        url = t.get("detail_url", "")
        if not url:
            continue
        if mb and mb > MAX_FETCH_SIZE_MB:
            continue
        candidates.append({"title": t.get("title", "movie"), "detail_url": url,
                           "seeds": seeds, "mb": mb, "source": "1337x"})
    if not candidates:
        return None
    # Most seeders; unknown size (mb==0) sorts after sized ones only if capped.
    candidates.sort(key=lambda c: (c["seeds"], 1 if c["mb"] else 0), reverse=True)
    return candidates[0]


async def _resolve_magnet(pick: dict) -> str:
    if pick.get("magnet"):
        return pick["magnet"]
    # 1337x needs its detail page scraped for the magnet.
    try:
        from plugins.torrent_search import get_1337x_magnet
        return await get_1337x_magnet(pick["detail_url"])
    except Exception as e:
        logger.warning(f"resolve magnet failed: {e}")
        return ""


async def _aria_download(magnet: str, dest_dir: str) -> list:
    """Run aria2c on a magnet / .torrent / http link. Return downloaded file paths."""
    if not ARIA2C:
        raise RuntimeError("aria2c not installed")
    os.makedirs(dest_dir, exist_ok=True)
    cmd = [
        ARIA2C,
        "--seed-time=0",            # don't seed
        "--bt-metadata-only=false",
        "--bt-save-metadata=false",
        f"--max-overall-download-limit=0",
        f"--download-result=hide",
        "--summary-interval=0",
        "--console-log-level=warn",
        "--file-allocation=none",
        f"--max-download-limit=0",
        f"-d", dest_dir,
        magnet,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("download timed out")
    files = []
    for root, _, names in os.walk(dest_dir):
        for n in names:
            if n.lower().endswith(_VIDEO_AUDIO_EXT) or (not n.startswith(".") and "." in n and not n.endswith((".aria2", ".torrent", ".!ut"))):
                fp = os.path.join(root, n)
                if os.path.getsize(fp) > 0:
                    files.append(fp)
    # Prefer media files over samples / extras.
    media = [f for f in files if f.lower().endswith(_VIDEO_AUDIO_EXT)]
    media = [f for f in media if "sample" not in os.path.basename(f).lower()]
    media.sort(key=lambda f: os.path.getsize(f), reverse=True)
    return media or files


async def _upload_and_index(client, file_path: str, chat_id: int):
    """Upload a local file to Telegram, save to DB, return the sent Message."""
    thumb = None
    prepared = None
    up_path = file_path
    if AUTO_CONVERT and is_video(file_path) and ffmpeg_available():
        prepared = await prepare_video(file_path, make_thumbnail=True)
        up_path = prepared.get("path", file_path)
        thumb = prepared.get("thumb")
    size = os.path.getsize(up_path)
    name = os.path.basename(up_path)

    target = LOG_CHANNEL if LOG_CHANNEL else chat_id
    sent = None
    if is_video(up_path):
        sent = await client.send_video(
            chat_id=target, video=up_path, caption=name,
            duration=(prepared or {}).get("duration", 0) or 0,
            width=(prepared or {}).get("width", 0) or 0,
            height=(prepared or {}).get("height", 0) or 0,
            thumb=thumb, file_name=name, supports_streaming=True,
        )
    elif is_audio(up_path):
        sent = await client.send_audio(chat_id=target, audio=up_path,
                                       caption=name, title=name, thumb=thumb)
    else:
        sent = await client.send_document(chat_id=target, document=up_path,
                                          caption=name, file_name=name)

    # Index into the search DB like /index does.
    try:
        if sent and sent.media:
            media = getattr(sent, sent.media.value, None)
            if media is not None:
                await save_file(media)
    except Exception as e:
        logger.warning(f"save_file after fetch failed: {e}")

    return sent, up_path


async def run_fetch(client, status_msg, chat_id: int, user_id: int, query: str, mention: str = ""):
    """Full pipeline. Sends its own progress messages. Never raises."""
    dedupe = f"{chat_id}:{query.lower().strip()}"
    if dedupe in _INFLIGHT:
        return
    _INFLIGHT.add(dedupe)
    work_dir = os.path.join(DOWNLOAD_DIR, f"fetch_{abs(chat_id)}_{abs(hash(query)) % 10_000_000}")
    try:
        if not ARIA2C:
            await status_msg.edit_text(
                "🤖 <b>Web agent is enabled but <code>aria2c</code> is not installed</b> on the server, "
                "so I can't auto-download yet. Ask the admin to install aria2."
            )
            return

        async with _SEM:
            # 1) search
            from plugins.torrent_search import search_torrents
            yts, l337x = await search_torrents(query)
            pick = _pick_best(yts, l337x)
            if not pick:
                await status_msg.edit_text(
                    f"🤖 Agent searched the web but found no downloadable result under "
                    f"<b>{MAX_FETCH_SIZE_MB} MB</b> for <code>{query}</code>."
                )
                return
            magnet = await _resolve_magnet(pick)
            if not magnet:
                await status_msg.edit_text("🤖 Couldn't obtain a magnet for the best match.")
                return

            size_txt = f"{pick['mb']:.0f} MB" if pick.get("mb") else "unknown size"
            await status_msg.edit_text(
                f"🤖 <b>Auto-fetch agent</b>\n\n"
                f"🔎 Query: <code>{query}</code>\n"
                f"🎯 Best match: <b>{pick['title']}</b> ({pick['source']})\n"
                f"📦 Size: {size_txt} · 🟢 Seeds: {pick['seeds']}\n\n"
                f"⬇️ Downloading via aria2… this can take a while."
            )

            # 2) download
            try:
                files = await _aria_download(magnet, work_dir)
            except Exception as e:
                await status_msg.edit_text(f"🤖 Download failed: <code>{e}</code>")
                return
            if not files:
                await status_msg.edit_text("🤖 Download produced no media files.")
                return
            media_path = files[0]
            mb = os.path.getsize(media_path) / (1024 * 1024)
            if mb > MAX_FETCH_SIZE_MB:
                await status_msg.edit_text(
                    f"🤖 Downloaded file is <b>{mb:.0f} MB</b> — over the {MAX_FETCH_SIZE_MB} MB cap; not uploaded."
                )
                return

            await status_msg.edit_text("⚙️ Processing (remux/thumbnail) & uploading to Telegram…")

            # 3) upload + index
            try:
                sent, up_path = await _upload_and_index(client, media_path, chat_id)
            except Exception as e:
                logger.exception(f"upload failed: {e}")
                await status_msg.edit_text(f"🤖 Upload failed: <code>{e}</code>")
                return

            # 4) deliver to the requesting chat
            await status_msg.delete()
            if sent.chat.id != chat_id:
                try:
                    await client.copy_message(chat_id=chat_id,
                                             from_chat_id=sent.chat.id,
                                             message_id=sent.id)
                except Exception as e:
                    logger.warning(f"copy fetched file to chat failed: {e}")
            await client.send_message(
                chat_id=chat_id,
                text=f"🤖 <b>Fetched from the web for you!</b>\n"
                     f"🎬 <b>{pick['title']}</b> ({mb:.0f} MB)\n"
                     f"<i>It's now in my database, so future searches find it instantly.</i>",
            )
            if LOG_CHANNEL:
                try:
                    await client.send_message(
                        LOG_CHANNEL,
                        f"#WebFetch 🤖\n👤 {mention} (<code>{user_id}</code>)\n"
                        f"📍 Chat <code>{chat_id}</code>\n🔎 <code>{query}</code>\n"
                        f"🎬 {pick['title']} ({pick['source']}, {mb:.0f} MB)",
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.exception(f"web agent error: {e}")
        try:
            await status_msg.edit_text(f"🤖 Auto-fetch hit an error: <code>{e}</code>")
        except Exception:
            pass
    finally:
        _INFLIGHT.discard(dedupe)
        # cleanup local temp files (thumbnails, downloaded/remuxed files)
        try:
            if os.path.isdir(work_dir):
                shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


def fetch_allowed(chat_id: int) -> bool:
    if not AUTO_FETCH:
        return False
    if FETCH_ALLOWED_CHATS:
        return chat_id in FETCH_ALLOWED_CHATS
    return True


@Client.on_message(filters.command(["fetch", "grab"]) & filters.user(ADMINS))
async def manual_fetch(client, message):
    """Admin: /fetch <name> — force the agent to grab a title."""
    if len(message.command) < 2:
        return await message.reply_text("Usage: <code>/fetch movie name</code>")
    query = message.text.split(" ", 1)[1].strip()
    if not ARIA2C:
        return await message.reply_text("🤖 <code>aria2c</code> is not installed — can't download.")
    status = await message.reply_text(f"🤖 Agent starting for <code>{query}</code>…")
    mention = message.from_user.mention if message.from_user else "admin"
    asyncio.create_task(run_fetch(client, status, message.chat.id,
                                  message.from_user.id if message.from_user else 0,
                                  query, mention))


async def maybe_auto_fetch(client, message, query: str):
    """Called from the search not-found path. Fires a background fetch."""
    if not fetch_allowed(message.chat.id):
        return
    if not query or len(query) < 3:
        return
    status = await message.reply_text(
        f"🤖 <b>Not in my database.</b> My web agent is searching &amp; trying to fetch "
        f"<code>{query}</code>… you'll be notified if it succeeds."
    )
    mention = message.from_user.mention if message.from_user else "user"
    asyncio.create_task(run_fetch(client, status, message.chat.id,
                                  message.from_user.id if message.from_user else 0,
                                  query, mention))
