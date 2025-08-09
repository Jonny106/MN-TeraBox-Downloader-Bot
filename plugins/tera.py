import os
import re
import uuid
import tempfile
import requests
import asyncio
import shutil
import mimetypes
import logging
from collections import defaultdict
from urllib.parse import urlencode, urlparse, parse_qs
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from verify_patch import IS_VERIFY, is_verified, build_verification_link, HOW_TO_VERIFY
from pymongo import MongoClient
from config import CHANNEL, DATABASE

# ---------- Global Constants ----------
TERABOX_REGEX = r'https?://(?:www\.)?[^/\s]*tera[^/\s]*\.[a-z]+/s/[^\s]+'

# ---------- Logger ----------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- Environment Config ----------
OWNER_ID = os.environ.get("OWNER")  # OWNER=your_telegram_id

# ---------- Helpers ----------
def is_video(filename):
    mtype, _ = mimetypes.guess_type(filename)
    return mtype and mtype.startswith("video")

def get_size(b):
    if b >= 1024 ** 3:
        return f"{b / 1024**3:.2f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024**2:.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.2f} KB"
    return f"{b} bytes"

def find_between(text: str, start: str, end: str):
    try:
        return text.split(start, 1)[1].split(end, 1)[0]
    except:
        return ""

# ---------- Config & DB ----------
mongo_client = MongoClient(DATABASE.URI)
db = mongo_client[DATABASE.NAME]

COOKIE = "PUT-YOUR-COOKIE-HERE"  # <-- Update this
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cookie": COOKIE
}
DL_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Cookie": COOKIE
}

# ---------- Queue Management ----------
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
                    logger.error(f"Failed to get file info: {e}")
                    size_info = "Unknown size"
                    file_name = "Unknown file"

                start_msg = await message.reply(
                    f"⬇️ Queue {current_pos}/{total_files}\n"
                    f"📦 Remaining: {remaining} file(s)\n"
                    f"📁 File: {file_name}\n"
                    f"💾 Size: {size_info}"
                )

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

                await func()
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

# ---------- Fixed Terabox Info Fetch ----------
def get_file_info_sync(share_url: str) -> dict:
    try:
        resp = requests.get(share_url, headers=HEADERS, allow_redirects=True, timeout=30)
        if resp.status_code != 200:
            raise ValueError(f"Failed to fetch share page (HTTP {resp.status_code})")
        final_url = resp.url
        parsed = urlparse(final_url)
        surl = parse_qs(parsed.query).get("surl", [None])[0]
        if not surl:
            raise ValueError("Invalid share URL (missing surl parameter)")

        page = requests.get(final_url, headers=HEADERS, timeout=30)
        html = page.text
        js_token = find_between(html, 'fn%28%22', '%22%29')
        logid = find_between(html, 'dp-logid=', '&')
        bdstoken = find_between(html, 'bdstoken":"', '"')
        if not all([js_token, logid, bdstoken]):
            raise ValueError("Failed to extract authentication tokens — HTML changed or cookie invalid.")

        params = {
            "app_id": "250528", "web": "1", "channel": "dubox",
            "clienttype": "0", "jsToken": js_token, "dp-logid": logid,
            "page": "1", "num": "20", "by": "name", "order": "asc",
            "site_referer": final_url, "shorturl": surl, "root": "1,"
        }
        api_url = "https://www.terabox.app/share/list?" + urlencode(params)
        info = requests.get(api_url, headers=HEADERS, timeout=30).json()

        if "list" not in info or not info["list"]:
            import json
            logger.error(f"Terabox API response (no 'list'): {json.dumps(info, indent=2)}")
            raise ValueError(f"API did not return file list. Reason: {info.get('errmsg', 'Unknown')}")

        file = info["list"][0]
        size_bytes = int(file.get("size", 0))
        return {
            "name": file.get("server_filename", "download"),
            "download_link": file.get("dlink", ""),
            "size_bytes": size_bytes,
            "size_str": get_size(size_bytes)
        }
    except Exception as e:
        logger.error(f"Error in get_file_info_sync: {e}")
        raise

# ---------- Download ----------
def download_file_sync(url: str
        
