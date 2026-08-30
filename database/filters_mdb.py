import logging
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import enums
from info import DATABASE_URI, DATABASE_NAME
from util.button_parser import serialize_buttons

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)

# Async (motor) client — the previous sync pymongo client blocked the event loop
# on every DB call because these functions run inside async handlers.
myclient = AsyncIOMotorClient(DATABASE_URI)
mydb = myclient[DATABASE_NAME]


async def add_filter(grp_id, text, reply_text, btn, file, alert):
    mycol = mydb[str(grp_id)]

    data = {
        'text': str(text),
        'reply': str(reply_text),
        'btn': serialize_buttons(btn),
        'file': str(file),
        'alert': str(alert),
    }

    try:
        await mycol.update_one({'text': str(text)}, {"$set": data}, upsert=True)
    except Exception:
        logger.exception('Some error occured!', exc_info=True)


async def find_filter(group_id, name):
    mycol = mydb[str(group_id)]

    reply_text = btn = fileid = alert = None
    try:
        async for file in mycol.find({"text": name}):
            reply_text = file['reply']
            btn = file['btn']
            fileid = file['file']
            try:
                alert = file['alert']
            except KeyError:
                alert = None
        return reply_text, btn, alert, fileid
    except Exception:
        return None, None, None, None


async def get_filters(group_id):
    mycol = mydb[str(group_id)]

    texts = []
    try:
        async for file in mycol.find():
            texts.append(file['text'])
    except Exception:
        pass
    return texts


async def delete_filter(message, text, group_id):
    mycol = mydb[str(group_id)]

    myquery = {'text': text}
    count = await mycol.count_documents(myquery)
    if count == 1:
        await mycol.delete_one(myquery)
        await message.reply_text(
            f"'`{text}`'  deleted. I'll not respond to that filter anymore.",
            quote=True,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    else:
        await message.reply_text("Couldn't find that filter!", quote=True)


async def del_all(message, group_id, title):
    if str(group_id) not in await mydb.list_collection_names():
        await message.edit_text(f"Nothing to remove in {title}!")
        return

    mycol = mydb[str(group_id)]
    try:
        await mycol.drop()
        await message.edit_text(f"All filters from {title} has been removed")
    except Exception:
        await message.edit_text("Couldn't remove all filters from group!")
        return


async def count_filters(group_id):
    mycol = mydb[str(group_id)]

    count = await mycol.count_documents({})
    return False if count == 0 else count


async def filter_stats():
    collections = await mydb.list_collection_names()

    if "CONNECTION" in collections:
        collections.remove("CONNECTION")

    totalcount = 0
    for collection in collections:
        mycol = mydb[collection]
        totalcount += await mycol.count_documents({})

    totalcollections = len(collections)

    return totalcollections, totalcount
