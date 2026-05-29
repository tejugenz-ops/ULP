"""
Entry point — starts Pyrogram bot + FastAPI web server + ARQ worker.
Pyrogram runs as the primary async loop owner; FastAPI runs in a thread.
"""

import asyncio
import glob
import logging
import threading
from pathlib import Path
from urllib.request import urlopen

import uvicorn

import bot.patches  # noqa: F401  — must run before any Session.start()
from bot.config import REDIS_URL
from bot.db.models import init_db
from bot.telegram.bot import app as tg_app
from bot.web.app import web_app
from bot.workers._arq import WorkerSettings, _parse_redis_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def _extract_pending_archives():
    """Find READY archives with no children and enqueue extraction."""
    from bot.db import crud
    from bot.workers.downloader import ARCHIVE_EXTENSIONS, _auto_extract

    try:
        archives = await crud.list_unextracted_archives(ARCHIVE_EXTENSIONS)
        if not archives:
            log.info("No pending archives to extract.")
            return
        log.info("Found %d unextracted archive(s), enqueuing...", len(archives))
        for f in archives:
            try:
                await _auto_extract(f.user_id, str(f.id), f.user_id)  # chat_id = user_id for private chats
                log.info("Enqueued extraction for %s (%s)", f.original_name, f.id)
            except Exception:
                log.exception("Failed to enqueue extraction for %s", f.id)
    except Exception:
        log.exception("Error scanning for pending archives")


def start_web_server():
    """Run FastAPI/uvicorn in a daemon thread."""
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.run()


async def start_arq_worker():
    """Start ARQ worker as a background task."""
    from arq.worker import create_worker

    worker = create_worker(WorkerSettings)
    await worker.async_run()


async def main():
    log.info("Initializing database...")
    await init_db()

    # Import handlers to register them BEFORE starting the client
    import bot.telegram.handlers  # noqa: F401
    log.info("Handler module loaded")

    # Start FastAPI in a daemon thread (won't block asyncio loop)
    web_thread = threading.Thread(target=start_web_server, daemon=True)
    web_thread.start()
    log.info("Web server started in background thread on :8080")

    # ── Flush stale ARQ jobs from Redis ──
    # Previous deploys may have left queued jobs that would fire immediately
    # and flood Telegram API calls before the session is stable.
    try:
        from bot.workers._arq import get_pool
        from bot.config import WORKER_COUNT
        pool = await get_pool()
        stale_keys = await pool.keys("arq:job:*")
        if stale_keys:
            await pool.delete(*stale_keys)
        # Flush all worker queues (arq:w0 .. arq:wN) plus old arq:queue
        queues_to_flush = [b"arq:queue", b"arq:w0"]
        for wid in range(1, (WORKER_COUNT or 0) + 1):
            queues_to_flush.append(f"arq:w{wid}".encode())
        flushed = 0
        for q in queues_to_flush:
            n = await pool.zcard(q)
            if n:
                await pool.delete(q)
                flushed += n
        log.info("Flushed %d stale ARQ job key(s) and %d queued job(s)",
                 len(stale_keys), flushed)
    except Exception:
        log.exception("Failed to flush stale ARQ jobs")

    # ── Reset stuck DOWNLOADING files ──
    # If the bot crashed mid-download the File row stays as DOWNLOADING but
    # the ARQ job no longer exists.  Mark them ERROR so duplicate-detection
    # doesn't block the user from re-sending the same file after a restart.
    try:
        from bot.db import crud
        from bot.db.models import FileStatus
        reset_count = await crud.reset_stuck_downloads()
        if reset_count:
            log.info("Reset %d stuck DOWNLOADING file(s) to ERROR on startup", reset_count)
    except Exception:
        log.exception("Failed to reset stuck downloads")

    # ── Clean Pyrogram session files ──
    # Delete ALL session-related files (session + SQLite journal/wal/shm)
    # to prevent corrupted auth_key / msg_id reconnect loops.
    # Bot tokens re-authenticate instantly so nothing valuable is lost.
    session_base = str(Path(tg_app.workdir) / tg_app.name) + ".session"
    for f in glob.glob(session_base + "*"):
        Path(f).unlink(missing_ok=True)
        log.info("Removed session file: %s", f)

    # ── Start Pyrogram (retry loop that handles FloodWait) ──
    log.info("Starting Telegram bot...")
    while True:
        try:
            await tg_app.start()
            break
        except Exception as exc:
            exc_name = type(exc).__name__
            flood_secs = getattr(exc, "value", None)
            try:
                await tg_app.stop()
            except Exception:
                pass
            # Re-clean session files before next attempt
            for f in glob.glob(session_base + "*"):
                Path(f).unlink(missing_ok=True)
            if flood_secs and "FloodWait" in exc_name:
                wait = int(flood_secs) + 10
                log.warning(
                    "FloodWait %ds from Telegram — sleeping inside process "
                    "(will not crash, Railway will not restart us)...", wait,
                )
                await asyncio.sleep(wait)
            else:
                log.error("tg_app.start() failed (%s: %s), retrying in 60s...", exc_name, exc)
                await asyncio.sleep(60)

    me = await tg_app.get_me()
    log.info("Bot authenticated as @%s (id=%s)", me.username, me.id)

    # Delete any webhook that might steal updates
    try:
        token = tg_app.bot_token
        with urlopen(f"https://api.telegram.org/bot{token}/deleteWebhook") as resp:
            log.info("deleteWebhook: %s", resp.read().decode())
    except Exception as e:
        log.warning("Could not delete webhook: %s", e)

    # Check pending updates via Bot API
    try:
        token = tg_app.bot_token
        with urlopen(f"https://api.telegram.org/bot{token}/getWebhookInfo") as resp:
            log.info("getWebhookInfo: %s", resp.read().decode())
    except Exception as e:
        log.warning("Could not get webhook info: %s", e)

    # NOW start ARQ worker — Pyrogram is ready so workers can send messages safely
    arq_task = asyncio.create_task(start_arq_worker())
    log.info("ARQ worker started")

    log.info("Bot is running! Send /start to test.")

    # Extract any already-downloaded archives that haven't been extracted yet
    asyncio.create_task(_extract_pending_archives())

    # Use Pyrogram's idle to keep the bot alive
    from pyrogram import idle
    await idle()

    # Cleanup
    await tg_app.stop()
    arq_task.cancel()


if __name__ == "__main__":
    tg_app.run(main())
