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
OWNER_ID = os.environ.get("OWNER")  # Set OWNER=your_telegram_id in environment

# ---------- Helpers ----------
def is_video(filename):
    mimetype, _ = mimetypes.guess_type(filename)
    return mimetype and mimetype.startswith("video")

def get_size(bytes_len: int) -> str:
    if bytes_len >= 1024 ** 3:
        return f"{bytes_len / 1024**3:.2f} GB"
    if bytes_len >= 1024 ** 2:
        return f"{bytes_len / 1024**2:.2f} MB"
    if bytes_len >= 1024:
        return f"{bytes_len / 1024:.2f} KB"
    return f"{bytes_len} bytes"

def find_between(text: str, start: str, end: str) -> str:
    try:
        return text.split(start, 1)[1].split(end, 1)[0]
    except Exception:
        return ""

# ---------- Config & DB ----------
mongo_client = MongoClient(DATABASE.URI)
db = mongo_client[DATABASE.NAME]

COOKIE = "ndus=Y2YqaCTteHuiU3Ud_MYU7vHoVW4DNBi0MPmg_1tQ"  # or load from env
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
        
    async def add_task(self, user_id: int, is_admin: bool, task_func, url: str):
        async with self.locks[user_id]:
            if not is_admin and len(self.queues[user_id]) >= 5:
                return False, "❌ Queue limit reached (max 5). Please wait."
            self.queues[user_id].append((task_func, url))
            return True, f"📥 Added to queue (Position: {len(self.queues[user_id])})"

    async def process_queue(self, client: Client, user_id: int, is_admin: bool, message: Message):
        if self.active_tasks[user_id] > 0:
            return

        self.active_tasks[user_id] += 1
        try:
            total_files = len(self.queues[user_id])
            current_pos = 0

            while self.queues[user_id]:
                current_pos += 1
                remaining_files = total_files - current_pos
                task_func, url = self.queues[user_id][0]

                # Get file info for display
                try:
                    info = await asyncio.to_thread(get_file_info_sync, url.strip())
                    size_info = info['size_str']
                    file_name = info['name']
                except Exception as e:
                    logger.error(f"Failed to get file info: {e}")
                    size_info = "Unknown size"
                    file_name = "Unknown file"

                # Send NEW start message for this queue item
                start_msg = await message.reply(
                    f"⬇️ Queue {current_pos}/{total_files}\n"
                    f"📦 Remaining: {remaining_files} file(s)\n"
                    f"📁 File: {file_name}\n"
                    f"💾 Size: {size_info}"
                )

                # Cooldown for non-admins
                if not is_admin:
                    elapsed = asyncio.get_event_loop().time() - self.last_download_time.get(user_id, 0)
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

                # Actually download/send the file
                await task_func()
                self.last_download_time[user_id] = asyncio.get_event_loop().time()
                self.queues[user_id].pop(0)

                # Delete the start message after file completes
                try:
                    await start_msg.delete()
                except:
                    pass

            await message.reply("✅ All downloads completed!")

        finally:
            self.active_tasks[user_id] -= 1

queue = DownloadQueue()

# ---------- TeraBox Helpers ----------
def get_file_info_sync(share_url: str) -> dict:
    try:
        resp = requests.get(share_url, headers=HEADERS, allow_redirects=True, timeout=30)
        final_url = resp.url
        parsed = urlparse(final_url)
        surl = parse_qs(parsed.query).get("surl", [None])[0]
        if not surl:
            raise ValueError("Invalid share URL")

        page = requests.get(final_url, headers=HEADERS, timeout=30)
        html = page.text
        js_token = find_between(html, 'fn%28%22', '%22%29')
        logid = find_between(html, 'dp-logid=', '&')
        bdstoken = find_between(html, 'bdstoken":"', '"')
        if not all([js_token, logid, bdstoken]):
            raise ValueError("Failed to extract tokens")

        params = {
            "app_id": "250528", "web": "1", "channel": "dubox",
            "clienttype": "0", "jsToken": js_token, "dp-logid": logid,
            "page": "1", "num": "20", "by": "name",
            "order": "asc", "site_referer": final_url,
            "shorturl": surl, "root": "1,"
        }
        info = requests.get(
            "https://www.terabox.app/share/list?" + urlencode(params),
            headers=HEADERS, timeout=30
        ).json()
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

def download_file_sync(url: str, dest_path: str):
    try:
        with requests.get(url, headers=DL_HEADERS, stream=True, timeout=60) as r:
            with open(dest_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
    except Exception as e:
        logger.error(f"Download failed: {e}")
        raise

# ---------- Background Deletion ----------
async def delete_later_task(sent_msg, file_path, delay=43200):
    try:
        await asyncio.sleep(delay)
        try:
            await sent_msg.delete()
        except:
            pass
    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except:
            pass

# ---------- Admin Check ----------
def is_admin(user_id: int) -> bool:
    return OWNER_ID and str(user_id) == str(OWNER_ID)

# ---------- Process Single Link ----------
async def process_single_link(client: Client, message: Message, url: str):
    try:
        info = await asyncio.to_thread(get_file_info_sync, url.strip())
    except Exception as e:
        await message.reply(f"❌ Failed to get file info:\n{e}")
        return

    safe_name = f"{uuid.uuid4().hex}_{info['name']}"
    temp_path = os.path.join(tempfile.gettempdir(), safe_name)

    try:
        await asyncio.to_thread(download_file_sync, info["download_link"], temp_path)
    except Exception as e:
        await message.reply(f"❌ Download failed:\n{e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return

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
            sent_msg = await client.send_video(message.chat.id, temp_path, caption=caption, file_name=info["name"], protect_content=True)
        else:
            sent_msg = await client.send_document(message.chat.id, temp_path, caption=caption, file_name=info["name"], protect_content=True)

        asyncio.create_task(delete_later_task(sent_msg, temp_path))
    except Exception as e:
        await message.reply(f"❌ Upload failed:\n{e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)

# ---------- Handler ----------
@Client.on_message(filters.private)
async def handle_terabox(client: Client, message: Message):
    user_id = message.from_user.id
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.reply("❌ Please send TeraBox links.")
        return

    matches = re.findall(TERABOX_REGEX, text)
    if not matches:
        await message.reply("❌ No valid TeraBox links found.")
        return

    if IS_VERIFY and not await is_verified(user_id):
        verify_url = await build_verification_link(client.me.username, user_id)
        buttons = [[InlineKeyboardButton("✅ Verify Now", url=verify_url),
                    InlineKeyboardButton("📖 Tutorial", url=HOW_TO_VERIFY)]]
        await message.reply("🔐 You must verify before using this bot.", reply_markup=InlineKeyboardMarkup(buttons))
        return

    admin_status = is_admin(user_id)
    for url in matches:
        async def create_task(url=url):
            await process_single_link(client, message, url)
        success, reply = await queue.add_task(user_id, admin_status, create_task, url)
        await message.reply(reply)

    asyncio.create_task(queue.process_queue(client, user_id, admin_status, message))
                                       
