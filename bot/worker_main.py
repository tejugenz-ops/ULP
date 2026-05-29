"""
Standalone ARQ worker entrypoint.

Run as: python -m bot.worker_main

On FIRST deploy each worker needs to call auth.ImportBotAuthorization once to
create its session file on /data.  After that, restarts reuse the saved session
(no auth call, instant startup).

Startup sequence:
  1. Health server starts immediately on PORT so Railway's healthcheck passes.
  2. Worker waits  WORKER_ID * STAGGER_SECONDS  before touching Telegram so
     only one worker auths at a time (avoids FloodWait from simultaneous calls).
  3. If Telegram returns a FloodWait the worker sleeps INSIDE the process for
     the required duration — Railway does NOT restart it, and the timer expires
     naturally.  On subsequent restarts the saved session is reused and no auth
     call is made.
  4. Once connected, the ARQ worker starts listening on  arq:w{WORKER_ID}.
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

# Gap between each worker's first Telegram auth attempt (seconds).
# Worker 1 → 30s, worker 2 → 60s, … worker 10 → 300s (5 min).
STAGGER_SECONDS = 30


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
    log.info("Worker %d: health server on :%d/health", WORKER_ID, port)
    server.serve_forever()


# ── Telegram connect (with FloodWait handling) ────────────────────────────────

async def _connect_telegram(tg_app) -> None:
    """Connect Pyrogram, sleeping inside the process if Telegram rate-limits us.

    If a session file already exists on /data the auth key is reused and this
    returns in ~1 second with no ImportBotAuthorization call at all.
    """
    import glob
    from pathlib import Path

    session_base = str(Path(tg_app.workdir) / tg_app.name) + ".session"

    while True:
        try:
            await tg_app.start()
            return
        except Exception as exc:
            exc_name = type(exc).__name__
            flood_secs = getattr(exc, "value", None)

            if flood_secs and "FloodWait" in exc_name:
                wait = int(flood_secs) + 10  # +10s safety buffer
                log.warning(
                    "Worker %d: FloodWait %ds — sleeping inside process "
                    "(Railway will NOT restart us during this wait)...",
                    WORKER_ID, wait,
                )
                await asyncio.sleep(wait)
            else:
                log.error(
                    "Worker %d: tg_app.start() failed (%s: %s), retrying in 60s...",
                    WORKER_ID, exc_name, exc,
                )
                await asyncio.sleep(60)

            # Clean any partial/corrupt session before retrying
            for f in glob.glob(session_base + "*"):
                Path(f).unlink(missing_ok=True)


# ── ARQ worker ────────────────────────────────────────────────────────────────

async def _start_arq_worker() -> None:
    from arq.worker import create_worker
    from bot.workers._arq import WorkerSettings

    log.info("Worker %d: starting ARQ on queue arq:w%d", WORKER_ID, WORKER_ID)
    worker = create_worker(WorkerSettings)
    await worker.async_run()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("=== Worker %d starting ===", WORKER_ID)

    # Stagger: spread auth calls so workers don't all call ImportBotAuthorization
    # at the same time.  On restarts (session file exists) this wait is harmless
    # and can be removed by setting STAGGER_SECONDS=0 via env var.
    stagger_env = int(os.environ.get("STAGGER_SECONDS", str(STAGGER_SECONDS)))
    wait = WORKER_ID * stagger_env
    if wait > 0:
        log.info(
            "Worker %d: stagger wait %ds before Telegram auth...",
            WORKER_ID, wait,
        )
        await asyncio.sleep(wait)

    log.info("Worker %d: initialising database...", WORKER_ID)
    await init_db()

    from bot.telegram.bot import app as tg_app

    log.info(
        "Worker %d: connecting to Telegram (session=%s)...",
        WORKER_ID, tg_app.name,
    )
    await _connect_telegram(tg_app)

    me = await tg_app.get_me()
    log.info("Worker %d: connected as @%s (id=%s)", WORKER_ID, me.username, me.id)

    await _start_arq_worker()

    await tg_app.stop()


if __name__ == "__main__":
    # Health server starts FIRST — Railway healthcheck passes immediately
    # even while the worker is in its stagger wait or sleeping through a FloodWait.
    threading.Thread(target=_start_health_server, daemon=True).start()

    asyncio.run(main())
