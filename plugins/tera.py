# please give credits https://github.com/MN-BOTS
#  @MrMNTG @MusammilN

import os
import re
import uuid
import tempfile
import asyncio
import shutil
import mimetypes
import logging
import time
from collections import defaultdict
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp
import requests  # used for some quick json endpoints if needed
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from config import CHANNEL, DATABASE

# ---------- Global Constants ----------
TERABOX_REGEX = r'https?://(?:www\.)?[^/\s]*tera[^/\s]*\.[a-z]+/s/[^\s]+'

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
        # each user_id -> list of urls
        self.queues = defaultdict(list)
        # active workers per user (0 or 1)
        self.active_tasks = defaultdict(int)
        # last finished timestamp per user, used to enforce 30s gap
        self.last_download_time = defaultdict(float)
        # per-user locks for queue operations
        self.locks = defaultdict(lambda: asyncio.Lock())
        # messages shown to user for status
        self.status_messages = defaultdict(list)
        # cancellation flag per user
        self.cancelled = defaultdict(bool)

    async def add_task(self, user_id: int, is_admin: bool, url: str):
        async with self.locks[user_id]:
            if (not is_admin) and len(self.queues[user_id]) >= 5:
                return False, "❌ Queue limit reached (max 5). Please wait for current downloads to finish."
            pos = len(self.queues[user_id]) + 1
            self.queues[user_id].append(url)
            return True, f"📥 Added to queue (Position: {pos})"

    async def send_status(self, client: Client, chat_id: int, text: str, with_cancel: bool = True):
        buttons = None
        if with_cancel:
            # callback includes the user id so handler can verify
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancel Queue", callback_data=f"cancel_q")]])
            buttons = kb
        try:
            msg = await client.send_message(chat_id, text, reply_markup=buttons)
            return msg
        except Exception as e:
            logger.error(f"Failed to send status message: {e}")
            return None

    async def cleanup_status(self, client: Client, chat_id: int):
        msgs = list(self.status_messages.get(chat_id, []))
        for msg in msgs:
            try:
                await msg.delete()
            except Exception:
                pass
        self.status_messages[chat_id] = []

    def cancel_queue(self, user_id: int):
        self.cancelled[user_id] = True

    def clear_cancel(self, user_id: int):
        self.cancelled[user_id] = False

    async def process_queue(self, client: Client, user_id: int, is_admin: bool, trigger_message: Message):
        # avoid starting multiple workers
        if self.active_tasks[user_id] > 0:
            return

        self.active_tasks[user_id] += 1
        try:
            while self.queues[user_id]:
                if self.cancelled.get(user_id, False):
                    # clear queue and inform user
                    self.queues[user_id].clear()
                    await trigger_message.reply("❌ Your queue was cancelled.")
                    self.cancelled[user_id] = False
                    await self.cleanup_status(client, user_id)
                    break

                url = self.queues[user_id][0]
                # enforce delay for non-admins (30s between tasks)
                if not is_admin:
                    elapsed = time.time() - self.last_download_time.get(user_id, 0)
                    if elapsed < 30:
                        wait = 30 - elapsed
                        await trigger_message.reply(f"⏳ Waiting {int(wait)}s before starting next task...")
                        await asyncio.sleep(wait)

                # create a status message (we keep one active status per step)
                info = None
                try:
                    info = await asyncio.to_thread(get_file_info_sync, url.strip())
                except Exception as e:
                    logger.error(f"Failed to fetch file info: {e}")
                    await trigger_message.reply(f"❌ Failed to get file info for:\n{url}\n`{e}`")
                    # remove and continue
                    self.queues[user_id].pop(0)
                    continue

                total_files = len(self.queues[user_id])
                position = 1

                status_text = (
                    f"⬇️ Starting download ({info['name']})\n"
                    f"📦 Position: {position}/{total_files}\n"
                    f"💾 Size: {info['size_str']}\n"
                    f"🔗 {url}\n\n"
                    f"Download: 0.00 MB / {info['size_str']}\n"
                    f"Speed: 0.00 MB/s\n"
                    f"Upload: 0.00 MB\n"
                    f"Upload speed: 0.00 MB/s\n"
                )
                status_msg = await self.send_status(client, user_id, status_text, with_cancel=True)
                if status_msg:
                    self.status_messages[user_id].append(status_msg)

                # perform download with progress (async)
                tmp_name = f"{uuid.uuid4().hex}_{info['name']}"
                tmp_path = os.path.join(tempfile.gettempdir(), tmp_name)
                try:
                    await download_with_progress(client, status_msg, url, info, tmp_path, user_id, self)
                except Exception as e:
                    logger.error(f"Download error: {e}")
                    try:
                        await status_msg.edit_text(f"❌ Download failed for {info['name']}:\n`{e}`")
                    except Exception:
                        pass
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
                    # pop and continue
                    self.queues[user_id].pop(0)
                    await asyncio.sleep(1)
                    continue

                # now upload to user with progress
                try:
                    await status_msg.edit_text(status_text + "\nUploading...")
                except Exception:
                    pass

                try:
                    await upload_with_progress(client, status_msg, tmp_path, info, trigger_message, user_id)
                    # schedule deletion after upload via delete_later_task inside upload
                except Exception as e:
                    logger.error(f"Upload failed: {e}")
                    try:
                        await status_msg.edit_text(f"❌ Upload failed: `{e}`")
                    except Exception:
                        pass
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass
                    # pop and continue
                    self.queues[user_id].pop(0)
                    await asyncio.sleep(1)
                    continue

                # finished task
                self.last_download_time[user_id] = time.time()
                # remove finished item
                if self.queues[user_id]:
                    self.queues[user_id].pop(0)

                # cleanup status messages
                await asyncio.sleep(1)
                await self.cleanup_status(client, user_id)

            # end while
        finally:
            self.active_tasks[user_id] -= 1
            # ensure flags cleared
            self.cancelled[user_id] = False
            await self.cleanup_status(client, user_id)

