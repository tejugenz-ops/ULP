"""Text search/extraction worker using ripgrep for blazing-fast search."""

import asyncio
import json
import logging
from pathlib import Path

from bot.db import crud
from bot.db.models import JobStatus

log = logging.getLogger(__name__)

MAX_RESULTS = 500
MAX_MESSAGE_LEN = 4000  # Telegram message limit ~4096


async def search_file(
    ctx: dict,
    *,
    user_id: int,
    file_id: str,
    pattern: str,
    job_id: str,
    chat_id: int,
) -> None:
    """
    Search a file using ripgrep.
    Streams results back to user as paginated Telegram messages.
    """
    from bot.telegram.bot import app as tg

    await crud.update_job(job_id, status=JobStatus.RUNNING, progress=0)

    try:
        file_record = await crud.get_file(file_id)
        if not file_record or not file_record.local_path:
            await tg.send_message(chat_id, "❌ File not found on disk. It may have been moved to storage.")
            await crud.update_job(job_id, status=JobStatus.FAILED, error_message="File not on disk")
            return

        filepath = Path(file_record.local_path)

        # If file was cleaned from volume, try to restore from bucket
        if not filepath.exists() and file_record.bucket_key:
            from bot.storage import bucket, local

            dest_dir = local.user_dir(user_id, str(file_id))
            filepath = dest_dir / file_record.original_name
            await tg.send_message(chat_id, "📥 Restoring file from storage...")
            await bucket.download_file(file_record.bucket_key, filepath)
            await crud.update_file(file_id, local_path=str(filepath))

        if not filepath.exists():
            await tg.send_message(chat_id, "❌ File not found.")
            await crud.update_job(job_id, status=JobStatus.FAILED, error_message="File missing")
            return

        # Run ripgrep — JSON output, max results cap, case-insensitive
        proc = await asyncio.create_subprocess_exec(
            "rg",
            "--json",
            "--max-count", str(MAX_RESULTS),
            "-i",  # case insensitive
            "--no-messages",  # suppress file permission errors
            pattern,
            str(filepath),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        matches: list[str] = []
        for line in stdout.decode(errors="replace").splitlines():
            try:
                obj = json.loads(line)
                if obj.get("type") == "match":
                    data = obj["data"]
                    line_num = data["line_number"]
                    text = data["lines"]["text"].rstrip()
                    # Truncate very long lines
                    if len(text) > 200:
                        text = text[:200] + "..."
                    matches.append(f"**L{line_num}:** `{text}`")
            except (json.JSONDecodeError, KeyError):
                continue

        await crud.update_job(job_id, progress=100, status=JobStatus.COMPLETED)

        if not matches:
            await tg.send_message(
                chat_id,
                f"🔍 **No matches found** for `{pattern}` in `{file_record.original_name}`",
            )
            return

        # Send results in paginated messages
        header = f"🔍 **{len(matches)} match(es)** for `{pattern}` in `{file_record.original_name}`\n\n"
        chunks = _paginate(matches, header)

        for i, chunk in enumerate(chunks):
            await tg.send_message(chat_id, chunk)
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)  # avoid flood

    except Exception as e:
        log.exception("Search failed: %s", e)
        await crud.update_job(job_id, status=JobStatus.FAILED, error_message=str(e))
        try:
            from bot.telegram.bot import app as tg
            await tg.send_message(chat_id, f"❌ Search failed: `{e}`")
        except Exception:
            pass


def _paginate(lines: list[str], header: str) -> list[str]:
    """Split lines into message-sized chunks."""
    pages: list[str] = []
    current = header
    for line in lines:
        if len(current) + len(line) + 2 > MAX_MESSAGE_LEN:
            pages.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        pages.append(current)
    return pages
