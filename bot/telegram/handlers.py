"""Telegram bot command and message handlers."""

import logging
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import ALLOWED_USERS
from bot.db import crud
from bot.db.models import FileStatus, JobStatus, JobType
from bot.telegram.bot import app

log = logging.getLogger(__name__)

# ── In-memory upload token store (token → user_id) ──
# In production this would live in Redis; kept simple here.
import time

upload_tokens: dict[str, dict] = {}  # token → {"user_id": int, "expires": float}


@dataclass
class GuidedSession:
    state: str = "idle"  # idle|waiting_files|waiting_confirm|waiting_keywords|processing
    mode: str | None = None  # ulp|logs
    file_ids: list[str] = field(default_factory=list)


guided_sessions: dict[int, GuidedSession] = {}

MAX_GUIDED_FILES = 50
MAX_GUIDED_KEYWORDS = 200


def _authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


# ── /start ───────────────────────────────────────────────────────────


@app.on_message(filters.command("start") & filters.private)
async def cmd_start(_, message: Message):
    log.info("Received /start from user %s", message.from_user.id)
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")
    try:
        guided_sessions[message.from_user.id] = GuidedSession()

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("ULP", callback_data="mode:ulp")],
                [InlineKeyboardButton("Logs", callback_data="mode:logs")],
            ]
        )

        await message.reply(
            "👋 **Choose Mode**\n\n"
            "Pick one option to start guided processing:",
            reply_markup=keyboard,
        )
        await crud.get_or_create_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
        )
    except Exception:
        log.exception("Error in /start handler")


@app.on_callback_query(filters.regex(r"^mode:(ulp|logs)$"))
async def on_mode_selected(_, callback: CallbackQuery):
    user_id = callback.from_user.id
    if not _authorized(user_id):
        return await callback.answer("Not authorized", show_alert=True)

    mode = callback.data.split(":", 1)[1]
    session = guided_sessions.get(user_id, GuidedSession())
    session.mode = mode
    session.state = "waiting_files"
    session.file_ids.clear()
    guided_sessions[user_id] = session

    await callback.message.reply(
        f"✅ Mode selected: **{mode.upper()}**\n\n"
        "Now send one or more files.\n"
        "When done, tap **Done Adding Files**."
    )
    await callback.answer()


@app.on_callback_query(filters.regex(r"^files:(add_more|done|cancel)$"))
async def on_files_action(_, callback: CallbackQuery):
    user_id = callback.from_user.id
    session = guided_sessions.get(user_id)
    if not session:
        await callback.answer("Start with /start first", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]

    if action == "add_more":
        session.state = "waiting_files"
        await callback.message.reply("📤 Send remaining files.")
        await callback.answer()
        return

    if action == "cancel":
        guided_sessions[user_id] = GuidedSession()
        await callback.message.reply("❎ Guided flow cancelled. Use /start to begin again.")
        await callback.answer()
        return

    # done
    if not session.file_ids:
        await callback.answer("Send at least one file first.", show_alert=True)
        return

    session.state = "waiting_keywords"
    ready_count = 0
    for fid in session.file_ids:
        fr = await crud.get_file(fid)
        if fr and fr.status == FileStatus.READY:
            ready_count += 1
    await callback.message.reply(
        f"📦 Files collected: {len(session.file_ids)} (ready: {ready_count}, downloading: {len(session.file_ids) - ready_count})\n\n"
        "🧾 Send domains/keywords, one per line.\n\n"
        "Example:\n"
        "amazon\n"
        "netflix\n"
        "optifine"
    )
    await callback.answer("Now send keywords/domains")


# ── /upload — Generate web upload link ───────────────────────────────


