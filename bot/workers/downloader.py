"""Download workers — Telegram files and URL downloads via aria2c."""

import asyncio
import json
import logging
import re
from pathlib import Path

from bot.db import crud
from bot.db.models import FileStatus, JobStatus
from bot.storage import bucket, local

log = logging.getLogger(__name__)


async def download_telegram_file(
    ctx: dict,
    *,
    user_id: int,
    file_id: str,
    telegram_file_id: str,
    original_name: str,
    job_id: str,
    chat_id: int,
    status_message_id: int,
) -> None:
    """Download a file from Telegram via Pyrogram (MTProto, chunked)."""
    from bot.telegram.bot import app as tg
    from bot.telegram.progress import ProgressTracker

    dest_dir = local.user_dir(user_id, file_id)
    dest = dest_dir / original_name

    await crud.update_job(job_id, status=JobStatus.RUNNING, progress=0)
    await crud.update_file(file_id, status=FileStatus.DOWNLOADING)

    try:
        # Get status message for progress updates
        msg = await tg.get_messages(chat_id, status_message_id)
        tracker = ProgressTracker(msg, action="⬇️ Downloading from Telegram")

        await tg.download_media(
            telegram_file_id,
            file_name=str(dest),
            progress=tracker,
        )

        size = await local.get_file_size(dest)

        # Move to bucket for permanent storage
        bkey = bucket.bucket_key(user_id, file_id, original_name)
        await bucket.upload_file(dest, bkey)

        await crud.update_file(
            file_id,
            status=FileStatus.READY,
            local_path=str(dest),
            bucket_key=bkey,
            size_bytes=size,
        )
        await crud.update_job(job_id, status=JobStatus.COMPLETED, progress=100)

        await tg.send_message(
            chat_id,
            f"✅ **Download complete!**\n"
            f"📄 `{original_name}`\n"
            f"📦 Size: {_human(size)}\n"
            f"🔑 File ID: `{file_id}`\n\n"
            f"Use `/search {file_id} <pattern>` to search\n"
            f"Use `/unzip {file_id}` to extract archives",
        )
    except Exception as e:
        log.exception("Telegram download failed: %s", e)
        await crud.update_file(file_id, status=FileStatus.ERROR, error_message=str(e))
        await crud.update_job(
            job_id, status=JobStatus.FAILED, error_message=str(e)
        )
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

        bkey = bucket.bucket_key(user_id, file_id, original_name)
        await bucket.upload_file(dest, bkey)

        await crud.update_file(
            file_id,
            status=FileStatus.READY,
            local_path=str(dest),
            bucket_key=bkey,
            size_bytes=size,
        )
        await crud.update_job(job_id, status=JobStatus.COMPLETED, progress=100)

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
