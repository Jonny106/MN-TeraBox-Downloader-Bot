# upgraded_bot.py
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
from collections import defaultdict, deque
from urllib.parse import urlencode, urlparse, parse_qs
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from verify_patch import IS_VERIFY, is_verified, build_verification_link, HOW_TO_VERIFY
from pymongo import MongoClient
from config import CHANNEL, DATABASE

# ---------- Config ----------
TERABOX_REGEX = r'https?://(?:www\.)?[^/\s]*tera[^/\s]*\.[a-z]+/s/[^\s]+'
logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
OWNER_ID = os.environ.get("OWNER")
COOKIE = os.environ.get("COOKIE", "ndus=example_cookie_here")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cookie": COOKIE
}
DL_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cookie": COOKIE
}

mongo_client = MongoClient(DATABASE.URI)
db = mongo_client[DATABASE.NAME]

# ---------- Helpers ----------
def is_video(filename):
    mtype, _ = mimetypes.guess_type(filename)
    return mtype and mtype.startswith("video")

def get_size(b):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024:
            return f"{b:.2f} {unit}"
        b /= 1024
    return f"{b:.2f} PB"

def progress_bar(percent, length=20):
    filled = int(length * percent / 100)
    return "█" * filled + "░" * (length - filled)

# ---------- Terabox API ----------
def get_file_info_sync(share_url):
    r = requests.get(share_url, headers=HEADERS, timeout=30)
    final_url = r.url
    parsed = urlparse(final_url)
    surl = parse_qs(parsed.query).get("surl", [None])[0]
    if not surl:
        raise ValueError("Invalid TeraBox link")

    html = r.text
    js_token = html.split('fn%28%22')[1].split('%22%29')[0]
    logid = html.split('dp-logid=')[1].split('&')[0]
    bdstoken = html.split('bdstoken":"')[1].split('"')[0]
    params = {
        "app_id": "250528", "web": "1", "channel": "dubox",
        "clienttype": "0", "jsToken": js_token, "dp-logid": logid,
        "site_referer": final_url, "shorturl": surl, "root": "1,"
    }
    info = requests.get("https://www.terabox.app/share/list?" + urlencode(params),
                        headers=HEADERS, timeout=30).json()
    file = info["list"][0]
    size_bytes = int(file.get("size", 0))
    return {
        "name": file.get("server_filename", "download"),
        "download_link": file.get("dlink", ""),
        "size_bytes": size_bytes,
        "size_str": get_size(size_bytes)
    }

def download_file_sync(url, dest, progress_callback=None):
    start_time = time.time()
    with requests.get(url, headers=DL_HEADERS, stream=True, timeout=60) as r:
        total = int(r.headers.get('content-length', 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        elapsed = time.time() - start_time
                        speed = downloaded / (elapsed + 0.001)
                        progress_callback(downloaded, total, speed)

# ---------- Queue ----------
class DownloadQueue:
    def __init__(self, max_per_user=1, max_global=5):
        self.user_queues = defaultdict(deque)
        self.user_locks = defaultdict(asyncio.Lock)
        self.status_msgs = {}
        self.max_per_user = max_per_user
        self.global_semaphore = asyncio.Semaphore(max_global)

    async def add(self, uid, task):
        self.user_queues[uid].append(task)
        return len(self.user_queues[uid])

    async def run_queue(self, client, uid):
        async with self.user_locks[uid]:
            while self.user_queues[uid]:
                async with self.global_semaphore:
                    task = self.user_queues[uid].popleft()
                    await task()

queue = DownloadQueue()

# ---------- Main Download Logic ----------
async def process_single_link(client, message, info, idx, total):
    uid = message.from_user.id
    temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex}_{info['name']}")

    async def update_progress(cur, total_bytes, speed):
        percent = int(cur * 100 / total_bytes) if total_bytes else 0
        bar = progress_bar(percent)
        speed_str = get_size(speed) + "/s"
        await safe_edit(client, uid, 
            f"⬇️ [{idx}/{total}] {info['name']}\n"
            f"{bar} {percent}%\n"
            f"💾 {get_size(cur)} / {info['size_str']}\n⚡ {speed_str}"
        )

    await safe_edit(client, uid, f"📥 Starting download {idx}/{total}: {info['name']} ({info['size_str']})")
    await asyncio.to_thread(download_file_sync, info["download_link"], temp_path,
                            progress_callback=lambda c, t, s: asyncio.run_coroutine_threadsafe(
                                update_progress(c, t, s), asyncio.get_event_loop()))
    
    await safe_edit(client, uid, f"⬆️ Uploading {info['name']} ({info['size_str']})...")

    async def upload_progress(cur, total_bytes):
        percent = int(cur * 100 / total_bytes) if total_bytes else 0
        await safe_edit(client, uid, f"⬆️ Uploading {info['name']} - {percent}%")

    try:
        if is_video(info["name"]):
            await client.send_video(message.chat.id, temp_path, caption=f"{info['name']}\n💾 {info['size_str']}",
                                    file_name=info["name"], protect_content=True, progress=upload_progress)
        else:
            await client.send_document(message.chat.id, temp_path, caption=f"{info['name']}\n💾 {info['size_str']}",
                                       file_name=info["name"], protect_content=True, progress=upload_progress)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

async def safe_edit(client, uid, text):
    try:
        if uid in queue.status_msgs:
            await queue.status_msgs[uid].edit_text(text)
    except Exception:
        pass

@Client.on_message(filters.private)
async def handle_links(client, message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    links = re.findall(TERABOX_REGEX, text)
    if not links:
        await message.reply("❌ No valid TeraBox links found.")
        return
    if IS_VERIFY and not await is_verified(uid):
        verify_url = await build_verification_link(client.me.username, uid)
        buttons = [[InlineKeyboardButton("✅ Verify Now", url=verify_url), InlineKeyboardButton("📖 Tutorial", url=HOW_TO_VERIFY)]]
        await message.reply("🔐 Please verify first.", reply_markup=InlineKeyboardMarkup(buttons))
        return
    infos = []
    total_size = 0
    for l in links:
        try:
            info = await asyncio.to_thread(get_file_info_sync, l)
            infos.append(info)
            total_size += info['size_bytes']
        except Exception as e:
            await message.reply(f"❌ Failed to get info for {l}\n{e}")
    queue.status_msgs[uid] = await message.reply(
        f"📜 Batch Summary:\nFiles: {len(infos)}\nTotal Size: {get_size(total_size)}"
    )
    for i, info in enumerate(infos, start=1):
        async def task(info=info, i=i):
            await process_single_link(client, message, info, i, len(infos))
        await queue.add(uid, task)
    asyncio.create_task(queue.run_queue(client, uid))
    