queue = DownloadQueue()

# ---------- Core terabox helpers ----------
def get_file_info_sync(share_url: str) -> dict:
    # Reuse your original implementation but keep it safe
    resp = requests.get(share_url, headers=HEADERS, allow_redirects=True, timeout=30)
    if resp.status_code != 200:
        raise ValueError(f"Failed to fetch share page ({resp.status_code})")
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

# ---------- download with progress (aiohttp) ----------
async def download_with_progress(client: Client, status_msg, share_url: str, info: dict, dest_path: str, user_id: int, queue_obj: DownloadQueue):
    """
    Downloads file via aiohttp, updates status_msg periodically with speed and progress.
    Checks queue_obj.cancelled[user_id] to allow cancelling mid-download.
    """
    url = info["download_link"]
    chunk_size = 64 * 1024
    start = time.time()
    downloaded = 0
    last_report = start
    last_downloaded = 0
    size_bytes = info.get("size_bytes", 0) or None

    async with aiohttp.ClientSession(headers=DL_HEADERS) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=None)) as resp:
            if resp.status >= 400:
                raise ValueError(f"Download request failed (status {resp.status})")
            # prefer Content-Length header if available
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    size_bytes = int(content_length)
                except Exception:
                    pass

            with open(dest_path, "wb") as f:
                while True:
                    if queue_obj.cancelled.get(user_id, False):
                        # cancel and cleanup
                        raise asyncio.CancelledError("Queue cancelled by user")
                    chunk = await resp.content.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    # report every 1 second or on completion
                    if now - last_report >= 1 or (size_bytes and downloaded >= size_bytes):
                        speed = (downloaded - last_downloaded) / (now - last_report + 1e-9)
                        last_report = now
                        last_downloaded = downloaded
                        # build progress text
                        dfmt = get_size(downloaded)
                        tfmt = get_size(size_bytes) if size_bytes else info.get("size_str", "Unknown")
                        perc = (downloaded / size_bytes * 100) if size_bytes else 0.0
                        try:
                            pct_text = f"{perc:.2f}%" if size_bytes else "?"
                            await status_msg.edit_text(
                                f"⬇️ Downloading: {info['name']}\n"
                                f"📦 Size: {tfmt}\n"
                                f"📥 Downloaded: {dfmt} / {tfmt} ({pct_text})\n"
                                f"🔄 Speed: {speed/1024/1024:.2f} MB/s\n\n"
                                f"🔗 {share_url}\n\n"
                                f"⏳ To cancel this entire queue press the button below.",
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancel Queue", callback_data="cancel_q")]])
                            )
                        except Exception:
                            # ignore edit errors
                            pass
    return dest_path

