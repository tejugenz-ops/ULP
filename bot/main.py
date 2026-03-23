"""
Entry point — starts Pyrogram bot + FastAPI web server + ARQ worker
all in a single process for Railway deployment.
"""

import asyncio
import logging
import signal

import uvicorn
from arq import create_pool

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


async def run_arq_worker():
    """Run ARQ worker in-process (no separate process needed)."""
    from arq.worker import create_worker

    worker = create_worker(WorkerSettings)
    await worker.async_run()


async def run_web_server():
    """Run FastAPI via uvicorn programmatically."""
    config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run_telegram():
    """Start Pyrogram and keep it alive."""
    # Import handlers to register them with the Pyrogram client
    import bot.telegram.handlers  # noqa: F401

    await tg_app.start()
    log.info("Telegram bot started, waiting for messages...")

    # Keep alive until cancelled — Pyrogram needs the event loop running
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await tg_app.stop()


async def main():
    log.info("Initializing database...")
    await init_db()

    log.info("Starting services...")

    # Start all three services concurrently
    await asyncio.gather(
        run_telegram(),        # Pyrogram bot (MTProto)
        run_web_server(),      # FastAPI (HTTP)
        run_arq_worker(),      # ARQ job worker
    )


def _shutdown(sig, loop):
    log.info("Received %s, shutting down...", sig.name)
    for task in asyncio.all_tasks(loop):
        task.cancel()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown, sig, loop)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down gracefully...")
    finally:
        # Stop Pyrogram cleanly
        try:
            loop.run_until_complete(tg_app.stop())
        except Exception:
            pass
        loop.close()
