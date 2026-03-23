"""Telegram bot command and message handlers."""

import secrets
import uuid
from pathlib import Path
from urllib.parse import urlparse

from pyrogram import filters
from pyrogram.types import Message

from bot.config import ALLOWED_USERS, MAX_CONCURRENT_DOWNLOADS
from bot.db import crud
from bot.db.models import FileStatus, JobStatus, JobType
from bot.telegram.bot import app
from bot.telegram.progress import ProgressTracker

# ── In-memory upload token store (token → user_id) ──
# In production this would live in Redis; kept simple here.
import time

upload_tokens: dict[str, dict] = {}  # token → {"user_id": int, "expires": float}


def _authorized(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


# ── /start ───────────────────────────────────────────────────────────


@app.on_message(filters.command("start") & filters.private)
async def cmd_start(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")
    await crud.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )
    await message.reply(
        "👋 **File Processing Bot**\n\n"
        "Send me any file (up to 4 GB via Telegram) and I'll process it.\n\n"
        "**Commands:**\n"
        "/upload — Get a web upload link (no size limit)\n"
        "/download `<url>` — Download file from URL\n"
        "/files — List your stored files\n"
        "/search `<file_id>` `<pattern>` — Search text in a file\n"
        "/unzip `<file_id>` `[password]` — Extract an archive\n"
        "/delete `<file_id>` — Delete a file\n"
        "/status — Show active jobs\n"
        "/cancel `<job_id>` — Cancel a job\n"
    )


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

    # Check concurrent downloads
    active = await crud.count_active_jobs(message.from_user.id)
    if active >= MAX_CONCURRENT_DOWNLOADS:
        return await message.reply(
            f"⏳ You have {active} active jobs. Max is {MAX_CONCURRENT_DOWNLOADS}. Wait or `/cancel` one."
        )

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

    active = await crud.count_active_jobs(message.from_user.id)
    if active >= MAX_CONCURRENT_DOWNLOADS:
        return await message.reply(
            f"⏳ You have {active} active jobs. Max is {MAX_CONCURRENT_DOWNLOADS}."
        )

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

    status_msg = await message.reply("⬇️ **Downloading file from Telegram...**")

    from bot.workers._arq import enqueue

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

    await message.reply(f"🔍 Searching for `{pattern}`...")

    from bot.workers._arq import enqueue

    await enqueue(
        "search_file",
        user_id=message.from_user.id,
        file_id=file_id,
        pattern=pattern,
        job_id=str(job.id),
        chat_id=message.chat.id,
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


# ── Helper ───────────────────────────────────────────────────────────


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore
    return f"{n:.1f} PB"
