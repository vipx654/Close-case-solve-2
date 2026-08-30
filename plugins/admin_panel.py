"""Button-driven admin panel.

Gives admins a single /admin (and /panel) command that opens an inline menu so
every common admin action can be tapped instead of typed. Actions that need an
argument (broadcast text, ban id, premium grant) prompt for the next message.

It reuses the SAME backend logic/commands already in the bot — nothing here
duplicates business logic; it dispatches the same handlers / db calls.

All destructive actions get a Yes/No confirm.
"""
import os
import sys
import asyncio
import logging

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from info import ADMINS, LOG_CHANNEL, AUTO_FETCH, AUTO_CONVERT
from database.users_chats_db import db
from util.media_processor import tools_available as ffmpeg_available

logger = logging.getLogger(__name__)

# per-admin pending input action: {user_id: action_string}
_PENDING: dict = {}


def _is_admin(uid) -> bool:
    return uid is not None and int(uid) in ADMINS


def _main_menu():
    rows = [
        [InlineKeyboardButton("📊 Stats", callback_data="adm#stats"),
         InlineKeyboardButton("👥 Users", callback_data="adm#users")],
        [InlineKeyboardButton("💬 Chats", callback_data="adm#chats"),
         InlineKeyboardButton("💎 Premium users", callback_data="adm#premium_users")],
        [InlineKeyboardButton("📢 Broadcast to users", callback_data="adm#bc_users"),
         InlineKeyboardButton("📣 Broadcast to groups", callback_data="adm#bc_groups")],
        [InlineKeyboardButton("🚫 Ban user", callback_data="adm#ban"),
         InlineKeyboardButton("✅ Unban user", callback_data="adm#unban")],
        [InlineKeyboardButton("🗄 Delete ALL index", callback_data="adm#delall"),
         InlineKeyboardButton("📋 Logs", callback_data="adm#logs")],
        [InlineKeyboardButton("⚙️ Group settings", callback_data="adm#settings"),
         InlineKeyboardButton("📺 Index channel", callback_data="adm#index")],
        [InlineKeyboardButton("🤖 Web agent", callback_data="adm#agent"),
         InlineKeyboardButton("🎬 Convert mode", callback_data="adm#convert")],
        [InlineKeyboardButton("🔄 Restart bot", callback_data="adm#restart")],
        [InlineKeyboardButton("✖️ Close", callback_data="adm#close")],
    ]
    return InlineKeyboardMarkup(rows)


def _back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm#home")]])


