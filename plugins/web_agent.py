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

# Format-picker state: key -> {"query":..., "cands":[...]} for the choice buttons.
_FETCH_CHOICES: dict = {}
_CHOICE_MAX = 2000

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


def _quality_of(text: str) -> str:
    """Best-effort quality/format label from a release title."""
    t = (text or "").lower()
    for tag in ["2160p", "4k", "uhd", "1080p", "720p", "480p", "360p", "web-dl", "webrip", "bluray", "bdrip", "brrip", "hdrip", "hevc", "x265", "x264"]:
        if tag in t:
            if tag == "4k":
                return "2160p"
            return tag.upper() if tag.endswith("p") else tag.upper()
    return "FILE"


def collect_candidates(yts: list, extras: list, limit: int = 6) -> list:
    """Return all downloadable, size-capped candidates across sources.

    Handles YTS movies (magnets nested under 'torrents') and flat extras
    (TPB with top-level 'magnet'/'size_mb'; 1337x with 'detail_url').
    Sorted best-first: direct magnets with the most seeders, capped to MAX.
    """
    candidates = []
    for movie in yts or []:
        for t in (movie.get("torrents") or []):
            mag = t.get("magnet", "")
            if not mag:
                continue
            mb = _size_mb_from_yts(t)
            seeds = int(t.get("seeds", 0) or 0)
            if mb and mb > MAX_FETCH_SIZE_MB:
                continue
            q = (t.get("quality", "") or "").upper() or "FILE"
            title = f"{movie.get('title', 'movie')} {movie.get('year', '')} {t.get('quality','')} {t.get('type','')}".strip()
            candidates.append({
                "title": title, "magnet": mag, "detail_url": "",
                "seeds": seeds, "mb": mb, "source": "YTS", "quality": q,
            })
    for it in extras or []:
        mag = it.get("magnet", "")
        url = it.get("detail_url", "")
        if not mag and not url:
            continue
        mb = it.get("size_mb") or _size_mb_from_text(it.get("size", "")) or _size_mb_from_text(it.get("title", ""))
        mb = float(mb or 0)
        if mb and mb > MAX_FETCH_SIZE_MB:
            continue
        seeds = int(it.get("seeds", it.get("seeders", 0)) or 0)
        candidates.append({
            "title": it.get("title", "movie"),
            "magnet": mag, "detail_url": url,
            "seeds": seeds, "mb": mb, "source": it.get("source", "?"),
            "quality": _quality_of(it.get("title", "")),
        })

    # de-dupe by magnet (or title), prefer direct magnets, sort by seeds
    seen = set()
    uniq = []
    for c in candidates:
        key = c.get("magnet") or c["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    uniq.sort(key=lambda c: (1 if c.get("magnet") else 0, c["seeds"]), reverse=True)
    return uniq[:limit]


def _pick_best(yts: list, extras: list) -> dict | None:
    """Highest-seeded candidate (kept for the manual /fetch command)."""
    cands = collect_candidates(yts, extras, limit=1)
    return cands[0] if cands else None


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


async def run_fetch(client, status_msg, chat_id: int, user_id: int, query: str,
                    mention: str = "", pick: dict | None = None):
    """Full pipeline. Sends its own progress messages. Never raises.

    `pick` may be a pre-selected candidate (from maybe_auto_fetch); if None
    the sources are searched here (used by the manual /fetch command).
    """
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
            # 1) search (unless a candidate was already chosen)
            if pick is None:
                from plugins.torrent_search import search_torrents
                yts, extras = await search_torrents(query)
                pick = _pick_best(yts, extras)
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


def _fmt_size(mb: float) -> str:
    mb = float(mb or 0)
    return f"{mb/1024:.2f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def _choice_key(chat_id: int, query: str) -> str:
    import hashlib
    return hashlib.md5(f"{chat_id}:{query.lower().strip()}".encode()).hexdigest()[:12]


async def maybe_auto_fetch(client, message, query: str) -> bool:
    """Called from the search not-found path.

    Searches the sources ONCE and, if downloadable matches exist, posts a
    format/quality picker (multiple buttons). The chosen format is fetched
    by the fetchpick# callback handler. Returns True if a picker was shown
    (so the caller skips the link/not-found fallbacks).
    """
    if not fetch_allowed(message.chat.id):
        return False
    if not ARIA2C:
        return False
    if not query or len(query) < 3:
        return False

    try:
        from plugins.torrent_search import search_torrents
        yts, extras = await search_torrents(query)
        cands = collect_candidates(yts, extras, limit=6)
    except Exception as e:
        logger.warning(f"web-agent search failed: {e}")
        return False
    if not cands:
        return False

    key = _choice_key(message.chat.id, query)
    if len(_FETCH_CHOICES) >= _CHOICE_MAX:
        _FETCH_CHOICES.pop(next(iter(_FETCH_CHOICES)))
    _FETCH_CHOICES[key] = {"query": query, "cands": cands}

    buttons = []
    for i, c in enumerate(cands):
        label = f"{c['quality']} · {_fmt_size(c['mb'])} · 🌱{c['seeds']}"
        buttons.append([InlineKeyboardButton(f"⬇️ {label}", callback_data=f"fetchpick#{key}#{i}")])
    buttons.append([InlineKeyboardButton("❌ Close", callback_data="fetchpick#close")])

    await message.reply_text(
        f"🤖 <b>Web agent found formats for:</b> <code>{query}</code>\n"
        f"Choose a quality to download & upload ({MAX_FETCH_SIZE_MB//1024} GB max):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return True


@Client.on_callback_query(filters.regex(r"^fetchpick#"))
async def fetch_pick_handler(client, query):
    data = query.data.split("#")
    if len(data) >= 2 and data[1] == "close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return await query.answer()

    if len(data) < 3:
        return await query.answer("Invalid choice, search again.", show_alert=True)
    _, key, idx = data
    entry = _FETCH_CHOICES.get(key)
    if not entry:
        return await query.answer("Choices expired. Please search again.", show_alert=True)
    try:
        cand = entry["cands"][int(idx)]
    except (IndexError, ValueError):
        return await query.answer("That format is no longer available.", show_alert=True)

    await query.answer("Starting download…")
    status = await query.message.reply_text(
        f"⬇️ <b>Downloading:</b> {cand['title'][:80]}\n"
        f"📦 {_fmt_size(cand['mb'])} · 🟢 {cand['seeds']} seeds ({cand['source']})\n"
        f"Please wait — this can take a few minutes."
    )
    try:
        await query.message.edit_reply_markup(None)
    except Exception:
        pass

    mention = query.from_user.mention if query.from_user else "user"
    asyncio.create_task(run_fetch(
        client, status, query.message.chat.id,
        query.from_user.id if query.from_user else 0,
        entry["query"], mention, pick=cand,
    ))
