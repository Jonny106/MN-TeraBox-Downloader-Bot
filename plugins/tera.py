import os
import re
import uuid
import tempfile
import requests
import asyncio
import shutil
import mimetypes
import logging
import time
from collections import defaultdict
from urllib.parse import urlencode, urlparse, parse_qs
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from verify_patch import IS_VERIFY, is_verified, build_verification_link, HOW_TO_VERIFY
from pymongo import MongoClient
from config import CHANNEL, DATABASE

TERABOX_REGEX = r'https?://(?:www\.)?[^/\s]*tera[^/\s]*\.[a-z]+/s/[^\s]+'

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
OWNER_ID = os.environ.get("OWNER")

def is_video(filename):
    mtype, _ = mimetypes.guess_type(filename)
    return mtype and mtype.startswith("video")

def get_size(b):
    for unit in ['B','KB','MB','GB','TB']:
        if b < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} PB"

def progress_bar(percent, length=12):
    filled = int(length * percent / 100)
    return "█" * filled + "░" * (length - filled)

def find_between(text, start, end):
    try:
        return text.split(start, 1)[1].split(end, 1)[0]
    except:
        return ""

mongo_client = MongoClient(DATABASE.URI)
db = mongo_client[DATABASE.NAME]

COOKIE = "ndus=Y2YqaCTteHuiU3Ud_MYU7vHoVW4DNBi0MPmg_1tQ"  # keep or use your env
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cookie": COOKIE
}
DL_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cookie": COOKIE
}

class DownloadQueue:
    def __init__(self):
        self.queues = defaultdict(list)
        self.active_tasks = defaultdict(int)
        self.last_download_time = defaultdict(float)
        self.locks = defaultdict(asyncio.Lock)

    async def add_task(self, uid, is_admin, func, url):
        async with self.locks[uid]:
            if not is_admin and len(self.queues[uid]) >= 5:
                return False, "❌ Queue limit reached (max 5). Wait for current downloads."
            self.queues[uid].append((func, url))
            return True, f"📥 Added to queue (Position: {len(self.queues[uid])})"

    async def process_queue(self, client, uid, is_admin, message):
        if self.active_tasks[uid] > 0:
            return
        self.active_tasks[uid] += 1
        try:
            total_files = len(self.queues[uid])
            current_pos = 0
            while self.queues[uid]:
                current_pos += 1
                remaining = total_files - current_pos
                func, url = self.queues[uid][0]

                try:
                    info = await asyncio.to_thread(get_file_info_sync, url.strip())
                    size_info = info['size_str']
                    file_name = info['name']
                except Exception as e:
                    size_info = "Unknown size"
                    file_name = "Unknown file"
                    logger.error(e)

                # Send start message (will be updated with progress)
                start_msg = await message.reply(
                    f"⬇️ Queue {current_pos}/{total_files}\n"
                    f"📦 Remaining: {remaining} file(s)\n"
                    f"📁 File: {file_name}\n"
                    f"💾 Size: {size_info}"
                )

                # Cooldown for non-admins
                if not is_admin:
                    elapsed = asyncio.get_event_loop().time() - self.last_download_time.get(uid, 0)
                    if elapsed < 30:
                        delay = 30 - elapsed
                        for sec in range(int(delay), 0, -1):
                            try:
                                await start_msg.edit_text(
                                    f"⏳ Cool down: {sec}s/30s\n"
                                    f"⬇️ Next: {current_pos}/{total_files}\n"
                                    f"📁 File: {file_name}\n"
                                    f"💾 Size: {size_info}"
                                )
                            except:
                                pass
                            await asyncio.sleep(1)

                # Note: Now pass start_msg to the download task
                await func(start_msg)  # Download/send logic gets start_msg for progress

                self.last_download_time[uid] = asyncio.get_event_loop().time()
                self.queues[uid].pop(0)

                try:
                    await start_msg.delete()
                except:
                    pass

            await message.reply("✅ All downloads completed!")
        finally:
            self.active_tasks[uid] -= 1

queue = DownloadQueue()

def get_file_info_sync(share_url: str) -> dict:
    resp = requests.get(share_url, headers=HEADERS, allow_redirects=True, timeout=30)
    resp.raise_for_status()
    final_url = resp.url
    parsed = urlparse(final_url)
    surl = parse_qs(parsed.query).get("surl", [None])[0]
    if not surl:
        raise ValueError("Invalid share URL (missing surl)")

    html = requests.get(final_url, headers=HEADERS, timeout=30).text
    js_token = find_between(html, 'fn%28%22', '%22%29')
    logid = find_between(html, 'dp-logid=', '&')
    bdstoken = find_between(html, 'bdstoken":"', '"')
    if not all([js_token, logid, bdstoken]):
        raise ValueError("Failed to extract authentication tokens — bad cookie or HTML changed.")

    params = {
        "app_id": "250528", "web": "1", "channel": "dubox",
        "clienttype": "0", "jsToken": js_token, "dp-logid": logid,
        "page": "1", "num": "20", "by": "name", "order": "asc",
        "site_referer": final_url, "shorturl": surl, "root": "1,"
    }
    info = requests.get(
        "https://www.terabox.app/share/list?" + urlencode(params),
        headers=HEADERS, timeout=30
    ).json()

    if "list" not in info or not info["list"]:
        raise ValueError(f"API did not return file list: {info}")

    file = info["list"][0]
    size_bytes = int(file.get("size", 0))
    return {
        "name": file.get("server_filename", "download"),
        "download_link": file.get("dlink", ""),
        "size_bytes": size_bytes,
        "size_str": get_size(size_bytes)
    }