@app.on_message(filters.command("upload") & filters.private)
async def cmd_upload(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    token = secrets.token_urlsafe(32)
    upload_tokens[token] = {
        "user_id": message.from_user.id,
        "expires": time.time() + 3600,
    }

    # The web upload URL will be the Railway public domain
    # Users should set WEB_BASE_URL env var; fallback to placeholder
    import os

    base = os.environ.get("WEB_BASE_URL", "https://your-app.up.railway.app")
    url = f"{base}/upload/{token}"

    await message.reply(
        f"🔗 **Web Upload Link** (valid for 1 hour):\n\n"
        f"`{url}`\n\n"
        f"Open this link in your browser to upload files of **any size**.\n"
        f"No Telegram size limits apply!"
    )


# ── /download <url> — Download from URL ─────────────────────────────


@app.on_message(filters.command("download") & filters.private)
async def cmd_download(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: `/download <url>`")

    url = parts[1].strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", "ftp"):
        return await message.reply("❌ Invalid URL. Must be http/https/ftp.")

    # Derive filename from URL
    original_name = Path(parsed.path).name or "download"

    await crud.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    file_record = await crud.create_file(
        user_id=message.from_user.id,
        original_name=original_name,
    )
    job = await crud.create_job(
        user_id=message.from_user.id,
        job_type=JobType.DOWNLOAD_URL,
        file_id=file_record.id,
    )

    status_msg = await message.reply("⬇️ **Starting URL download...**")

    # Enqueue download job via ARQ
    from bot.workers._arq import enqueue

    await enqueue(
        "download_url",
        user_id=message.from_user.id,
        file_id=str(file_record.id),
        url=url,
        original_name=original_name,
        job_id=str(job.id),
        chat_id=message.chat.id,
        status_message_id=status_msg.id,
    )


# ── Receive any document/file ────────────────────────────────────────


@app.on_message(filters.document & filters.private)
async def on_document(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    doc = message.document
    original_name = doc.file_name or f"file_{doc.file_id}"

    await crud.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    file_record = await crud.create_file(
        user_id=message.from_user.id,
        original_name=original_name,
        size_bytes=doc.file_size,
        mime_type=doc.mime_type,
    )
    job = await crud.create_job(
        user_id=message.from_user.id,
        job_type=JobType.DOWNLOAD_TELEGRAM,
        file_id=file_record.id,
    )

    # Check if we're in a guided flow — download silently
    session = guided_sessions.get(message.from_user.id)
    in_guided = session and session.state in {"waiting_files", "waiting_confirm"}

    from bot.workers._arq import enqueue

    if in_guided:
        # Silent download: no progress messages, no completion message
        await enqueue(
            "download_telegram_file",
            user_id=message.from_user.id,
            file_id=str(file_record.id),
            telegram_file_id=doc.file_id,
            original_name=original_name,
            job_id=str(job.id),
            chat_id=message.chat.id,
            silent=True,
        )

        if len(session.file_ids) >= MAX_GUIDED_FILES:
            return await message.reply(
                f"⚠️ Max {MAX_GUIDED_FILES} files reached. Tap Done Adding Files."
            )

        session.file_ids.append(str(file_record.id))
        session.state = "waiting_confirm"

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Add More", callback_data="files:add_more"),
                    InlineKeyboardButton("Done Adding Files", callback_data="files:done"),
                ],
                [InlineKeyboardButton("Cancel", callback_data="files:cancel")],
            ]
        )
        await message.reply(
            f"📎 File {len(session.file_ids)} added.",
            reply_markup=kb,
        )
    else:
        # Non-guided: full progress + completion message
        status_msg = await message.reply("⬇️ **Downloading file from Telegram...**")
        await enqueue(
            "download_telegram_file",
            user_id=message.from_user.id,
            file_id=str(file_record.id),
            telegram_file_id=doc.file_id,
            original_name=original_name,
            job_id=str(job.id),
            chat_id=message.chat.id,
            status_message_id=status_msg.id,
        )


# ── /files — List user's files ───────────────────────────────────────


