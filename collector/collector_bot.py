"""
File ID Collector Bot — collects forwarded files and exports telegram_file_id list.

Send files (documents, photos, videos, audio) → wait 3s → "Are these all?" →
user says "done" or taps button → bot sends count + .txt file + copyable code block.
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("collector")

BOT_TOKEN = os.environ["COLLECTOR_BOT_TOKEN"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

app = Client(
    "collector_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=tempfile.gettempdir(),
)

# Per-user state
_state: dict[int, dict] = {}  # user_id → {"file_ids": [...], "timer": Task|None, "asked": bool}


def _get(user_id: int) -> dict:
    if user_id not in _state:
        _state[user_id] = {"file_ids": [], "timer": None, "asked": False}
    return _state[user_id]


def _reset(user_id: int) -> None:
    st = _state.pop(user_id, None)
    if st and st.get("timer"):
        st["timer"].cancel()


async def _ask_after_delay(user_id: int, chat_id: int):
    """Wait 3 seconds of inactivity then ask if these are all files."""
    await asyncio.sleep(3)
    st = _state.get(user_id)
    if not st or not st["file_ids"] or st["asked"]:
        return
    st["asked"] = True
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Done", callback_data="collect:done")],
        [InlineKeyboardButton("Cancel", callback_data="collect:cancel")],
    ])
    count = len(st["file_ids"])
    await app.send_message(
        chat_id,
        f"Received **{count}** file(s) so far.\n\nAre these all?",
        reply_markup=kb,
    )


def _restart_timer(user_id: int, chat_id: int):
    st = _get(user_id)
    if st["timer"]:
        st["timer"].cancel()
    st["asked"] = False
    st["timer"] = asyncio.create_task(_ask_after_delay(user_id, chat_id))


def _extract_file_id(message: Message) -> str | None:
    """Get the telegram file_id from any media type."""
    if message.document:
        return message.document.file_id
    if message.photo:
        return message.photo.file_id
    if message.video:
        return message.video.file_id
    if message.audio:
        return message.audio.file_id
    if message.voice:
        return message.voice.file_id
    if message.video_note:
        return message.video_note.file_id
    if message.animation:
        return message.animation.file_id
    if message.sticker:
        return message.sticker.file_id
    return None


# ── /start ──

@app.on_message(filters.command("start") & filters.private)
async def cmd_start(_, message: Message):
    _reset(message.from_user.id)
    await message.reply(
        "**File ID Collector**\n\n"
        "Send me files (documents, photos, videos, audio).\n"
        "After you stop sending, I'll ask if you're done.\n"
        "Then I'll export all file IDs to a .txt file.\n\n"
        "Send /cancel to reset."
    )


# ── /cancel ──

@app.on_message(filters.command("cancel") & filters.private)
async def cmd_cancel(_, message: Message):
    _reset(message.from_user.id)
    await message.reply("Cleared. Send files to start again.")


# ── Receive any media ──

@app.on_message(filters.private & (filters.document | filters.photo | filters.video | filters.audio | filters.voice | filters.video_note | filters.animation | filters.sticker))
async def on_media(_, message: Message):
    fid = _extract_file_id(message)
    if not fid:
        return

    user_id = message.from_user.id
    st = _get(user_id)
    st["file_ids"].append(fid)
    _restart_timer(user_id, message.chat.id)


# ── Done / Cancel callbacks ──

@app.on_callback_query(filters.regex(r"^collect:(done|cancel)$"))
async def on_collect_action(_, callback: CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data.split(":", 1)[1]

    if action == "cancel":
        _reset(user_id)
        await callback.message.edit_text("Cancelled. Send files to start again.")
        await callback.answer()
        return

    # done
    st = _state.get(user_id)
    if not st or not st["file_ids"]:
        await callback.answer("No files collected!", show_alert=True)
        return

    file_ids = st["file_ids"]
    count = len(file_ids)
    _reset(user_id)

    # Build .txt content
    txt_content = "\n".join(file_ids)

    # Send count
    await callback.message.edit_text(f"Total files: **{count}**\n\nGenerating export...")
    await callback.answer()

    # Send .txt file
    txt_path = Path(tempfile.gettempdir()) / f"file_ids_{user_id}.txt"
    txt_path.write_text(txt_content, encoding="utf-8")
    try:
        await app.send_document(
            callback.message.chat.id,
            str(txt_path),
            caption=f"{count} file IDs exported.",
            file_name="file_ids.txt",
        )
    finally:
        txt_path.unlink(missing_ok=True)

    # Send copyable code block
    # Telegram has a 4096 char limit per message, split if needed
    block = f"```\n{txt_content}\n```"
    if len(block) <= 4096:
        await app.send_message(callback.message.chat.id, block)
    else:
        # Split into chunks
        chunks = []
        current = []
        current_len = 0
        for fid in file_ids:
            line_len = len(fid) + 1  # +1 for newline
            if current_len + line_len + 8 > 4096:  # 8 for ``` markers
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(fid)
            current_len += line_len
        if current:
            chunks.append("\n".join(current))

        for i, chunk in enumerate(chunks, 1):
            await app.send_message(
                callback.message.chat.id,
                f"```\n{chunk}\n```",
            )


# ── "done" text message ──

@app.on_message(filters.text & filters.private & ~filters.command(["start", "cancel"]))
async def on_text(_, message: Message):
    if (message.text or "").strip().lower() == "done":
        st = _state.get(message.from_user.id)
        if st and st["file_ids"]:
            # Simulate the done callback
            class FakeCallback:
                from_user = message.from_user
                data = "collect:done"
                message_obj = message

                async def answer(self, *a, **kw):
                    pass

            # Reuse on_collect_action logic directly
            file_ids = st["file_ids"]
            count = len(file_ids)
            _reset(message.from_user.id)

            txt_content = "\n".join(file_ids)

            await message.reply(f"Total files: **{count}**\n\nGenerating export...")

            txt_path = Path(tempfile.gettempdir()) / f"file_ids_{message.from_user.id}.txt"
            txt_path.write_text(txt_content, encoding="utf-8")
            try:
                await app.send_document(
                    message.chat.id,
                    str(txt_path),
                    caption=f"{count} file IDs exported.",
                    file_name="file_ids.txt",
                )
            finally:
                txt_path.unlink(missing_ok=True)

            block = f"```\n{txt_content}\n```"
            if len(block) <= 4096:
                await app.send_message(message.chat.id, block)
            else:
                chunks = []
                current = []
                current_len = 0
                for fid in file_ids:
                    line_len = len(fid) + 1
                    if current_len + line_len + 8 > 4096:
                        chunks.append("\n".join(current))
                        current = []
                        current_len = 0
                    current.append(fid)
                    current_len += line_len
                if current:
                    chunks.append("\n".join(current))
                for chunk in chunks:
                    await app.send_message(message.chat.id, f"```\n{chunk}\n```")


if __name__ == "__main__":
    log.info("Starting File ID Collector Bot...")
    app.run()