def download_file_sync(url: str, dest_path: str, progress_cb=None):
    start_t = time.time()
    with requests.get(url, headers=DL_HEADERS, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get('content-length', 0))
        downloaded = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        speed = downloaded / (time.time() - start_t + 0.1)
                        progress_cb(downloaded, total, speed)

async def delete_later_task(msg, path, delay=43200):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass
    if os.path.exists(path):
        try: os.remove(path)
        except: pass

def is_admin(uid: int) -> bool:
    return OWNER_ID and str(uid) == str(OWNER_ID)

async def process_single_link(client: Client, message: Message, url: str, start_msg: Message):
    try:
        info = await asyncio.to_thread(get_file_info_sync, url.strip())
    except Exception as e:
        await start_msg.edit_text(f"❌ Failed to get file info:\n{e}")
        return

    temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex}_{info['name']}")
    last_update = 0

    def dl_progress(cur, total, speed):
        nonlocal last_update
        if time.time() - last_update >= 2:  # throttle updates to every 2s
            last_update = time.time()
            percent = int(cur * 100 / total) if total else 0
            bar = progress_bar(percent)
            asyncio.run_coroutine_threadsafe(
                start_msg.edit_text(
                    f"⬇️ Downloading: {info['name']}\n"
                    f"{bar} {percent}% — {get_size(speed)}/s\n"
                    f"💾 {get_size(cur)} / {info['size_str']}"
                ),
                asyncio.get_event_loop()
            )

    try:
        await asyncio.to_thread(download_file_sync, info["download_link"], temp_path, dl_progress)
    except Exception as e:
        await start_msg.edit_text(f"❌ Download failed:\n{e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return

    async def ul_progress(cur, total):
        nonlocal last_update
        if time.time() - last_update >= 2:
            last_update = time.time()
            percent = int(cur * 100 / total) if total else 0
            bar = progress_bar(percent)
            try:
                await start_msg.edit_text(
                    f"⬆️ Uploading: {info['name']}\n"
                    f"{bar} {percent}%\n"
                    f"💾 {get_size(cur)} / {info['size_str']}"
                )
            except:
                pass

    caption = f"📁 {info['name']}\n💾 Size: {info['size_str']}"
    try:
        if getattr(CHANNEL, "ID", None):
            try:
                if is_video(info["name"]):
                    await client.send_video(CHANNEL.ID, temp_path, caption=caption, file_name=info["name"])
                else:
                    await client.send_document(CHANNEL.ID, temp_path, caption=caption, file_name=info["name"])
            except:
                pass

        if is_video(info["name"]):
            sent_msg = await client.send_video(
                message.chat.id, temp_path, caption=caption, file_name=info["name"],
                protect_content=True, progress=ul_progress
            )
        else:
            sent_msg = await client.send_document(
                message.chat.id, temp_path, caption=caption, file_name=info["name"],
                protect_content=True, progress=ul_progress
            )

        asyncio.create_task(delete_later_task(sent_msg, temp_path))

    except Exception as e:
        await start_msg.edit_text(f"❌ Upload failed:\n{e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)

@Client.on_message(filters.private)
async def handle_terabox(client: Client, message: Message):
    uid = message.from_user.id
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.reply("❌ Please send a TeraBox link.")
        return

    matches = re.findall(TERABOX_REGEX, text)
    if not matches:
        await message.reply("❌ No valid TeraBox links found.")
        return

    if IS_VERIFY and not await is_verified(uid):
        verify_url = await build_verification_link(client.me.username, uid)
        buttons = [
            [InlineKeyboardButton("✅ Verify Now", url=verify_url),
             InlineKeyboardButton("📖 Tutorial", url=HOW_TO_VERIFY)]
        ]
        await message.reply_text(
            "🔐 You must verify before using this command.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    admin_status = is_admin(uid)
    for url in matches:
        async def create_task(start_msg=None, url=url):
            await process_single_link(client, message, url, start_msg)
        success, reply = await queue.add_task(uid, admin_status, create_task, url)
        await message.reply(reply)

    asyncio.create_task(queue.process_queue(client, uid, admin_status, message))
    
