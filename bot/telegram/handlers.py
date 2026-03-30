"""Telegram bot command and message handlers."""

import asyncio
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

# Pending archive password input: user_id → file_id (latest archive awaiting password)
pending_archive_pw: dict[int, str] = {}

# Track the "files added" button message per user so we can edit it instead of
# flooding the chat with one message per file.
_file_button_msg: dict[int, int] = {}  # user_id → message_id

MAX_GUIDED_FILES = 999999
MAX_GUIDED_KEYWORDS = 999999


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
        _file_button_msg.pop(message.from_user.id, None)

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
        f"✅ Mode: **{mode.upper()}** — Send your files."
    )
    await callback.answer()


@app.on_callback_query(filters.regex(r"^files:(done|cancel)$"))
async def on_files_action(_, callback: CallbackQuery):
    user_id = callback.from_user.id
    session = guided_sessions.get(user_id)
    if not session:
        await callback.answer("Start with /start first", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]

    if action == "cancel":
        guided_sessions[user_id] = GuidedSession()
        _file_button_msg.pop(user_id, None)
        await callback.message.edit_text("❎ Cancelled.")
        await callback.answer()
        return

    # done
    if not session.file_ids:
        await callback.answer("Send at least one file first.", show_alert=True)
        return

    session.state = "waiting_keywords"
    _file_button_msg.pop(user_id, None)
    await callback.message.edit_text(
        f"📦 **{len(session.file_ids)}** file(s) collected.\n\n"
        "Send keywords/domains to search (one per line):"
    )
    await callback.answer()


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

    # ── Duplicate detection ──
    unique_id = getattr(doc, "file_unique_id", None)
    if unique_id:
        existing = await crud.find_file_by_unique_id(unique_id)
        if existing:
            status_label = "already downloaded" if existing.status.value == "ready" else "already downloading"
            await message.reply(
                f"♻️ **Duplicate skipped** — `{existing.original_name}` is {status_label}."
            )
            # Still track in guided session so user doesn't re-send
            session = guided_sessions.get(message.from_user.id)
            if session and session.state in {"waiting_files", "waiting_confirm"}:
                session.file_ids.append(str(existing.id))
                session.state = "waiting_confirm"
            return

    file_record = await crud.create_file(
        user_id=message.from_user.id,
        original_name=original_name,
        size_bytes=doc.file_size,
        mime_type=doc.mime_type,
        telegram_file_unique_id=unique_id,
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

        session.file_ids.append(str(file_record.id))
        session.state = "waiting_confirm"

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Done — these were all files", callback_data="files:done"),
                ],
                [InlineKeyboardButton("Cancel", callback_data="files:cancel")],
            ]
        )
        text = f"📎 **{len(session.file_ids)}** file(s) added. Send more or tap Done."

        # Edit the existing button message instead of sending a new one per file
        prev_msg_id = _file_button_msg.get(message.from_user.id)
        if prev_msg_id:
            try:
                await app.edit_message_text(
                    message.chat.id, prev_msg_id, text, reply_markup=kb,
                )
            except Exception:
                # Message may have been deleted or too old — send a new one
                sent = await message.reply(text, reply_markup=kb)
                _file_button_msg[message.from_user.id] = sent.id
        else:
            sent = await message.reply(text, reply_markup=kb)
            _file_button_msg[message.from_user.id] = sent.id
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

    files = await crud.list_all_ready_files()
    if not files:
        return await message.reply("📂 No files in database. Upload files first or use /start.")

    total_bytes = sum(f.size_bytes or 0 for f in files)
    total_gb = total_bytes / (1024 ** 3)
    file_ids = [str(f.id) for f in files]

    session = GuidedSession()
    session.state = "waiting_keywords"
    session.mode = "ulp"
    session.file_ids = file_ids
    guided_sessions[message.from_user.id] = session

    await message.reply(
        f"📊 **{len(files)} files** in database — **{total_gb:.2f} GB** total.\n\n"
        "Send keywords to scan (one per line):"
    )


# ── /status — Dashboard ──────────────────────────────────────────────


