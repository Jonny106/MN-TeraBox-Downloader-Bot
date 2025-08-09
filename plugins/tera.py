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
from collections import defaultdict
from urllib.parse import urlencode, urlparse, parse_qs
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from verify_patch import IS_VERIFY, is_verified, build_verification_link, HOW_TO_VERIFY
from pymongo import MongoClient
from config import CHANNEL, DATABASE

# ---------- Environment Config ----------
OWNER_ID = os.environ.get("OWNER")  # Set OWNER=your_telegram_id in environment

# [Previous helper functions and config remain the same...]

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
            except:
                pass
    
    async def process_queue(self, client: Client, user_id: int, is_admin: bool, message: Message):
        if self.active_tasks[user_id] > 0:
            return
            
        self.active_tasks[user_id] += 1
        try:
            total_files = len(self.queues[user_id])
            current_position = 0
            
            while self.queues[user_id]:
                current_position += 1
                remaining_files = total_files - current_position
                task_func, url = self.queues[user_id][0]
                
                # Get file info for status message
                try:
                    info = await asyncio.to_thread(get_file_info_sync, url.strip())
                    size_info = f" ({info['size_str']})" if info['size_str'] else ""
                except:
                    info = None
                    size_info = ""
                
                # Create/update status message
                status_msg = (
                    f"⬇️ Downloading Queue {current_position}/{total_files}\n"
                    f"📦 Remaining: {remaining_files} file(s)\n"
                    f"💾 Size: {size_info}" if info else ""
                )
                self.status_messages[user_id] = await message.reply(status_msg)
                
                # Apply delay for non-admins
                if not is_admin:
                    elapsed = asyncio.get_event_loop().time() - self.last_download_time.get(user_id, 0)
                    if elapsed < 30:
                        delay = 30 - elapsed
                        for remaining in range(int(delay), 0, -1):
                            await self.update_status(
                                client, 
                                user_id, 
                                f"⏳ Waiting {remaining}s (30s cooldown)\n"
                                f"📦 Next: Queue {current_position}/{total_files}\n"
                                f"💾 Size: {size_info}" if info else ""
                            )
                            await asyncio.sleep(1)
                
                # Update status before download
                await self.update_status(
                    client,
                    user_id,
                    f"⬇️ Downloading Queue {current_position}/{total_files}\n"
                    f"📦 Remaining: {remaining_files} file(s)\n"
                    f"💾 Size: {size_info}" if info else ""
                )
                
                await task_func()
                self.last_download_time[user_id] = asyncio.get_event_loop().time()
                self.queues[user_id].pop(0)
                
                # Update status after download
                if self.queues[user_id]:
                    await self.update_status(
                        client,
                        user_id,
                        f"✅ Completed Queue {current_position}\n"
                        f"📦 Remaining: {remaining_files} file(s)"
                    )
                    await asyncio.sleep(1)  # Brief pause between downloads
                else:
                    await self.update_status(client, user_id, "✅ All downloads completed!")
                    try:
                        await self.status_messages[user_id].delete()
                    except:
                        pass
        finally:
            self.active_tasks[user_id] -= 1
            if user_id in self.status_messages:
                try:
                    await self.status_messages[user_id].delete()
                except:
                    pass
                del self.status_messages[user_id]

# [Rest of the helper functions remain the same...]

# ---------- message handler ----------
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
        await message.reply(f"❌ Download failed:\n`{e}`")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return

    # Simplified caption with just file name and size
    caption = f"📁 {info['name']}\n💾 Size: {info['size_str']}"

    try:
        if getattr(CHANNEL, "ID", None):
            try:
                if is_video(info["name"]):
                    await client.send_video(chat_id=CHANNEL.ID, video=temp_path, caption=caption, file_name=info["name"])
                else:
                    await client.send_document(chat_id=CHANNEL.ID, document=temp_path, caption=caption, file_name=info["name"])
            except Exception:
                pass

        if is_video(info["name"]):
            sent_msg = await client.send_video(chat_id=message.chat.id, video=temp_path, caption=caption, file_name=info["name"], protect_content=True)
        else:
            sent_msg = await client.send_document(chat_id=message.chat.id, document=temp_path, caption=caption, file_name=info["name"], protect_content=True)

        asyncio.create_task(delete_later_task(sent_msg, temp_path, delay=43200))
    except Exception as e:
        await message.reply(f"❌ Upload failed:\n`{e}`")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

# [Rest of the code remains the same...]                            
    
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
        return

    safe_name = f"{uuid.uuid4().hex}_{info['name']}"
    temp_path = os.path.join(tempfile.gettempdir(), safe_name)

    try:
        await asyncio.to_thread(download_file_sync, info["download_link"], temp_path)
    except Exception as e:
        await message.reply(f"❌ Download failed:\n`{e}`")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return

    caption = (
        f"<code>File Name: {info['name']}</code>\n"
        f"<code>File Size: {info['size_str']}</code>\n"
        f"<code>Link: {url}</code>"
    )

    try:
        if getattr(CHANNEL, "ID", None):
            try:
                if is_video(info["name"]):
                    await client.send_video(chat_id=CHANNEL.ID, video=temp_path, caption=caption, file_name=info["name"])
                else:
                    await client.send_document(chat_id=CHANNEL.ID, document=temp_path, caption=caption, file_name=info["name"])
            except Exception:
                pass

        if is_video(info["name"]):
            sent_msg = await client.send_video(chat_id=message.chat.id, video=temp_path, caption=caption, file_name=info["name"], protect_content=True)
        else:
            sent_msg = await client.send_document(chat_id=message.chat.id, document=temp_path, caption=caption, file_name=info["name"], protect_content=True)

        asyncio.create_task(delete_later_task(sent_msg, temp_path, delay=43200))
    except Exception as e:
        await message.reply(f"❌ Upload failed:\n`{e}`")
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

    matches = re.findall(TERABOX_REGEX, text)
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
