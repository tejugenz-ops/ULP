"""Telegram bot command and message handlers."""

import asyncio
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from pyrogram import filters
from pyrogram.errors import FloodWait
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

# ── Flood-aware reply helper ──
_flood_until = 0.0  # monotonic timestamp when SendMessage flood expires


async def _safe_reply(message, text, **kwargs):
    """Reply, skipping the API call entirely when flood-limited."""
    global _flood_until
    now = time.monotonic()
    if now < _flood_until:
        return None
    try:
        return await message.reply(text, **kwargs)
    except FloodWait as e:
        _flood_until = now + e.value
        log.warning("FloodWait %ds on reply, skipping", e.value)
        return None


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

# ── Collection mode state ──
_collect_state: dict[int, dict] = {}  # user_id → {items: [{msg_id, file_id, name}], timer, asked}


def _reset_collect(user_id: int):
    st = _collect_state.pop(user_id, None)
    if st and st.get("timer"):
        st["timer"].cancel()


async def _collect_ask(user_id: int, chat_id: int):
    """Fire after 3s inactivity to ask if collection is complete."""
    await asyncio.sleep(3)
    st = _collect_state.get(user_id)
    if not st or not st["items"] or st.get("asked"):
        return
    st["asked"] = True
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Done \u2014 download all", callback_data="collect:done")],
        [InlineKeyboardButton("Export .txt only", callback_data="collect:txt")],
        [InlineKeyboardButton("Cancel", callback_data="collect:cancel")],
    ])
    try:
        await app.send_message(
            chat_id,
            f"\U0001f4e5 **{len(st['items'])}** file(s) collected.\n\nAre these all?",
            reply_markup=kb,
        )
    except Exception:
        pass


def _restart_collect_timer(user_id: int, chat_id: int):
    st = _collect_state.get(user_id)
    if not st:
        return
    if st.get("timer"):
        st["timer"].cancel()
    st["asked"] = False
    st["timer"] = asyncio.create_task(_collect_ask(user_id, chat_id))


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

    status_msg = await _safe_reply(message, "⬇️ **Starting URL download...")
    status_message_id = status_msg.id if status_msg else 0

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
        status_message_id=status_message_id,
    )


# ── Receive any document/file ────────────────────────────────────────


