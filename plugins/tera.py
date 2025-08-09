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
from urllib.parse import urlencode, urlparse, parse_qs
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from verify_patch import IS_VERIFY, is_verified, build_verification_link, HOW_TO_VERIFY
from pymongo import MongoClient
from config import CHANNEL, DATABASE

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

TERABOX_REGEX = r'https?://(?:www\.)?[^/\s]*tera[^/\s]*\.[a-z]+/s/[^\s]+'
COOKIE = "ndus=Y2YqaCTteHuiU3Ud_MYU7vHoVW4DNBi0MPmg_1tQ"  # keep or use your env
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Connection": "keep-alive",
    "DNT": "1",
    "Host": "www.terabox.app",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
    "sec-ch-ua": '"Microsoft Edge";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cookie": COOKIE,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
DL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;"
              "q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.terabox.com/",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cookie": COOKIE,
}

# ---------- Concurrency control ----------
# Locks to ensure one active queue per non-admin user
user_locks = {}  # user_id -> asyncio.Lock()

def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]

# ---------- Core terabox helpers (kept from your original) ----------
def get_file_info_sync(share_url: str) -> dict:
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

def download_file_sync(url: str, dest_path: str):
    with requests.get(url, headers=DL_HEADERS, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(r.raw, f)

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
# Change this to your updates channel username if different
UPDATES_CHANNEL = "@Request_bots"

async def is_admin_in_updates_channel(client: Client, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(UPDATES_CHANNEL, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        # If get_chat_member fails (e.g. bot not in channel), treat as non-admin
        return False

# ---------- message handler ----------
@Client.on_message(filters.private)
async def handle_terabox(client: Client, message: Message):
    user_id = message.from_user.id
    # accept text or caption
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.reply("❌ Please send a message containing one or more TeraBox links.")
        return

    # extract links
    matches = re.findall(TERABOX_REGEX, text)
    if not matches:
        await message.reply("❌ No valid TeraBox links found in your message.")
        return

    # verification check
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

    # check admin status in updates channel
    is_admin = await is_admin_in_updates_channel(client, user_id)

    # If not admin -> ensure single active queue per user using a lock
    user_lock = get_user_lock(user_id)
    if not is_admin:
        if user_lock.locked():
            await message.reply("🔒 You already have an active download queue. Please wait until it finishes.")
            return
        # acquire lock for non-admins (will be released after queue completes)
        await user_lock.acquire()

    # Run the sequential processing in a background task so handler returns quickly
    asyncio.create_task(process_links_sequentially(client, message, matches, is_admin, user_lock))

async def process_links_sequentially(client: Client, message: Message, matches: list, is_admin: bool, user_lock: asyncio.Lock):
    user_id = message.from_user.id
    total = len(matches)
    try:
        for idx, url in enumerate(matches, start=1):
            await message.reply(f"🟡 Task {idx}/{total}: Preparing to download\n{url}")

            # fetch file info in thread to avoid blocking
            try:
                info = await asyncio.to_thread(get_file_info_sync, url.strip())
            except Exception as e:
                await message.reply(f"❌ Task {idx}: Failed to get file info:\n{e}")
                # continue to next link instead of aborting all
                continue

            # build unique temp path (avoid conflicts)
            safe_name = f"{uuid.uuid4().hex}_{info['name']}"
            temp_path = os.path.join(tempfile.gettempdir(), safe_name)

            # download in thread
            try:
                await message.reply(f"⬇️ Task {idx}: Downloading {info['name']} ({info['size_str']})...")
                await asyncio.to_thread(download_file_sync, info["download_link"], temp_path)
            except Exception as e:
                await message.reply(f"❌ Task {idx}: Download failed:\n`{e}`")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                continue

            # prepare caption
            caption = (
                f"<code>File Name: {info['name']}</code>\n"
                f"<code>File Size: {info['size_str']}</code>\n"
                f"<code>Link: {url}</code>"
            )

            sent_msg = None
            try:
                # optionally forward to channel first
                if getattr(CHANNEL, "ID", None):
                    try:
                        if is_video(info["name"]):
                            await client.send_video(chat_id=CHANNEL.ID, video=temp_path, caption=caption, file_name=info["name"])
                        else:
                            await client.send_document(chat_id=CHANNEL.ID, document=temp_path, caption=caption, file_name=info["name"])
                    except Exception:
                        # channel send errors should not block user delivery
                        pass

                # send to user
                if is_video(info["name"]):
                    sent_msg = await client.send_video(chat_id=message.chat.id, video=temp_path, caption=caption, file_name=info["name"], protect_content=True)
                else:
                    sent_msg = await client.send_document(chat_id=message.chat.id, document=temp_path, caption=caption, file_name=info["name"], protect_content=True)

                # schedule deletion in background (12 hours)
                asyncio.create_task(delete_later_task(sent_msg, temp_path, delay=43200))

                await message.reply(f"✅ Task {idx}: Done! Scheduled deletion in 12 hours.")
            except Exception as e:
                await message.reply(f"❌ Task {idx}: Upload failed:\n`{e}`")
                # cleanup
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
                continue

            # small pause between tasks to be polite (avoid rate limits). admins can skip the pause.
            if not is_admin:
                await asyncio.sleep(2)

    finally:
        # release lock for non-admins
        if not is_admin and user_lock.locked():
            try:
                user_lock.release()
            except Exception:
                pass
                        
