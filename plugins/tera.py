# please give credits https://github.com/MN-BOTS
#  @MrMNTG @MusammilN
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
TERABOX_REGEX = r'https?://(?:www\.)?(terabox\.app|terabox\.com)/s/[a-zA-Z0-9_-]+'

# ---------- Logger Setup ----------
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

COOKIE = "ndus=Y2YqaCTteHuiU3Ud_MYU7vHoVW4DNBi0MPmg_1tQ"  # keep or use your env
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Connection": "keep-alive",
    "DNT": "1",
    "Host": "www.terabox.app",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
    "Cookie": COOKIE,
}
DL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Cookie": COOKIE,
}

# ---------- Queue Management System ----------
class DownloadQueue:
    def __init__(self):
        self.queues = defaultdict(list)
        self.active_tasks = defaultdict(int)
        self.last_download_time = defaultdict(float)
        self.locks = defaultdict(asyncio.Lock)
        self.status_messages = defaultdict(dict)
        
    async def add_task(self, user_id: int, is_admin: bool, task_func, url: str):
        async with self.locks[user_id]:
            if not is_admin and len(self.queues[user_id]) >= 5:
                return False, "❌ Queue limit reached (max 5). Please wait for current downloads to finish."
            
            position = len(self.queues[user_id]) + 1
            self.queues[user_id].append((task_func, url))
            return True, f"📥 Added to queue (Position: {position})"

    async def update_status(self, client: Client, user_id: int, message: str):
        if user_id in self.status_messages:
            try:
                await self.status_messages[user_id].edit_text(message)
            except Exception as e:
                logger.error(f"Failed to update status: {e}")

    async def process_queue(self, client: Client, user_id: int, is_admin: bool, message: Message):
        if self.active_tasks[user_id] > 0:
            return
            
        self.active_tasks[user_id] += 1
        try:
            total_files = len(self.queues[user_id])
            current_pos = 0
            
            while self.queues[user_id]:
                current_pos += 1
                remaining_files = len(self.queues[user_id]) - 1
                task_func, url = self.queues[user_id][0]
                
                try:
                    info = await asyncio.to_thread(get_file_info_sync, url.strip())
                    size_info = info['size_str']
                except Exception as e:
                    logger.error(f"Failed to get file info: {e}")
                    info = None
                    size_info = "Unknown size"

                # Update status before download
                status_msg = (
                    f"⬇️ Downloading {current_pos}/{total_files}\n"
                    f"📦 Remaining: {remaining_files} file(s)\n"
                    f"💾 Size: {size_info}"
                )
                await self.update_status(client, user_id, status_msg)
                
                # Apply delay for non-admins
                if not is_admin:
                    elapsed = asyncio.get_event_loop().time() - self.last_download_time.get(user_id, 0)
                    if elapsed < 30:
                        delay = 30 - elapsed
                        for remaining in range(int(delay), 0, -1):
                            await self.update_status(
                                client, 
                                user_id,
                                f"⏳ Cool down: {remaining}s/30s\n"
                                f"⬇️ Next: Queue {current_pos}/{total_files}\n"
                                f"💾 Size: {size_info}"
                            )
                            await asyncio.sleep(1)
                
                await task_func()
                self.last_download_time[user_id] = asyncio.get_event_loop().time()
                self.queues[user_id].pop(0)
                
                # Update status after download
                if self.queues[user_id]:
                    await self.update_status(
                        client,
                        user_id,
                        f"✅ Completed Queue {current_pos}/{total_files}\n"
                        f"📦 Remaining: {remaining_files} file(s)"
                    )
                    await asyncio.sleep(1)
                else:
                    await self.update_status(client, user_id, "✅ All downloads completed!")
                    try:
                        await self.status_messages[user_id].delete()
                    except Exception as e:
                        logger.error(f"Failed to delete status message: {e}")
        finally:
            self.active_tasks[user_id] -= 1
            if user_id in self.status_messages:
                try:
                    await self.status_messages[user_id].delete()
                except Exception as e:
                    logger.error(f"Failed to clean up status message: {e}")
                del self.status_messages[user_id]

queue = DownloadQueue()

