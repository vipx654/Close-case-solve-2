import logging
import asyncio
import re
import aiohttp
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from info import ADMINS, LOG_CHANNEL, CHANNELS  # noqa: F401

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# In-memory store for YTS magnet links (keyed by short hash)
# Telegram does NOT allow magnet: scheme in inline URL buttons, so we cache
# magnets here and deliver them via callback_data.
_YTS_MAGNETS: dict[str, str] = {}

# In-memory store for 1337x detail-page URLs. Telegram inline-button
# callback_data is limited to 64 bytes; a 1337x URL far exceeds that and would
# raise BUTTON_DATA_INVALID, so we cache the URL and pass only a short key.
_L337X_URLS: dict[str, str] = {}

# Cap the in-memory caches so they can't grow without bound.
_MAX_CACHE = 2000

YTS_API = "https://yts.mx/api/v2/list_movies.json"
L337X_BASE = "https://www.1337x.to"
# 1337x main domain is frequently Cloudflare-blocked / geo-blocked; try these
# mirrors in order and use the first one that returns a 200 result page.
L337X_MIRRORS = [
    "https://www.1337x.to",
    "https://1337x.to",
    "https://1337x.st",
    "https://x1337x.ws",
    "https://x1337x.eu",
    "https://1337x.tw",
    "https://1337x.tc",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
QUALITY_EMOJI = {"720p": "🎬", "1080p": "🔥", "2160p": "⚡", "3D": "🥽"}


# ─── YTS (movies) ─────────────────────────────────────────────────────────────

async def search_yts(query: str, limit: int = 5):
    """
    Search YTS API for movies.
    Returns list of dicts with title, year, rating, torrents.
    """
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            params = {
                "query_term": query,
                "limit": limit,
                "sort_by": "seeds",
                "order_by": "desc",
            }
            async with session.get(YTS_API, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

        if data.get("status") != "ok":
            return []

        movies = data.get("data", {}).get("movies") or []
        results = []
        for movie in movies:
            torrents = movie.get("torrents") or []
            results.append({
                "title": movie.get("title", "Unknown"),
                "year": movie.get("year", ""),
                "rating": movie.get("rating", "N/A"),
                "genres": ", ".join(movie.get("genres") or []),
                "summary": (movie.get("summary") or "")[:300],
                "cover": movie.get("medium_cover_image", ""),
                "url": movie.get("url", ""),
                "torrents": [
                    {
                        "quality": t.get("quality", ""),
                        "type": t.get("type", ""),
                        "size": t.get("size", ""),
                        "seeds": t.get("seeds", 0),
                        "magnet": _build_magnet(t.get("hash", ""), movie.get("title", "")),
                        "url": t.get("url", ""),
                    }
                    for t in torrents
                ],
                "source": "YTS",
            })
        return results

    except asyncio.TimeoutError:
        logger.warning("YTS API timeout")
        return []
    except Exception as e:
        logger.exception(f"YTS search error: {e}")
        return []


def _build_magnet(hash_: str, title: str) -> str:
    if not hash_:
        return ""
    trackers = (
        "udp://open.demonii.com:1337/announce&"
        "udp://tracker.openbittorrent.com:80&"
        "udp://tracker.coppersurfer.tk:6969&"
        "udp://glotorrents.pw:6969/announce&"
        "udp://tracker.opentrackr.org:1337/announce&"
        "udp://torrent.gresille.org:80/announce&"
        "udp://p4p.arenabg.com:1337&"
        "udp://tracker.leechers-paradise.org:6969"
    )
    return f"magnet:?xt=urn:btih:{hash_}&dn={title}&tr={trackers}"


# ─── 1337x (movies + series) ──────────────────────────────────────────────────

async def search_1337x(query: str, limit: int = 8):
    """
    Scrape 1337x (trying several mirrors) for movies/series results.
    Returns list of dicts with title, size, seeds, detail_url, detail_path.
    """
    q = query.replace(' ', '+')
    last_err = None
    for base in L337X_MIRRORS:
        try:
            search_url = f"{base}/search/{q}/1/"
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=12),
                                       allow_redirects=True) as resp:
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status} from {base}"
                        continue
                    html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select("table.table-list tbody tr")
            if not rows:
                # Page loaded but has no results -> empty query result, stop.
                if "table-list" in html:
                    return []
                last_err = f"no table on {base}"
                continue  # likely a Cloudflare challenge page -> try next mirror

            results = []
            for row in rows[:limit]:
                try:
                    name_tag = row.select_one("td.name a:nth-of-type(2)")
                    seed_tag = row.select_one("td.seeds")
                    size_tag = row.select_one("td.size")
                    if not name_tag:
                        continue
                    title = name_tag.get_text(strip=True)
                    detail_path = name_tag.get("href", "")
                    seeds = seed_tag.get_text(strip=True) if seed_tag else "0"
                    size_text = size_tag.get_text(strip=True) if size_tag else "?"
                    size_clean = "".join(c for c in size_text if c in "0123456789.GMKBgmkb ").strip()
                    results.append({
                        "title": title,
                        "seeds": int(seeds) if seeds.isdigit() else 0,
                        "size": size_clean,
                        "detail_path": detail_path if detail_path.startswith("/") else "/" + detail_path,
                        "detail_url": base + (detail_path if detail_path.startswith("/") else "/" + detail_path),
                        "source": "1337x",
                    })
                except Exception:
                    continue
            return results
        except asyncio.TimeoutError:
            last_err = f"timeout {base}"
            continue
        except Exception as e:
            last_err = f"{base}: {e}"
            continue
    logger.warning(f"1337x all mirrors failed: {last_err}")
    return []