@app.on_message(filters.command("status") & filters.private)
async def cmd_status(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    stats = await crud.get_db_stats()
    jobs = await crud.list_all_active_jobs()

    lines = [
        "📊 **Bot Status**\n",
        f"📁 **Total files in DB:** {stats['total_files']}",
        f"✅ **Ready:** {stats['ready_count']}  ({_human(stats['ready_bytes'])})",
        f"⬇️ **Downloading:** {stats['downloading']}",
    ]

    # Active jobs breakdown
    running = [j for j in jobs if j.status == JobStatus.RUNNING]
    queued = [j for j in jobs if j.status == JobStatus.QUEUED]
    lines.append(f"\n⚙️ **Active jobs:** {len(running)} running, {len(queued)} queued")

    if running:
        lines.append("")
        for j in running:
            pct = f" — {j.progress}%" if j.progress else ""
            lines.append(f"🔄 `{j.job_type.value}`{pct}")

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


# ── /stop — Stop own jobs  |  /stop all — Stop everything ────────────


@app.on_message(filters.command("stop") & filters.private)
async def cmd_stop(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    from bot.workers._arq import abort_all_queued

    args = message.text.split(maxsplit=1)
    stop_all = len(args) > 1 and args[1].strip().lower() == "all"

    if stop_all:
        cancelled = await crud.cancel_all_jobs()
        q_removed = await abort_all_queued()
        await message.reply(
            f"🛑 **Stopped everything.**\n"
            f"Jobs cancelled: {cancelled}\n"
            f"Queued tasks flushed: {q_removed}"
        )
    else:
        cancelled = await crud.cancel_user_jobs(message.from_user.id)
        await message.reply(
            f"🛑 **Stopped all your jobs.**\n"
            f"Jobs cancelled: {cancelled}"
        )


@app.on_callback_query(filters.regex(r"^archcancel:"))
async def on_archive_cancel(_, callback: CallbackQuery):
    """Cancel a password-protected archive and delete it."""
    user_id = callback.from_user.id
    if not _authorized(user_id):
        return await callback.answer("Not authorized", show_alert=True)

    file_id = callback.data.split(":", 1)[1]

    from bot.workers.decompressor import pending_passwords

    info = pending_passwords.pop(file_id, None)
    pending_archive_pw.pop(user_id, None)

    # Delete the archive file
    file_record = await crud.get_file(uuid.UUID(file_id))
    if file_record:
        if file_record.local_path:
            p = Path(file_record.local_path)
            if p.exists():
                p.unlink()
            parent = p.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        if file_record.bucket_key:
            from bot.storage import bucket
            try:
                await bucket.delete_object(file_record.bucket_key)
            except Exception:
                pass
        await crud.delete_jobs_for_file(uuid.UUID(file_id))
        await crud.delete_children(uuid.UUID(file_id))
        await crud.delete_file(uuid.UUID(file_id))

    # Cancel the job if tracked
    if info:
        try:
            await crud.update_job(info["job_id"], status=JobStatus.CANCELLED)
        except Exception:
            pass

    await callback.message.edit_text(
        f"🗑️ Archive `{file_record.original_name if file_record else file_id}` cancelled and deleted."
    )
    await callback.answer()


@app.on_message(filters.text & filters.private & ~filters.command(["start", "upload", "download", "files", "search", "unzip", "delete", "status", "cancel", "scan", "stop"]))
async def on_guided_keywords(_, message: Message):
    try:
        user_id = message.from_user.id

        # Check if user is providing an archive password
        pw_file_id = pending_archive_pw.get(user_id)
        if not pw_file_id:
            # Also check decompressor pending_passwords to auto-detect
            from bot.workers.decompressor import pending_passwords
            for fid, info in pending_passwords.items():
                if info.get("user_id") == user_id:
                    pw_file_id = fid
                    break

        if pw_file_id:
            password = (message.text or "").strip()
            if not password:
                return

            from bot.workers.decompressor import pending_passwords
            info = pending_passwords.pop(pw_file_id, None)
            pending_archive_pw.pop(user_id, None)

            if not info:
                return

            # Delete the user's password message for privacy
            try:
                await message.delete()
            except Exception:
                pass

            await message.reply(f"🔑 Retrying extraction with password...")

            from bot.workers._arq import enqueue
            await enqueue(
                "extract_archive",
                user_id=info["user_id"],
                file_id=pw_file_id,
                password=password,
                job_id=info["job_id"],
                chat_id=info["chat_id"],
            )
            return

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