@app.on_message(filters.document & filters.private)
async def on_document(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")

    doc = message.document
    original_name = doc.file_name or f"file_{doc.file_id}"

    # ── Collect mode — store file, don't download yet ──
    collect = _collect_state.get(message.from_user.id)
    if collect is not None:
        collect["items"].append({
            "message_id": message.id,
            "file_id": doc.file_id,
            "name": original_name,
        })
        _restart_collect_timer(message.from_user.id, message.chat.id)
        return

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
            await _safe_reply(
                message,
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
                sent = await _safe_reply(message, text, reply_markup=kb)
                if sent:
                    _file_button_msg[message.from_user.id] = sent.id
        else:
            sent = await _safe_reply(message, text, reply_markup=kb)
            if sent:
                _file_button_msg[message.from_user.id] = sent.id
    else:
        # Non-guided: full progress + completion message
        status_msg = await _safe_reply(message, "⬇️ **Downloading file from Telegram...**")
        status_message_id = status_msg.id if status_msg else 0
        await enqueue(
            "download_telegram_file",
            user_id=message.from_user.id,
            file_id=str(file_record.id),
            telegram_file_id=doc.file_id,
            original_name=original_name,
            job_id=str(job.id),
            chat_id=message.chat.id,
            status_message_id=status_message_id,
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

    status_msg = await _safe_reply(message, f"\U0001f50d Searching for `{pattern}`...\nProgress: 0%")
    status_message_id = status_msg.id if status_msg else 0

    from bot.workers._arq import enqueue

    await enqueue(
        "search_file",
        user_id=message.from_user.id,
        file_id=file_id,
        pattern=pattern,
        job_id=str(job.id),
        chat_id=message.chat.id,
        status_message_id=status_message_id,
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

    await _safe_reply(message, "\n".join(lines))


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


# ── /collect — Collect files then batch-download ─────────────────────


@app.on_message(filters.command("collect") & filters.private)
async def cmd_collect(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("⛔ You are not authorized.")
    uid = message.from_user.id
    _reset_collect(uid)
    await crud.get_or_create_user(uid, message.from_user.username, message.from_user.first_name)
    _collect_state[uid] = {"items": [], "timer": None, "asked": False}
    await message.reply(
        "\U0001f4e5 **Collection mode started.**\n\n"
        "Send or forward files. After you stop sending, I'll ask if you're done.\n"
        "Then I'll batch-download everything."
    )


# ── /d — Batch download from file_id .txt ────────────────────────────


@app.on_message(filters.command("d") & filters.private & filters.reply)
async def cmd_batch_download(_, message: Message):
    if not _authorized(message.from_user.id):
        return await message.reply("\u26d4 You are not authorized.")

    replied = message.reply_to_message
    if not replied or not replied.document:
        return await message.reply("\u274c Reply to a .txt file with /d")

    doc = replied.document
    if not (doc.file_name or "").lower().endswith(".txt"):
        return await message.reply("\u274c The replied file must be a .txt")

    await crud.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    # Download the .txt
    import tempfile as _tmpmod
    tmp = Path(_tmpmod.gettempdir()) / f"batch_{message.from_user.id}.txt"
    try:
        await app.download_media(doc.file_id, file_name=str(tmp))
        content = tmp.read_text(encoding="utf-8", errors="ignore")
    finally:
        tmp.unlink(missing_ok=True)

    file_ids = [line.strip() for line in content.splitlines() if line.strip()]
    if not file_ids:
        return await message.reply("\u274c The .txt file is empty or has no valid lines.")

    total = len(file_ids)
    status_msg = await _safe_reply(
        message,
        f"\u2b07\ufe0f **Batch download starting...**\n{total} file(s) queued."
    )
    status_message_id = status_msg.id if status_msg else 0

    from bot.workers._arq import enqueue

    job_map: list[dict] = []  # [{job_id, name, telegram_file_id}]

    for i, tg_fid in enumerate(file_ids):
        name = f"file_{i+1}"
        file_record = await crud.create_file(
            user_id=message.from_user.id,
            original_name=name,
        )
        job = await crud.create_job(
            user_id=message.from_user.id,
            job_type=JobType.DOWNLOAD_TELEGRAM,
            file_id=file_record.id,
        )
        job_map.append({
            "job_id": str(job.id),
            "file_id": str(file_record.id),
            "name": name,
            "telegram_file_id": tg_fid,
        })
        await enqueue(
            "download_telegram_file",
            user_id=message.from_user.id,
            file_id=str(file_record.id),
            telegram_file_id=tg_fid,
            original_name=name,
            job_id=str(job.id),
            chat_id=message.chat.id,
            silent=True,
        )

    # Poll for progress
    chat_id = message.chat.id
    job_ids = [j["job_id"] for j in job_map]
    last_edit = 0.0

    while True:
        await asyncio.sleep(3)
        counts = await crud.count_job_statuses(job_ids)
        done = counts.get("completed", 0) + counts.get("failed", 0) + counts.get("cancelled", 0)
        failed = counts.get("failed", 0)
        running_names = []

        if done < total:
            # Find a currently downloading file name for display
            for jm in job_map:
                j = await crud.get_job(jm["job_id"])
                if j and j.status == JobStatus.RUNNING:
                    running_names.append(jm["name"])
                    if len(running_names) >= 2:
                        break

        pct = int(done * 100 / total) if total else 100
        current = ", ".join(running_names[:2]) if running_names else "..."
        text = f"\u2b07\ufe0f **Batch download:** {done}/{total} ({pct}%)"
        if running_names:
            text += f"\n\U0001f4e5 Downloading: {current}"

        now = time.monotonic()
        if status_message_id and now - last_edit >= 5.0:
            try:
                await app.edit_message_text(chat_id, status_message_id, text)
                last_edit = now
            except Exception:
                pass

        if done >= total:
            break

    # Final summary
    total_bytes = 0
    for jm in job_map:
        f = await crud.get_file(jm["file_id"])
        if f and f.size_bytes:
            total_bytes += f.size_bytes

    summary = (
        f"\u2705 **Batch complete!**\n"
        f"\U0001f4c1 {total} file(s) \u2014 {_human(total_bytes)}"
    )
    if failed:
        summary += f"\n\u26a0\ufe0f {failed} failed"

    if status_message_id:
        try:
            await app.edit_message_text(chat_id, status_message_id, summary)
        except Exception:
            await _safe_reply(message, summary)
    else:
        await _safe_reply(message, summary)


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


# ── Collect-mode callbacks ────────────────────────────────────────────


@app.on_callback_query(filters.regex(r"^collect:(done|cancel|txt)$"))
async def on_collect_action(_, callback: CallbackQuery):
    uid = callback.from_user.id
    if not _authorized(uid):
        return await callback.answer("Not authorized", show_alert=True)

    action = callback.data.split(":", 1)[1]

    if action == "cancel":
        _reset_collect(uid)
        await callback.message.edit_text("❌ Collection cancelled.")
        return await callback.answer()

    st = _collect_state.get(uid)
    if not st or not st["items"]:
        return await callback.answer("No files collected!", show_alert=True)

    items = list(st["items"])
    count = len(items)
    _reset_collect(uid)
    chat_id = callback.message.chat.id
    await callback.answer()

    # ── Export .txt with file_ids ──
    file_ids_text = "\n".join(item["file_id"] for item in items)
    import tempfile as _tmpmod
    txt_path = Path(_tmpmod.gettempdir()) / f"file_ids_{uid}.txt"
    txt_path.write_text(file_ids_text, encoding="utf-8")
    try:
        await app.send_document(chat_id, str(txt_path),
                                caption=f"{count} file IDs exported.",
                                file_name="file_ids.txt")
    except Exception:
        pass
    finally:
        txt_path.unlink(missing_ok=True)

    block = f"```\n{file_ids_text}\n```"
    if len(block) <= 4096:
        try:
            await app.send_message(chat_id, block)
        except Exception:
            pass

    if action == "txt":
        await callback.message.edit_text(f"✅ **{count}** file ID(s) exported.")
        return

    # ── action == "done" → batch download ──
    await callback.message.edit_text(
        f"✅ **{count}** file(s) collected. Starting batch download..."
    )

    from bot.workers._arq import enqueue

    job_map: list[dict] = []
    for item in items:
        # Re-fetch message so Pyrogram returns a fresh file_reference
        try:
            fresh = await app.get_messages(chat_id, item["message_id"])
            fid = (fresh.document.file_id
                   if fresh and fresh.document else item["file_id"])
        except Exception:
            fid = item["file_id"]

        name = item["name"]
        file_record = await crud.create_file(user_id=uid, original_name=name)
        job = await crud.create_job(
            user_id=uid, job_type=JobType.DOWNLOAD_TELEGRAM,
            file_id=file_record.id,
        )
        job_map.append({
            "job_id": str(job.id),
            "file_id": str(file_record.id),
            "name": name,
        })
        await enqueue(
            "download_telegram_file",
            user_id=uid,
            file_id=str(file_record.id),
            telegram_file_id=fid,
            original_name=name,
            job_id=str(job.id),
            chat_id=chat_id,
            silent=True,
        )

    # ── Progress poll (shared logic with /d) ──
    total = count
    status_msg = None
    try:
        status_msg = await app.send_message(
            chat_id, f"⬇️ **Batch download:** 0/{total} (0%)")
    except Exception:
        pass
    smid = status_msg.id if status_msg else 0

    job_ids = [j["job_id"] for j in job_map]
    last_edit = 0.0

    while True:
        await asyncio.sleep(3)
        counts = await crud.count_job_statuses(job_ids)
        done = (counts.get("completed", 0) + counts.get("failed", 0)
                + counts.get("cancelled", 0))
        failed = counts.get("failed", 0)
        running_names: list[str] = []

        if done < total:
            for jm in job_map:
                j = await crud.get_job(jm["job_id"])
                if j and j.status == JobStatus.RUNNING:
                    running_names.append(jm["name"])
                    if len(running_names) >= 2:
                        break

        pct = int(done * 100 / total) if total else 100
        text = f"⬇️ **Batch download:** {done}/{total} ({pct}%)"
        if running_names:
            text += f"\n📥 Downloading: {', '.join(running_names[:2])}"

        now = time.monotonic()
        if smid and now - last_edit >= 5.0:
            try:
                await app.edit_message_text(chat_id, smid, text)
                last_edit = now
            except Exception:
                pass

        if done >= total:
            break

    total_bytes = 0
    for jm in job_map:
        f = await crud.get_file(jm["file_id"])
        if f and f.size_bytes:
            total_bytes += f.size_bytes
    summary = f"✅ **Batch complete!**\n📁 {total} file(s) — {_human(total_bytes)}"
    if failed:
        summary += f"\n⚠️ {failed} failed"
    if smid:
        try:
            await app.edit_message_text(chat_id, smid, summary)
        except Exception:
            pass


@app.on_message(filters.text & filters.private & ~filters.command(["start", "upload", "download", "files", "search", "unzip", "delete", "status", "cancel", "scan", "stop", "d", "collect"]))
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

        status_msg = await _safe_reply(
            message,
            f"\U0001f680 Starting {mode_label} extraction for {len(keywords)} keyword(s) across {len(session.file_ids)} file(s)...\n"
            "Progress: 0%"
        )
        status_message_id = status_msg.id if status_msg else 0

        from bot.workers._arq import enqueue

        await enqueue(
            "extract_keywords_batch",
            user_id=user_id,
            file_ids=session.file_ids,
            keywords=keywords,
            job_id=str(job.id),
            chat_id=message.chat.id,
            status_message_id=status_message_id,
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
