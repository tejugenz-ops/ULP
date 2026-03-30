"""Download workers — Telegram files and URL downloads via aria2c."""

import asyncio
import json
import logging
import re
from pathlib import Path

from bot.config import TG_DOWNLOAD_WORKERS
from bot.db import crud
from bot.db.models import FileStatus, JobStatus, JobType
from bot.storage import bucket, local

log = logging.getLogger(__name__)

# Archive extensions that should be auto-extracted after download
ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
    ".cab", ".iso", ".lzma", ".lz",
}


def _is_archive(name: str) -> bool:
    """Check if filename looks like an archive."""
    low = name.lower()
    # Check double extensions first (e.g. .tar.gz)
    for ext in ARCHIVE_EXTENSIONS:
        if low.endswith(ext):
            return True
    return False


async def _auto_extract(user_id: int, file_id: str, chat_id: int) -> None:
    """Enqueue an extract_archive job for the file."""
    from bot.workers._arq import enqueue

    job = await crud.create_job(
        user_id=user_id,
        job_type=JobType.EXTRACT,
        file_id=file_id,
    )
    await enqueue(
        "extract_archive",
        user_id=user_id,
        file_id=file_id,
        password=None,
        job_id=str(job.id),
        chat_id=chat_id,
    )

# Limit concurrent Telegram downloads to avoid flooding Telegram's media DC
_tg_download_sem: asyncio.Semaphore | None = None


def _get_tg_sem() -> asyncio.Semaphore:
    global _tg_download_sem
    if _tg_download_sem is None:
        _tg_download_sem = asyncio.Semaphore(TG_DOWNLOAD_WORKERS)
    return _tg_download_sem


async def download_telegram_file(
    ctx: dict,
    *,
    user_id: int,
    file_id: str,
    telegram_file_id: str,
    original_name: str,
    job_id: str,
    chat_id: int,
    status_message_id: int | None = None,
    silent: bool = False,
) -> None:
    """Download a file from Telegram via Pyrogram (MTProto, chunked)."""
    from bot.telegram.bot import app as tg
    from bot.telegram.progress import ProgressTracker

    dest_dir = local.user_dir(user_id, file_id)
    dest = dest_dir / original_name

    await crud.update_job(job_id, status=JobStatus.RUNNING, progress=0)
    await crud.update_file(file_id, status=FileStatus.DOWNLOADING)

    try:
        sem = _get_tg_sem()
        async with sem:
            tracker = None
            if not silent and status_message_id:
                msg = await tg.get_messages(chat_id, status_message_id)
                tracker = ProgressTracker(msg, action="⬇️ Downloading from Telegram")

            await tg.download_media(
                telegram_file_id,
                file_name=str(dest),
                progress=tracker.__call__ if tracker else None,
            )

        size = await local.get_file_size(dest)

        # Mark READY immediately so scanning can start
        await crud.update_file(
            file_id,
            status=FileStatus.READY,
            local_path=str(dest),
            size_bytes=size,
        )
        await crud.update_job(job_id, status=JobStatus.COMPLETED, progress=100)

        # Upload to bucket in background — don't block scanning
        async def _bg_upload():
            try:
                bkey = bucket.bucket_key(user_id, file_id, original_name)
                await bucket.upload_file(dest, bkey)
                await crud.update_file(file_id, bucket_key=bkey)
            except Exception as exc:
                log.warning("Background bucket upload failed for %s: %s", file_id, exc)
        asyncio.create_task(_bg_upload())

        # Auto-extract if archive
        if _is_archive(original_name):
            asyncio.create_task(_auto_extract(user_id, file_id, chat_id))
        elif not silent:
            async def _notify():
                try:
                    await tg.send_message(
                        chat_id,
                        f"✅ **Download complete!**\n"
                        f"📄 `{original_name}`\n"
                        f"📦 Size: {_human(size)}\n"
                        f"🔑 File ID: `{file_id}`\n\n"
                        f"Use `/search {file_id} <pattern>` to search\n"
                        f"Use `/unzip {file_id}` to extract archives",
                    )
                except Exception:
                    pass
            asyncio.create_task(_notify())
    except Exception as e:
        log.exception("Telegram download failed: %s", e)
        await crud.update_file(file_id, status=FileStatus.ERROR, error_message=str(e))
        await crud.update_job(
            job_id, status=JobStatus.FAILED, error_message=str(e)
        )
        if not silent:
            try:
                await tg.send_message(chat_id, f"❌ Download failed: `{e}`")
            except Exception:
                pass