# ---------- Core terabox helpers ----------
def get_file_info_sync(share_url: str) -> dict:
    try:
        resp = requests.get(share_url, headers=HEADERS, allow_redirects=True, timeout=30)
        resp.raise_for_status()
        final_url = resp.url

        parsed = urlparse(final_url)
        surl = parse_qs(parsed.query).get("surl", [None])[0]
        if not surl:
            raise ValueError("Invalid share URL (missing surl)")

        page = requests.get(final_url, headers=HEADERS, timeout=30)
        html = page.text

        js_token = find_between(html, 'fn%28%22', '%22%29')
        logid = find_between(html, 'dp-logid=', '&')
        bdstoken = find_between(html, 'bdstoken":"', '"')
        if not all([js_token, logid, bdstoken]):
            raise ValueError("Failed to extract authentication tokens")

        params = {
            "app_id": "250528", "web": "1", "channel": "dubox",
            "clienttype": "0", "jsToken": js_token, "dp-logid": logid,
            "page": "1", "num": "20", "by": "name", "order": "asc",
            "site_referer": final_url, "shorturl": surl, "root": "1,",
        }
        info = requests.get(
            "https://www.terabox.app/share/list?" + urlencode(params),
            headers=HEADERS, timeout=30
        ).json()

        if info.get("errno") or not info.get("list"):
            errmsg = info.get("errmsg", "Unknown error")
            raise ValueError(f"List API error: {errmsg}")

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
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
    except Exception as e:
        logger.error(f"Download failed: {e}")
        raise

# ---------- background deletion ----------
async def delete_later_task(sent_msg, file_path, delay=43200):
    try:
        await asyncio.sleep(delay)
        try:
            await sent_msg.delete()
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

# ---------- admin check ----------
def is_admin(user_id: int) -> bool:
    """Check if user is owner"""
    return OWNER_ID and str(user_id) == str(OWNER_ID)

# ---------- message handler ----------
async def process_single_link(client: Client, message: Message, url: str):
    try:
        info = await asyncio.to_thread(get_file_info_sync, url.strip())
    except Exception as e:
        await message.reply(f"❌ Failed to get file info:\n{e}")
        logger.error(f"Failed to process {url}: {e}")
        return

    safe_name = f"{uuid.uuid4().hex}_{info['name']}"
    temp_path = os.path.join(tempfile.gettempdir(), safe_name)

    try:
        await asyncio.to_thread(download_file_sync, info["download_link"], temp_path)
    except Exception as e:
        await message.reply(f"❌ Download failed:\n`{e}`")
        logger.error(f"Download failed for {url}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return

    # Simplified caption without link
    caption = f"📁 {info['name']}\n💾 Size: {info['size_str']}"

    try:
        if getattr(CHANNEL, "ID", None):
            try:
                if is_video(info["name"]):
                    await client.send_video(chat_id=CHANNEL.ID, video=temp_path, caption=caption, file_name=info["name"])
                else:
                    await client.send_document(chat_id=CHANNEL.ID, document=temp_path, caption=caption, file_name=info["name"])
            except Exception as e:
                logger.error(f"Failed to forward to channel: {e}")

        if is_video(info["name"]):
            sent_msg = await client.send_video(
                chat_id=message.chat.id,
                video=temp_path,
                caption=caption,
                file_name=info["name"],
                protect_content=True
            )
        else:
            sent_msg = await client.send_document(
                chat_id=message.chat.id,
                document=temp_path,
                caption=caption,
                file_name=info["name"],
                protect_content=True
            )

        asyncio.create_task(delete_later_task(sent_msg, temp_path))
    except Exception as e:
        await message.reply(f"❌ Upload failed:\n`{e}`")
        logger.error(f"Upload failed for {url}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

@Client.on_message(filters.private)
async def handle_terabox(client: Client, message: Message):
    user_id = message.from_user.id
    text = (message.text or message.caption or "").strip()
    
    if not text:
        await message.reply("❌ Please send a message containing one or more TeraBox links.")
        return

    try:
        matches = re.findall(TERABOX_REGEX, text)
    except Exception as e:
        logger.error(f"Regex matching failed: {e}")
        await message.reply("❌ Error processing your links. Please try again.")
        return

    if not matches:
        await message.reply("❌ No valid TeraBox links found in your message.")
        return

    if IS_VERIFY and not await is_verified(user_id):
        verify_url = await build_verification_link(client.me.username, user_id)
        buttons = [
            [
                InlineKeyboardButton("✅ Verify Now", url=verify_url),
                InlineKeyboardButton("📖 Tutorial", url=HOW_TO_VERIFY)
            ]
        ]
        await message.reply_text(
            "🔐 You must verify before using this command.\n\n⏳ Verification lasts for 12 hours.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    admin_status = is_admin(user_id)
    
    for url in matches:
        async def create_task(url=url):
            await process_single_link(client, message, url)
        
        success, reply = await queue.add_task(user_id, admin_status, create_task, url)
        await message.reply(reply)
        
    # Start processing if not already running
    asyncio.create_task(queue.process_queue(client, user_id, admin_status, message))
