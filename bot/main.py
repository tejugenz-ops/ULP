"""
Entry point — starts Pyrogram bot + FastAPI web server + ARQ worker.
Pyrogram runs as the primary async loop owner; FastAPI runs in a thread.
"""

import asyncio
import logging
import threading
from urllib.request import urlopen

import uvicorn

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

    # Start ARQ worker as background task
    arq_task = asyncio.create_task(start_arq_worker())
    log.info("ARQ worker started")

    # Start Pyrogram bot
    log.info("Starting Telegram bot...")
    await tg_app.start()

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
