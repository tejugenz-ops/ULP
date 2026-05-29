"""
Standalone ARQ worker entrypoint.

Run as: python -m bot.worker_main
Env vars required: WORKER_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID,
                   TELEGRAM_API_HASH, DATABASE_URL, REDIS_URL, DATA_DIR, WORKER_COUNT

This process:
  1. Starts a tiny HTTP server on PORT (default 8080) serving GET /health → 200
     so Railway's healthcheck passes immediately.
  2. Waits WORKER_ID * 15 seconds before touching Telegram so all 10 workers
     don't flood auth.ImportBotAuthorization at the same time.
  3. Inits the DB (runs safe migrations)
  4. Starts a Pyrogram client with session "file_bot_w{WORKER_ID}" (own MTProto pipe)
  5. Runs the ARQ worker listening on queue "arq:w{WORKER_ID}"

It does NOT start the Telegram bot polling loop — that lives in main.py (bot service only).
"""

import asyncio
import glob
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import bot.patches  # noqa: F401  — must run before any Session.start()
from bot.config import WORKER_ID
from bot.db.models import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# Seconds between each worker's first Telegram auth attempt.
# Worker 1 waits 15s, worker 2 waits 30s, … worker 10 waits 150s.
_STAGGER_SECONDS = 15


# ── Minimal health server ─────────────────────────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # silence access logs


def _start_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    log.info("Worker %d: health server listening on :%d/health", WORKER_ID, port)
    server.serve_forever()


# ── ARQ worker ────────────────────────────────────────────────────────────────

async def start_arq_worker() -> None:
    from arq.worker import create_worker
    from bot.workers._arq import WorkerSettings

    log.info("Worker %d: starting ARQ on queue arq:w%d", WORKER_ID, WORKER_ID)
    worker = create_worker(WorkerSettings)
    await worker.async_run()


async def main() -> None:
    log.info("=== Worker %d starting ===", WORKER_ID)

    # Stagger Telegram auth: each worker waits WORKER_ID * _STAGGER_SECONDS
    # before attempting auth.ImportBotAuthorization so they don't all flood
    # Telegram's rate limit simultaneously.
    stagger = WORKER_ID * _STAGGER_SECONDS
    if stagger > 0:
        log.info(
            "Worker %d: waiting %ds before Telegram auth (stagger offset)...",
            WORKER_ID, stagger,
        )
        await asyncio.sleep(stagger)

    log.info("Worker %d: initialising database...", WORKER_ID)
    await init_db()

    from bot.telegram.bot import app as tg_app

    # Clean stale session files so we always get a fresh MTProto auth
    session_base = str(Path(tg_app.workdir) / tg_app.name) + ".session"
    for f in glob.glob(session_base + "*"):
        Path(f).unlink(missing_ok=True)
        log.info("Worker %d: removed session file: %s", WORKER_ID, f)

    log.info("Worker %d: connecting to Telegram (session=%s)...", WORKER_ID, tg_app.name)

    # Retry loop that respects Telegram's FloodWait instead of crash-looping.
    while True:
        try:
            await tg_app.start()
            break
        except Exception as exc:
            exc_name = type(exc).__name__
            # FloodWait carries the required wait in seconds as exc.value
            wait_secs = getattr(exc, "value", None)
            if wait_secs and "FloodWait" in exc_name:
                log.warning(
                    "Worker %d: Telegram FloodWait — sleeping %ds as instructed...",
                    WORKER_ID, wait_secs,
                )
                await asyncio.sleep(int(wait_secs) + 5)  # +5s safety buffer
            else:
                log.error(
                    "Worker %d: tg_app.start() failed (%s: %s), retrying in 60s...",
                    WORKER_ID, exc_name, exc,
                )
                await asyncio.sleep(60)
            # Clean session files before each retry
            for f in glob.glob(session_base + "*"):
                Path(f).unlink(missing_ok=True)

    me = await tg_app.get_me()
    log.info("Worker %d: authenticated as @%s (id=%s)", WORKER_ID, me.username, me.id)

    # Run ARQ worker — blocks until process is stopped
    await start_arq_worker()

    await tg_app.stop()


if __name__ == "__main__":
    # Start health server in a background daemon thread FIRST so Railway's
    # healthcheck passes immediately while the worker initialises / waits.
    health_thread = threading.Thread(target=_start_health_server, daemon=True)
    health_thread.start()

    asyncio.run(main())