def _detail_path_from_url(detail_url: str) -> str:
    """Extract the /torrent/.../ path from a (possibly mirror-based) detail url."""
    m = re.search(r"(/torrent/.*)", detail_url)
    return m.group(1) if m else ""


async def get_1337x_magnet(detail_url: str) -> str:
    """Fetch a magnet link from a 1337x detail page, trying mirrors if needed."""
    path = _detail_path_from_url(detail_url)
    candidates = [detail_url] + [b.rstrip("/") + path for b in L337X_MIRRORS if path]
    seen = set()
    for url in candidates:
        if url in seen or not url:
            continue
        seen.add(url)
        try:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                       allow_redirects=True) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            magnet_tag = soup.select_one('a[href^="magnet:"]')
            if magnet_tag and magnet_tag.get("href"):
                return magnet_tag["href"]
        except Exception:
            continue
    return ""


# ─── Combined search ──────────────────────────────────────────────────────────

async def search_torrents(query: str):
    """
    Search both YTS and 1337x concurrently.
    Returns (yts_results, l337x_results).
    """
    yts_task = asyncio.create_task(search_yts(query))
    l337x_task = asyncio.create_task(search_1337x(query))
    yts_results, l337x_results = await asyncio.gather(yts_task, l337x_task)
    return yts_results, l337x_results


# ─── Format results as Telegram message ───────────────────────────────────────

def _magnet_key(magnet: str) -> str:
    """Return a short key for storing a value in the in-memory caches."""
    import hashlib
    return hashlib.md5(magnet.encode()).hexdigest()[:16]