@Client.on_message(filters.command(["admin", "panel", "adminpanel"]) & filters.user(ADMINS))
async def admin_panel(client, message):
    await message.reply_text(
        "🛠 <b>Admin Control Panel</b>\n\nTap an action. You can still use the text commands too.",
        reply_markup=_main_menu(),
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^adm#") & filters.user(ADMINS))
async def admin_panel_cb(client, query):
    data = query.data.split("#", 1)[1]
    msg = query.message

    if data == "home":
        return await msg.edit_text("🛠 <b>Admin Control Panel</b>", reply_markup=_main_menu(),
                                   parse_mode=enums.ParseMode.HTML)
    if data == "close":
        try:
            return await msg.delete()
        except Exception:
            return await query.answer()

    if data == "stats":
        users = await db.total_users_count()
        chats = await db.total_chat_count()
        try:
            size = await db.get_db_size()
            free = 536870912 - size
            size = size / (1024 * 1024)
            free = free / (1024 * 1024)
            size_text = f"{size:.1f} MB used / {free:.1f} MB free"
        except Exception:
            size_text = "n/a"
        return await msg.edit_text(
            f"📊 <b>Bot Stats</b>\n\n👥 Users: <b>{users}</b>\n💬 Chats: <b>{chats}</b>\n💾 DB: {size_text}",
            reply_markup=_back_kb(), parse_mode=enums.ParseMode.HTML)

    if data == "users":
        users = await db.total_users_count()
        return await msg.edit_text(f"👥 <b>Total users:</b> {users}", reply_markup=_back_kb(),
                                   parse_mode=enums.ParseMode.HTML)

    if data == "chats":
        chats = await db.total_chat_count()
        return await msg.edit_text(f"💬 <b>Total chats/groups:</b> {chats}", reply_markup=_back_kb(),
                                   parse_mode=enums.ParseMode.HTML)

    if data == "premium_users":
        return await _show_premium(msg)

    if data in ("bc_users", "bc_groups"):
        _PENDING[query.from_user.id] = data
        what = "all USERS" if data == "bc_users" else "all GROUPS"
        return await msg.edit_text(
            f"📢 <b>Broadcast to {what}.</b>\n\nNow SEND (or forward) the message you want broadcast. "
            f"Send /cancel to abort.",
            reply_markup=_back_kb(), parse_mode=enums.ParseMode.HTML)

    if data in ("ban", "unban"):
        _PENDING[query.from_user.id] = data
        action = "BAN" if data == "ban" else "UNBAN"
        return await msg.edit_text(
            f"🚫 <b>{action} a user.</b>\n\nSend the user's numeric Telegram ID now. /cancel to abort.",
            reply_markup=_back_kb(), parse_mode=enums.ParseMode.HTML)

    if data == "logs":
        try:
            await client.send_document(chat_id=query.from_user.id, document="TELEGRAM BOT.LOG",
                                       caption="📋 Bot log file")
            return await query.answer("Log sent to your PM", show_alert=False)
        except Exception as e:
            return await msg.edit_text(f"📋 Could not send log: <code>{e}</code>",
                                       reply_markup=_back_kb(), parse_mode=enums.ParseMode.HTML)

    if data == "settings":
        return await msg.edit_text(
            "⚙️ <b>Group settings</b> are per-chat.\n\nOpen <code>/settings</code> inside the group "
            "(or after <code>/connect</code> in PM) to toggle result mode, IMDb, spell-check, "
            "shortlink, auto-delete, etc.",
            reply_markup=_back_kb(), parse_mode=enums.ParseMode.HTML)

    if data == "index":
        return await msg.edit_text(
            "📺 <b>Index files</b> from a channel: make sure I'm admin there, then send "
            "<code>/index</code> (optionally <code>/setskip N</code> first).\n\n"
            "Indexing stores files so searches find them.",
            reply_markup=_back_kb(), parse_mode=enums.ParseMode.HTML)

    if data == "agent":
        from plugins.web_agent import ARIA2C
        return await msg.edit_text(
            f"🤖 <b>Auto web-fetch agent</b>\n\n"
            f"Enabled (AUTO_FETCH): <b>{'ON' if AUTO_FETCH else 'OFF'}</b>\n"
            f"aria2c installed: <b>{'YES' if ARIA2C else 'NO'}</b>\n\n"
            f"When ON and a file isn't in the DB, I search YTS/1337x, download the best match "
            f"with aria2, upload &amp; index it. Admin can force a grab with <code>/fetch &lt;name&gt;</code>.\n"
            f"<i>Set AUTO_FETCH=True and install aria2 to activate.</i>",
            reply_markup=_back_kb(), parse_mode=enums.ParseMode.HTML)

    if data == "convert":
        return await msg.edit_text(
            f"🎬 <b>Fast video convert mode</b>\n\n"
            f"Enabled (AUTO_CONVERT): <b>{'ON' if AUTO_CONVERT else 'OFF'}</b>\n"
            f"ffmpeg/ffprobe: <b>{'YES' if ffmpeg_available() else 'NO'}</b>\n\n"
            f"Fetched videos are remuxed MKV/AVI→MP4 (no slow re-encode) with a generated thumbnail "
            f"so they stream/play directly inside Telegram.",
            reply_markup=_back_kb(), parse_mode=enums.ParseMode.HTML)

    if data == "delall":
        return await msg.edit_text(
            "🗄 <b>WARNING:</b> this deletes ALL indexed files from the database.\n\nProceed?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚠️ YES, DELETE ALL", callback_data="adm#delall_yes"),
                InlineKeyboardButton("❌ No", callback_data="adm#home")]]),
            parse_mode=enums.ParseMode.HTML)

    if data == "delall_yes":
        try:
            from database.ia_filterdb import Media
            await Media.collection.drop()
            return await msg.edit_text("🗄 All indexed files deleted.", reply_markup=_back_kb())
        except Exception as e:
            return await msg.edit_text(f"Delete failed: <code>{e}</code>", reply_markup=_back_kb(),
                                       parse_mode=enums.ParseMode.HTML)

    if data == "restart":
        return await msg.edit_text(
            "🔄 <b>Restart the bot?</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Yes, restart", callback_data="adm#restart_yes"),
                InlineKeyboardButton("❌ No", callback_data="adm#home")]]),
            parse_mode=enums.ParseMode.HTML)

    if data == "restart_yes":
        await msg.edit_text("🔄 Restarting…")
        os.execl(sys.executable, sys.executable, *sys.argv)

    await query.answer()


