# Load variables from a local .env file (KEY=VALUE) FIRST, before any module
# reads os.environ. This lets Windows users configure the bot in a plain .env
# with no batch-escaping of & / < / > characters. Safe if python-dotenv or the
# file is missing.
try:
    from dotenv import load_dotenv
    # override=True so the .env file always wins over stale 'set' variables
    # left in the Command Prompt session from an earlier run.
    load_dotenv(override=True)
except Exception:
    pass

# Normalise a few values that may have been pasted with Windows-batch
# escaping (^&) or accidental quotes; pymongo needs the raw URI.
import os as _os
for _k in ("DATABASE_URI",):
    _v = _os.environ.get(_k)
    if _v:
        _v = _v.strip().strip('"').strip("'").replace("^&", "&")
        _os.environ[_k] = _v

import sys
import glob
import importlib
from pathlib import Path
from pyrogram import idle
import logging
import logging.config

logging.config.fileConfig('logging.conf')
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("imdbpy").setLevel(logging.ERROR)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logging.getLogger("aiohttp").setLevel(logging.ERROR)
logging.getLogger("aiohttp.web").setLevel(logging.ERROR)

from pyrogram import Client, __version__
from pyrogram.raw.all import layer
from database.ia_filterdb import Media
from database.users_chats_db import db
from info import *
from utils import temp
from typing import Union, Optional, AsyncGenerator
from pyrogram import types
from Script import script
from datetime import date, datetime
import pytz
from aiohttp import web
from plugins import web_server

import asyncio
from pyrogram import idle
from lazybot import LazyPrincessBot
from util.keepalive import ping_server
from lazybot.clients import initialize_clients


ppath = "plugins/*.py"
files = glob.glob(ppath)
loop = asyncio.get_event_loop()


async def Lazy_start():
    print('\n')
    print('Initalizing The Movie Provider Bot')
    # The main client must be started (awaited) here. Calling LazyPrincessBot.start()
    # at module level without await only creates a coroutine that is never run, so the
    # client never connects, plugins never load, and get_me() raises ConnectionError.
    await LazyPrincessBot.start()
    bot_info = await LazyPrincessBot.get_me()
    LazyPrincessBot.username = bot_info.username
    await initialize_clients()
    # NOTE: plugins are auto-loaded by pyrogram at client start because the Client is
    # created with plugins={"root": "plugins"}. Manually importing them here would import
    # every module a second time and register every handler twice (double replies).
    # The import order below (reactions.py etc.) is handled automatically by pyrogram.
    print("Plugins auto-loaded via pyrogram plugin loader")
    if ON_HEROKU:
        asyncio.create_task(ping_server())
    b_users, b_chats = await db.get_banned()
    temp.BANNED_USERS = b_users
    temp.BANNED_CHATS = b_chats
    await Media.ensure_indexes()
    me = await LazyPrincessBot.get_me()
    temp.ME = me.id
    temp.U_NAME = me.username
    temp.B_NAME = me.first_name
    LazyPrincessBot.username = '@' + me.username
    logging.info(f"{me.first_name} with for Pyrogram v{__version__} (Layer {layer}) started on {me.username}.")
    logging.info(LOG_STR)
    logging.info(script.LOGO)
    tz = pytz.timezone('Asia/Kolkata')
    today = date.today()
    now = datetime.now(tz)
    time = now.strftime("%H:%M:%S %p")
    try:
        await LazyPrincessBot.get_chat(LOG_CHANNEL)
        await LazyPrincessBot.send_message(chat_id=LOG_CHANNEL, text=script.RESTART_TXT.format(today, time))
    except Exception as e:
        logging.warning(f"Could not send startup message to LOG_CHANNEL: {e}")
    app = web.AppRunner(await web_server())
    await app.setup()
    bind_address = "0.0.0.0"
    await web.TCPSite(app, bind_address, PORT).start()
    await idle()


if __name__ == '__main__':
    try:
        loop.run_until_complete(Lazy_start())
    except KeyboardInterrupt:
        logging.info('Service Stopped Bye 👋')