def _cache_put(store: dict, key: str, value: str) -> None:
    """Store key->value, trimming the oldest entries if the cache is full."""
    if len(store) >= _MAX_CACHE:
        for old in list(store)[: _MAX_CACHE // 4]:
            store.pop(old, None)
    store[key] = value


def format_torrent_results(yts: list, l337x: list, query: str) -> tuple:
    """
    Build message text + inline keyboard from torrent results.
    Returns (text, reply_markup) or (None, None) if nothing found.

    NOTE: Telegram does NOT allow magnet: URLs in inline buttons.
    YTS magnets are stored in _YTS_MAGNETS and delivered via callback.
    """
    if not yts and not l337x:
        return None, None

    lines = [f"🌐 <b>Torrent results for:</b> <code>{query}</code>\n"]
    buttons = []

    if yts:
        lines.append("━━━━ 🎬 YTS ━━━━")
        for movie in yts[:3]:
            try:
                stars = "⭐" * round(float(movie.get("rating") or 0) / 2)
            except Exception:
                stars = ""
            lines.append(
                f"\n<b>{movie['title']} ({movie['year']})</b>\n"
                f"{stars} {movie.get('rating', 'N/A')}/10  |  {movie.get('genres', '')}"
            )
            row = []
            for t in movie.get("torrents", [])[:4]:
                q = t.get("quality", "")
                sz = t.get("size", "")
                seeds = t.get("seeds", 0)
                emoji = QUALITY_EMOJI.get(q, "📥")
                label = f"{emoji} {q} [{sz}] 🌱{seeds}"
                magnet = t.get("magnet", "")
                if magnet:
                    key = _magnet_key(magnet)
                    _cache_put(_YTS_MAGNETS, key, magnet)
                    row.append(
                        InlineKeyboardButton(label, callback_data=f"yts_magnet#{key}")
                    )
            if row:
                for i in range(0, len(row), 2):
                    buttons.append(row[i : i + 2])

        if len(yts) > 3:
            lines.append(f"\n<i>+{len(yts)-3} more on YTS</i>")

    if l337x:
        lines.append("\n━━━━ 🔍 1337x ━━━━")
        for item in l337x[:5]:
            seeds = item.get("seeds", 0)
            size = item.get("size", "?")
            seed_icon = "🟢" if seeds > 50 else ("🟡" if seeds > 10 else "🔴")
            lines.append(
                f"\n{seed_icon} <b>{item['title'][:60]}</b>\n"
                f"   Size: {size}  Seeds: {seeds}"
            )
            # Cache the detail URL and put only a short key in callback_data
            # (Telegram limits callback_data to 64 bytes).
            url = item.get("detail_url", "")
            key = _magnet_key(url)
            _cache_put(_L337X_URLS, key, url)
            buttons.append([
                InlineKeyboardButton(
                    f"🔗 Get link — {item['title'][:35]}",
                    callback_data=f"torrent_magnet#{key}"
                )
            ])

    buttons.append([
        InlineKeyboardButton("❌ Close", callback_data="torrent_close")
    ])

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons)


# ─── Callback: fetch magnet on demand ────────────────────────────────────────

async def post_to_channel(client, text: str, markup: InlineKeyboardMarkup):
    """Post torrent results to the main index channel for record-keeping."""
    if not CHANNELS:
        return
    try:
        await client.send_message(
            chat_id=CHANNELS[0],
            text=text,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"Could not post torrent result to channel: {e}")


@Client.on_callback_query(filters.regex(r"^torrent_magnet#"))
async def send_magnet_link(client, query):
    key = query.data.split("#", 1)[1]
    detail_url = _L337X_URLS.get(key)
    if not detail_url:
        return await query.answer(
            "❌ Link expired. Search again to get a fresh one.", show_alert=True
        )
    await query.answer("Fetching magnet link…", show_alert=False)
    magnet = await get_1337x_magnet(detail_url)
    if magnet:
        await query.message.reply_text(
            f"🧲 <b>Magnet Link:</b>\n\n<code>{magnet}</code>",
            quote=True,
        )
    else:
        await query.answer("❌ Could not fetch magnet link. Try opening the page manually.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^yts_magnet#"))
async def send_yts_magnet(client, query):
    key = query.data.split("#", 1)[1]
    magnet = _YTS_MAGNETS.get(key)
    if magnet:
        await query.answer("Here's your magnet link!", show_alert=False)
        await query.message.reply_text(
            f"🧲 <b>YTS Magnet Link:</b>\n\n<code>{magnet}</code>",
            quote=True,
        )
    else:
        await query.answer("❌ Link expired. Search again to get a fresh one.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^torrent_close$"))
async def close_torrent_results(client, query):
    await query.message.delete()
    await query.answer()


# ─── Admin: /searchtorrent ─────────────────────────────────────────────────────

@Client.on_message(filters.command(["searchtorrent", "torrent"]) & filters.private & filters.user(ADMINS) & filters.incoming)
async def manual_torrent_search(client, message):
    if len(message.command) < 2:
        return await message.reply_text(
            "Usage: <code>/torrent movie name</code>"
        )
    query = " ".join(message.command[1:])
    status = await message.reply_text(f"🔍 Searching torrents for: <code>{query}</code>…")

    yts, l337x = await search_torrents(query)
    text, markup = format_torrent_results(yts, l337x, query)

    if text:
        await status.delete()
        await message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)
    else:
        await status.edit_text(f"❌ No torrent results found for: <code>{query}</code>")