# ---------- upload with progress (pyrogram progress callback) ----------
async def upload_with_progress(client: Client, status_msg, file_path: str, info: dict, trigger_message: Message, user_id: int):
    total = os.path.getsize(file_path)
    start = time.time()
    state = {"uploaded": 0, "last_time": start, "last_uploaded": 0}

    async def progress_callback(current, total_bytes):
        now = time.time()
        uploaded = current
        elapsed = now - state["last_time"]
        if elapsed < 0.5:
            return  # throttle UI updates
        speed = (uploaded - state["last_uploaded"]) / (elapsed + 1e-9)
        state["last_time"] = now
        state["last_uploaded"] = uploaded
        try:
            pct = uploaded / total_bytes * 100 if total_bytes else 0
            await status_msg.edit_text(
                f"⬆️ Uploading: {info['name']}\n"
                f"📥 Uploaded: {get_size(uploaded)} / {get_size(total_bytes)} ({pct:.2f}%)\n"
                f"🔄 Upload speed: {speed/1024/1024:.2f} MB/s\n\n"
                f"⏳ Remaining files: {len(queue.queues[user_id]) - 1}"
            )
        except Exception:
            pass

    # send to channel first if configured
    sent_msg = None
    if getattr(CHANNEL, "ID", None):
        try:
            if is_video(info["name"]):
                await client.send_video(chat_id=CHANNEL.ID, video=file_path, caption=f"{info['name']}\n{info['size_str']}")
            else:
                await client.send_document(chat_id=CHANNEL.ID, document=file_path, caption=f"{info['name']}\n{info['size_str']}")
        except Exception as e:
            logger.error(f"Channel forward failed: {e}")

    # now actual user upload with progress callback
    if is_video(info["name"]):
        sent_msg = await client.send_video(
            chat_id=trigger_message.chat.id,
            video=file_path,
            caption=f"{info['name']}\n{info['size_str']}",
            file_name=info['name'],
            progress=progress_callback
        )
    else:
        sent_msg = await client.send_document(
            chat_id=trigger_message.chat.id,
            document=file_path,
            caption=f"{info['name']}\n{info['size_str']}",
            file_name=info['name'],
            progress=progress_callback
        )

    # schedule deletion of file/message after 12 hours
    asyncio.create_task(delete_later_task(sent_msg, file_path, delay=43200))

# ---------- admin check ----------
def is_admin(user_id: int) -> bool:
    """Check if user is owner (admin bypass)"""
    return OWNER_ID and str(user_id) == str(OWNER_ID)

# ---------- message handler ----------
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


    admin_status = is_admin(user_id)

    # add each url as a task
    for url in matches:
        success, reply = await queue.add_task(user_id, admin_status, url)
        await message.reply(reply)

    # start worker if not running
    asyncio.create_task(queue.process_queue(client, user_id, admin_status, message))

# ---------- callback handler (Cancel Queue) ----------
@Client.on_callback_query()
async def callback_handler(client, callback_query):
    user_id = callback_query.from_user.id
    # Only the owner / the queue owner can cancel
    if callback_query.data == "cancel_q":
        # mark cancellation
        queue.cancel_queue(user_id)
        await callback_query.answer("Cancelling your queue...", show_alert=False)
        try:
            await callback_query.message.edit_text("⛔ Queue cancellation requested. Stopping tasks...")
        except Exception:
            pass
                    