@app.on_message(filters.command("files") & filters.private)
async def cmd_files(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    files = await crud.list_user_files(message.from_user.id)
    if not files:
        return await message.reply("📂 No files stored yet. Send me a file!")

    lines = ["📂 **Your Files:**\n"]
    for f in files[:30]:
        size = _human(f.size_bytes) if f.size_bytes else "?"
        lines.append(f"• `{f.id}` — **{f.original_name}** ({size})")

    if len(files) > 30:
        lines.append(f"\n... and {len(files) - 30} more")

    await message.reply("\n".join(lines))


# ── /search <file_id> <pattern> ──────────────────────────────────────


@app.on_message(filters.command("search") & filters.private)
async def cmd_search(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply("Usage: `/search <file_id> <pattern>`")

    file_id = parts[1]
    pattern = parts[2]

    try:
        uuid.UUID(file_id)
    except ValueError:
        return await message.reply("❌ Invalid file ID.")

    file_record = await crud.get_file(uuid.UUID(file_id))
    if not file_record or file_record.user_id != message.from_user.id:
        return await message.reply("❌ File not found.")

    job = await crud.create_job(
        user_id=message.from_user.id,
        job_type=JobType.SEARCH,
        file_id=uuid.UUID(file_id),
    )

    status_msg = await message.reply(f"🔍 Searching for `{pattern}`...\nProgress: 0%")

    from bot.workers._arq import enqueue

    await enqueue(
        "search_file",
        user_id=message.from_user.id,
        file_id=file_id,
        pattern=pattern,
        job_id=str(job.id),
        chat_id=message.chat.id,
        status_message_id=status_msg.id,
    )


# ── /unzip <file_id> [password] ─────────────────────────────────────


@app.on_message(filters.command("unzip") & filters.private)
async def cmd_unzip(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        return await message.reply("Usage: `/unzip <file_id> [password]`")

    file_id = parts[1]
    password = parts[2] if len(parts) > 2 else None

    try:
        uuid.UUID(file_id)
    except ValueError:
        return await message.reply("❌ Invalid file ID.")

    file_record = await crud.get_file(uuid.UUID(file_id))
    if not file_record or file_record.user_id != message.from_user.id:
        return await message.reply("❌ File not found.")

    job = await crud.create_job(
        user_id=message.from_user.id,
        job_type=JobType.EXTRACT,
        file_id=uuid.UUID(file_id),
    )

    await message.reply("📦 Starting extraction...")

    from bot.workers._arq import enqueue

    await enqueue(
        "extract_archive",
        user_id=message.from_user.id,
        file_id=file_id,
        password=password,
        job_id=str(job.id),
        chat_id=message.chat.id,
    )


# ── /delete <file_id> ───────────────────────────────────────────────


@app.on_message(filters.command("delete") & filters.private)
async def cmd_delete(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: `/delete <file_id>`")

    file_id = parts[1].strip()
    try:
        fid = uuid.UUID(file_id)
    except ValueError:
        return await message.reply("❌ Invalid file ID.")

    file_record = await crud.get_file(fid)
    if not file_record or file_record.user_id != message.from_user.id:
        return await message.reply("❌ File not found.")

    # Delete from local disk
    if file_record.local_path:
        from bot.storage import local

        await local.delete_path(Path(file_record.local_path))

    # Delete from bucket
    if file_record.bucket_key:
        from bot.storage import bucket

        await bucket.delete_object(file_record.bucket_key)

    await crud.delete_file(fid)
    await message.reply(f"🗑️ File `{file_record.original_name}` deleted.")


# ── /scan — Scan all stored files ────────────────────────────────────


@app.on_message(filters.command("scan") & filters.private)
async def cmd_scan(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    await crud.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    files = await crud.list_user_files(message.from_user.id)
    if not files:
        return await message.reply("📂 No files stored. Send files first or use /start.")

    total_bytes = sum(f.size_bytes or 0 for f in files)
    total_gb = total_bytes / (1024 ** 3)
    file_ids = [str(f.id) for f in files]

    session = GuidedSession()
    session.state = "waiting_keywords"
    session.mode = "ulp"
    session.file_ids = file_ids
    guided_sessions[message.from_user.id] = session

    await message.reply(
        f"📊 **{len(files)} files** stored — **{total_gb:.2f} GB** total.\n\n"
        "Send keywords to scan (one per line):"
    )


# ── /status — Active jobs ────────────────────────────────────────────


@app.on_message(filters.command("status") & filters.private)
async def cmd_status(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    jobs = await crud.list_active_jobs(message.from_user.id)
    if not jobs:
        return await message.reply("✅ No active jobs.")

    lines = ["⚙️ **Active Jobs:**\n"]
    for j in jobs:
        emoji = "🔄" if j.status == JobStatus.RUNNING else "⏳"
        pct = f" ({j.progress}%)" if j.progress else ""
        lines.append(f"{emoji} `{j.id}` — {j.job_type.value}{pct}")

    await message.reply("\n".join(lines))


# ── /cancel <job_id> ─────────────────────────────────────────────────


@app.on_message(filters.command("cancel") & filters.private)
async def cmd_cancel(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: `/cancel <job_id>`")

    try:
        jid = uuid.UUID(parts[1].strip())
    except ValueError:
        return await message.reply("❌ Invalid job ID.")

    job = await crud.get_job(jid)
    if not job or job.user_id != message.from_user.id:
        return await message.reply("❌ Job not found.")

    await crud.update_job(jid, status=JobStatus.CANCELLED)
    await message.reply(f"🛑 Job `{jid}` cancelled.")


@app.on_message(filters.text & filters.private & ~filters.command(["start", "upload", "download", "files", "search", "unzip", "delete", "status", "cancel", "scan"]))
async def on_guided_keywords(_, message: Message):
    try:
        user_id = message.from_user.id
        session = guided_sessions.get(user_id)
        if not session or session.state != "waiting_keywords":
            return

        lines = [line.strip() for line in (message.text or "").splitlines()]
        keywords: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if not line:
                continue
            norm = line.lower()
            if norm in seen:
                continue
            seen.add(norm)
            keywords.append(line)

        if not keywords:
            await message.reply("❌ No valid keywords found. Send one keyword/domain per line.")
            return

        if len(keywords) > MAX_GUIDED_KEYWORDS:
            await message.reply(f"❌ Too many keywords. Max is {MAX_GUIDED_KEYWORDS}.")
            return

        if not session.file_ids:
            await message.reply("❌ No files collected. Use /start and send files first.")
            session.state = "idle"
            return

        session.state = "processing"
        mode = session.mode or "ulp"
        mode_label = mode.upper()

        primary_file_id = session.file_ids[0] if session.file_ids else None
        # Use existing enum value to avoid runtime failure when DB enum schema wasn't migrated.
        job = await crud.create_job(
            user_id=user_id,
            job_type=JobType.SEARCH,
            file_id=primary_file_id,
        )

        status_msg = await message.reply(
            f"🚀 Starting {mode_label} extraction for {len(keywords)} keyword(s) across {len(session.file_ids)} file(s)...\n"
            "Progress: 0%"
        )

        from bot.workers._arq import enqueue

        await enqueue(
            "extract_keywords_batch",
            user_id=user_id,
            file_ids=session.file_ids,
            keywords=keywords,
            job_id=str(job.id),
            chat_id=message.chat.id,
            status_message_id=status_msg.id,
            mode=mode,
        )

        # Reset for next /start flow.
        guided_sessions[user_id] = GuidedSession()
    except Exception as e:
        log.exception("Guided keyword submission failed: %s", e)
        await message.reply(f"❌ Failed to start extraction: `{e}`")


# ── Helper ───────────────────────────────────────────────────────────


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore
    return f"{n:.1f} PB"


# ── Debug: catch-all handler to confirm updates are arriving ─────────


@app.on_message(group=99)
async def _debug_log(_, message: Message):
    log.info(
        "DEBUG incoming message: chat=%s user=%s text=%r",
        message.chat.id,
        getattr(message.from_user, "id", None),
        (message.text or "")[:80],
    )
