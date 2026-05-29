"""
Standalone ARQ worker entrypoint.

Run as: python -m bot.worker_main
Env vars required: WORKER_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID,
                   TELEGRAM_API_HASH, DATABASE_URL, REDIS_URL, DATA_DIR, WORKER_COUNT

This process:
  1. Starts a tiny HTTP server on PORT (default 8080) serving GET /health → 200
     so Railway's healthcheck passes immediately.
  2. Inits the DB (runs safe migrations)
  3. Connects the Pyrogram client using the saved session on /data — no fresh
     auth.ImportBotAuthorization call; the session file is never deleted.
  4. Runs the ARQ worker listening on queue "arq:w{WORKER_ID}"

It does NOT start the Telegram bot polling loop — that lives in main.py (bot service only).
"""

import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import bot.patches  # noqa: F401  — must run before any Session.start()
from bot.config import WORKER_ID
from bot.db.models import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


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

    log.info("Worker %d: initialising database...", WORKER_ID)
    await init_db()

    from bot.telegram.bot import app as tg_app

    # Connect using the saved session file on /data — Pyrogram will reuse
    # the existing auth key without calling auth.ImportBotAuthorization.
    # The session file is intentionally NOT deleted so restarts are instant.
    log.info("Worker %d: connecting to Telegram (session=%s)...", WORKER_ID, tg_app.name)
    await tg_app.start()
    me = await tg_app.get_me()
    log.info("Worker %d: connected as @%s (id=%s)", WORKER_ID, me.username, me.id)

    # Run ARQ worker — blocks until process is stopped
    await start_arq_worker()

    await tg_app.stop()


if __name__ == "__main__":
    # Start health server first so Railway's healthcheck passes immediately.
    health_thread = threading.Thread(target=_start_health_server, daemon=True)
    health_thread.start()

    asyncio.run(main())