async def _show_premium(msg):
    try:
        from datetime import datetime, timezone
        cur = datetime.now(timezone.utc)
        count = 0
        names = []
        async for u in db.col.find({"expiry_time": {"$gt": cur}}):
            count += 1
            if len(names) < 20:
                names.append(str(u.get("id")))
        text = f"💎 <b>Active premium users:</b> {count}"
        if names:
            text += "\n\n" + "\n".join(f"• <code>{n}</code>" for n in names)
    except Exception as e:
        text = f"💎 Could not load premium users: <code>{e}</code>"
    return await msg.edit_text(text, reply_markup=_back_kb(), parse_mode=enums.ParseMode.HTML)


# ---- Pending-input handler (broadcast / ban) -------------------------------
@Client.on_message(filters.private & filters.user(ADMINS) & filters.incoming & ~filters.command(["admin", "panel", "adminpanel"]))
async def admin_panel_input(client, message):
    uid = message.from_user.id if message.from_user else None
    action = _PENDING.get(uid)
    if not action:
        return  # not awaiting input from this admin -> let other handlers run

    if message.text and message.text.strip().lower() in ("/cancel", "cancel"):
        _PENDING.pop(uid, None)
        return await message.reply_text("Cancelled.", reply_markup=_main_menu())

    if action in ("bc_users", "bc_groups"):
        _PENDING.pop(uid, None)
        sts = await message.reply_text("📢 Broadcasting…")
        done = ok = failed = 0
        try:
            if action == "bc_users":
                targets = db.get_all_users()
                async for t in targets:
                    pti, _sh = await _safe_bc_user(int(t["id"]), message)
                    done += 1; ok += 1 if pti else 0; failed += 0 if pti else 1
                    await asyncio.sleep(0.2)
            else:
                targets = db.get_all_chats()
                async for t in targets:
                    pti = await _safe_bc_group(int(t["id"]), message)
                    done += 1; ok += 1 if pti else 0; failed += 0 if pti else 1
                    await asyncio.sleep(0.2)
        except Exception as e:
            return await sts.edit_text(f"Broadcast error: <code>{e}</code>",
                                       parse_mode=enums.ParseMode.HTML)
        return await sts.edit_text(
            f"✅ <b>Broadcast finished.</b>\n\nTargeted: {done}\nSuccess: {ok}\nFailed: {failed}",
            reply_markup=_main_menu(), parse_mode=enums.ParseMode.HTML)

    if action in ("ban", "unban"):
        _PENDING.pop(uid, None)
        raw = (message.text or "").strip()
        try:
            target_id = int(raw)
        except ValueError:
            return await message.reply_text("That isn't a numeric ID. Reopen the panel to retry.",
                                           reply_markup=_main_menu())
        try:
            if action == "ban":
                await db.ban_user(target_id)
                return await message.reply_text(f"🚫 Banned <code>{target_id}</code>.",
                                               reply_markup=_main_menu(), parse_mode=enums.ParseMode.HTML)
            else:
                await db.remove_ban(target_id)
                return await message.reply_text(f"✅ Unbanned <code>{target_id}</code>.",
                                               reply_markup=_main_menu(), parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            return await message.reply_text(f"Failed: <code>{e}</code>", reply_markup=_main_menu(),
                                           parse_mode=enums.ParseMode.HTML)


async def _safe_bc_user(user_id: int, message):
    try:
        await message.copy(chat_id=user_id)
        return True, "Ok"
    except Exception:
        return False, "Error"


async def _safe_bc_group(chat_id: int, message):
    try:
        await message.copy(chat_id=chat_id)
        return True
    except Exception:
        return False
