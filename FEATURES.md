# Newly Added Features

Three features added on top of the review fixes. All are **config-gated and
fail-safe**: if an external tool (aria2/ffmpeg) is missing or a step fails, the
bot degrades gracefully and the normal search flow keeps working.

---

## 1. 🛠 Button-driven Admin Panel — `plugins/admin_panel.py`

**Command:** `/admin` (aliases `/panel`, `/adminpanel`) — admins only.

Opens an inline menu so every admin action is a button tap instead of typing a
command. Reuses the same backend/db logic as the text commands (no duplicated
business rules).

| Button | Action |
|---|---|
| 📊 Stats | total users / chats / DB size |
| 👥 Users · 💬 Chats | counts |
| 💎 Premium users | lists active premium users |
| 📢 Broadcast to users / 📣 to groups | prompts you to send/forward the message |
| 🚫 Ban / ✅ Unban user | prompts for the numeric Telegram ID |
| 🗄 Delete ALL index | Yes/No confirm → drops the media collection |
| 📋 Logs | sends the log file to your PM |
| ⚙️ Group settings | points to `/settings` (per-chat) |
| 📺 Index channel | instructions for `/index` |
| 🤖 Web agent / 🎬 Convert mode | shows live status + tool availability |
| 🔄 Restart bot | Yes/No confirm → restarts the process |

- Actions needing input (`broadcast`, `ban`) put you into a one-shot prompt;
  send `/cancel` to abort. Handled in the bot's **PM**.
- Destructive actions (delete-all, restart) require an explicit confirmation.

All original text commands (`/stats`, `/broadcast`, `/ban`, …) keep working
exactly as before.

---

## 2. 🤖 Auto Web-Fetch Agent — `plugins/web_agent.py`

When a requested file is **not in the database**, the agent can automatically
search the web, download the best match, upload it, index it, and deliver it.

**Flow:** search YTS + 1337x → pick highest-seeded result under the size cap →
resolve magnet → `aria2c` download → (optional fast convert) → upload to the
storage channel → `save_file()` into the search DB → copy to the requester.

**Manual override:** `/fetch <movie name>` (admins) forces a grab at any time.

**Guardrails:** global concurrency semaphore, per-chat+query de-duplication,
size cap, per-chat allow-list, subprocess timeouts, temp-file cleanup after
each run. All errors are caught and reported as a status message (never crash
search).

### Config (env vars in `info.py`)
| Var | Default | Meaning |
|---|---|---|
| `AUTO_FETCH` | `False` | master switch — **set `True` to enable** |
| `MAX_FETCH_SIZE_MB` | `2048` | skip files bigger than this |
| `MAX_CONCURRENT_FETCH` | `1` | parallel downloads |
| `FETCH_TIMEOUT` | `1800` | seconds per download |
| `DOWNLOAD_DIR` | `downloads` | temp download folder |
| `FETCH_ALLOWED_CHATS` | *(empty)* | restrict to specific group ids; empty = everywhere |
| `FETCH_ON_REQUEST` | `True` | (reserved) also fetch from `/request` |

**Host requirement:** `aria2c` must be installed (`apt install aria2`). The
panel's "🤖 Web agent" button shows whether it's detected. If `AUTO_FETCH` is
off or aria2 is missing, the bot simply continues to the normal "not found"
message.

> ⚠️ This downloads torrents on your server — only enable where that's allowed
> and where you have the disk/bandwidth. It is OFF by default.

---

## 3. 🎬 Fast Video Convert (play directly in Telegram) — `util/media_processor.py`

Telegram streams **MP4 / H.264** natively; MKV/H.265 usually only download. In
**fast stream mode** (your choice) we never do a slow full re-encode:

1. `ffprobe` reads duration / width / height / codecs.
2. If the file isn't an MP4 (MKV, AVI, MOV…), **remux** to MP4 with
   `ffmpeg -c copy -movflags +faststart` — stream copy = seconds, not minutes —
   so playback starts before download finishes.
3. Generate a **thumbnail** (one frame, JPEG).
4. Upload via `send_video(..., supports_streaming=True)` with duration,
   dimensions and thumbnail → it plays inline in Telegram.
5. If a file can't be remuxed or ffmpeg is missing, it uploads as a document
   instead (no failure).

Used automatically by the web-fetch agent for downloaded videos.

### Config
| Var | Default | Meaning |
|---|---|---|
| `AUTO_CONVERT` | `True` | remux + thumbnail for fetched videos |
| `CONVERT_TIMEOUT` | `1800` | ffmpeg timeout per file |
| `DOWNLOAD_DIR` | `downloads` | temp folder |

**Host requirement:** `ffmpeg` + `ffprobe` (`apt install ffmpeg`). The panel's
"🎬 Convert mode" button reports whether they're detected.

---

## Files
- `plugins/admin_panel.py` — new (feature 1)
- `plugins/web_agent.py` — new (feature 2)
- `util/media_processor.py` — new (feature 3, shared by 2)
- `info.py` — new config block (`AUTO_FETCH*`, `AUTO_CONVERT`, `DOWNLOAD_DIR`…)
- `plugins/pmfilter.py` — auto_filter not-found chain now:
  DB → dump channel → torrents → spell-check → **web agent** → not-found message.

All files pass `python -m py_compile`.
