"""
Standalone ARQ worker entrypoint.

Run as: python -m bot.worker_main
Env vars required: WORKER_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID,
                   TELEGRAM_API_HASH, DATABASE_URL, REDIS_URL, DATA_DIR, WORKER_COUNT

This process:
  1. Inits the DB (runs safe migrations)
  2. Starts a Pyrogram client with session "file_bot_w{WORKER_ID}" (own MTProto pipe)
  3. Runs the ARQ worker listening on queue "arq:w{WORKER_ID}"

It does NOT start the Telegram bot polling loop — that lives in main.py (bot service only).
"""

import asyncio
import glob
import logging
from pathlib import Path

import bot.patches  # noqa: F401  — must run before any Session.start()
from bot.config import WORKER_ID
from bot.db.models import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def start_arq_worker() -> None:
    from arq.worker import create_worker
    from bot.workers._arq import WorkerSettings

    log.info("Worker %d: starting ARQ on queue arq:w%d", WORKER_ID, WORKER_ID)
    worker = create_worker(WorkerSettings)
    await worker.async_run()


async def main() -> None:
    log.info("=== Worker %d starting ===", WORKER_ID)

    log.info("Worker %d: initialising database...", WORKER_ID)
    await init_db()

    # Import and start Pyrogram client
    from bot.telegram.bot import app as tg_app

    # Clean stale session files so we always get a fresh MTProto auth
    session_base = str(Path(tg_app.workdir) / tg_app.name) + ".session"
    for f in glob.glob(session_base + "*"):
        Path(f).unlink(missing_ok=True)
        log.info("Worker %d: removed session file: %s", WORKER_ID, f)

    log.info("Worker %d: connecting to Telegram (session=%s)...", WORKER_ID, tg_app.name)
    await tg_app.start()
    me = await tg_app.get_me()
    log.info("Worker %d: authenticated as @%s (id=%s)", WORKER_ID, me.username, me.id)

    # Run ARQ worker — blocks until process is stopped
    await start_arq_worker()

    await tg_app.stop()


if __name__ == "__main__":
    asyncio.run(main())
