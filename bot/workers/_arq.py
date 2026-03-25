"""ARQ (async Redis queue) setup and enqueue helper."""

from arq import create_pool
from arq.connections import RedisSettings, ArqRedis

from bot.config import REDIS_URL, ARQ_MAX_JOBS

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


async def enqueue(func_name: str, **kwargs) -> None:
    pool = await get_pool()
    await pool.enqueue_job(func_name, **kwargs)


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
    job_timeout = 7200  # 2 hours per job max
    health_check_interval = 30
