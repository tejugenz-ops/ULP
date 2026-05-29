"""ARQ (async Redis queue) setup and enqueue helper."""

from arq import create_pool
from arq.connections import RedisSettings, ArqRedis

from bot.config import REDIS_URL, ARQ_MAX_JOBS, WORKER_ID, WORKER_COUNT

_pool: ArqRedis | None = None


def _parse_redis_url(url: str) -> RedisSettings:
    """Parse a redis:// URL into ArqRedis settings."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        password=parsed.password,
        database=int(parsed.path.lstrip("/") or "0"),
    )


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(_parse_redis_url(REDIS_URL))
    return _pool


def worker_queue(worker_id: int) -> str:
    """Return the ARQ queue name for a given worker ID."""
    return f"arq:w{worker_id}"


async def get_next_worker_queue() -> str:
    """Round-robin across WORKER_COUNT dedicated workers.

    Falls back to arq:w0 in single-service mode (WORKER_COUNT=0).
    Uses Redis INCR as an atomic counter so multiple bot instances agree.
    """
    if WORKER_COUNT <= 0:
        return worker_queue(0)
    pool = await get_pool()
    idx = await pool.incr("worker:rr_counter")
    wid = ((int(idx) - 1) % WORKER_COUNT) + 1
    return worker_queue(wid)


async def enqueue(func_name: str, *, _queue: str | None = None, **kwargs) -> None:
    """Enqueue a job to a specific queue (defaults to arq:w0)."""
    pool = await get_pool()
    q = _queue or worker_queue(0)
    await pool.enqueue_job(func_name, **kwargs, _queue_name=q)


async def enqueue_round_robin(func_name: str, **kwargs) -> str:
    """Enqueue a download job to the next worker in rotation.

    Returns the queue name used so the caller can stamp worker_id if needed.
    """
    q = await get_next_worker_queue()
    await enqueue(func_name, _queue=q, **kwargs)
    return q


async def abort_all_queued() -> int:
    """Delete all queued ARQ jobs from Redis. Returns count removed."""
    pool = await get_pool()
    all_jobs = await pool.queued_jobs()
    count = 0
    for job_info in all_jobs:
        try:
            await pool.delete(job_info.job_id)  # type: ignore[attr-defined]
        except Exception:
            pass
        count += 1
    return count


# ── Worker class (used by ARQ worker process) ────────────────────────

from bot.workers.downloader import download_telegram_file, download_url
from bot.workers.extractor import extract_keywords_batch, search_file
from bot.workers.decompressor import extract_archive


class WorkerSettings:
    """ARQ worker settings — discovered by `arq bot.workers._arq.WorkerSettings`."""

    functions = [
        download_telegram_file,
        download_url,
        search_file,
        extract_keywords_batch,
        extract_archive,
    ]
    redis_settings = _parse_redis_url(REDIS_URL)
    max_jobs = ARQ_MAX_JOBS
    job_timeout = 7200
    health_check_interval = 30
    # Each worker listens exclusively to its own queue.
    # Bot service: arq:w0  |  Worker N: arq:wN
    queue_name = worker_queue(WORKER_ID)
