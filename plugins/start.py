#please give credits https://github.com/MN-BOTS
#  @MrMNTG @MusammilN
from pyrogram import Client as MN_Bot
from pyrogram import filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from verify_patch import IS_VERIFY, validate_token_and_verify, is_verified, build_verification_link, HOW_TO_VERIFY
from datetime import datetime

#please give credits https://github.com/MN-BOTS
#  @MrMNTG @MusammilN
class TEXT:
    START = """
<b>I’m a powerful Terabox downloader!</b>

📥 Send me a Terabox link to download.
⚠️ Only videos under 2GB are supported.
📢 Don’t forget to join our update channel.

"""
    DEVELOPER = "👨‍💻 Developer"
    UPDATES_CHANNEL = "📢 Updates Channel"
    SOURCE_CODE = "💬 Support Group"

class INLINE:
    START_BTN = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(TEXT.DEVELOPER, url="https://t.me/Chowdary106")],
            [
                InlineKeyboardButton(TEXT.UPDATES_CHANNEL, url="https://t.me/Request_bots"),
                InlineKeyboardButton(TEXT.SOURCE_CODE, url="https://t.me/botsadmin_support"),
            ],
        ]
    )

#please give credits https://github.com/MN-BOTS
#  @MrMNTG @MusammilN
@MN_Bot.on_message(filters.command("start"))
async def start(client: MN_Bot, message: Message):
    user_id = message.from_user.id
    args = message.text.split()

    # Handle verification token in /start parameter
    if len(args) > 1 and args[1].startswith("verify_"):
        token = args[1].split("_", 1)[1]
        if await validate_token_and_verify(user_id, token):
            await message.reply_text("✅ You are now verified! You can use the bot for 12 hours.")
        else:
            await message.reply_text("❌ Invalid or expired verification link.")
        return

    user = message.from_user
    mention = user.mention
    await message.reply_text(
        TEXT.START,
        disable_web_page_preview=True,
        reply_markup=INLINE.START_BTN,
    )

#please give credits https://github.com/MN-BOTS
#  @MrMNTG @MusammilN