async def download_url(
    ctx: dict,
    *,
    user_id: int,
    file_id: str,
    url: str,
    original_name: str,
    job_id: str,
    chat_id: int,
    status_message_id: int,
) -> None:
    """Download a file from a URL using aria2c (multi-connection)."""
    from bot.telegram.bot import app as tg

    dest_dir = local.user_dir(user_id, file_id)

    await crud.update_job(job_id, status=JobStatus.RUNNING, progress=0)
    await crud.update_file(file_id, status=FileStatus.DOWNLOADING)

    try:
        proc = await asyncio.create_subprocess_exec(
            "aria2c",
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            "--continue=true",
            "--max-tries=5",
            "--retry-wait=3",
            "--summary-interval=5",
            "--console-log-level=notice",
            f"--dir={dest_dir}",
            f"--out={original_name}",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        last_edit = 0.0
        msg = await tg.get_messages(chat_id, status_message_id)

        async def _read_progress():
            nonlocal last_edit
            import time

            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                # Parse aria2c progress: [#abc 50MiB/200MiB(25%) ...]
                m = re.search(r"\((\d+)%\)", text)
                if m:
                    pct = int(m.group(1))
                    now = time.monotonic()
                    if now - last_edit >= 3:
                        last_edit = now
                        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                        try:
                            await msg.edit_text(
                                f"**⬇️ Downloading URL**\n{bar} {pct}%"
                            )
                        except Exception:
                            pass
                    await crud.update_job(job_id, progress=pct)

        await _read_progress()
        await proc.wait()

        if proc.returncode != 0:
            raise RuntimeError(f"aria2c exited with code {proc.returncode}")

        dest = dest_dir / original_name
        size = await local.get_file_size(dest)

        # Mark READY immediately so scanning can start
        await crud.update_file(
            file_id,
            status=FileStatus.READY,
            local_path=str(dest),
            size_bytes=size,
        )
        await crud.update_job(job_id, status=JobStatus.COMPLETED, progress=100)

        # Upload to bucket in background — don't block scanning
        async def _bg_upload():
            try:
                bkey = bucket.bucket_key(user_id, file_id, original_name)
                await bucket.upload_file(dest, bkey)
                await crud.update_file(file_id, bucket_key=bkey)
            except Exception as exc:
                log.warning("Background bucket upload failed for %s: %s", file_id, exc)
        asyncio.create_task(_bg_upload())

        # Auto-extract if archive
        if _is_archive(original_name):
            await _auto_extract(user_id, file_id, chat_id)
        else:
            await tg.send_message(
                chat_id,
                f"✅ **URL download complete!**\n"
                f"📄 `{original_name}`\n"
                f"📦 Size: {_human(size)}\n"
                f"🔑 File ID: `{file_id}`\n\n"
                f"Use `/search {file_id} <pattern>` to search\n"
                f"Use `/unzip {file_id}` to extract archives",
            )
    except Exception as e:
        log.exception("URL download failed: %s", e)
        await crud.update_file(file_id, status=FileStatus.ERROR, error_message=str(e))
        await crud.update_job(
            job_id, status=JobStatus.FAILED, error_message=str(e)
        )
        try:
            await tg.send_message(chat_id, f"❌ URL download failed: `{e}`")
        except Exception:
            pass


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore
    return f"{n:.1f} PB"
