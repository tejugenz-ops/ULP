"""Archive decompression worker using 7z (handles encrypted ZIPs, RAR, etc.)."""

import asyncio
import logging
import uuid
from pathlib import Path

from bot.db import crud
from bot.db.models import FileStatus, JobStatus, JobType
from bot.storage import bucket, local

log = logging.getLogger(__name__)


async def extract_archive(
    ctx: dict,
    *,
    user_id: int,
    file_id: str,
    password: str | None = None,
    job_id: str,
    chat_id: int,
) -> None:
    """Extract an archive file using 7z."""
    from bot.telegram.bot import app as tg

    await crud.update_job(job_id, status=JobStatus.RUNNING, progress=0)

    try:
        file_record = await crud.get_file(file_id)
        if not file_record or not file_record.local_path:
            await tg.send_message(chat_id, "❌ File not found on disk.")
            await crud.update_job(job_id, status=JobStatus.FAILED, error_message="File not on disk")
            return

        filepath = Path(file_record.local_path)

        # Restore from bucket if needed
        if not filepath.exists() and file_record.bucket_key:
            dest_dir = local.user_dir(user_id, str(file_id))
            filepath = dest_dir / file_record.original_name
            await tg.send_message(chat_id, "📥 Restoring file from storage...")
            await bucket.download_file(file_record.bucket_key, filepath)
            await crud.update_file(file_id, local_path=str(filepath))

        if not filepath.exists():
            await tg.send_message(chat_id, "❌ File not found.")
            await crud.update_job(job_id, status=JobStatus.FAILED, error_message="File missing")
            return

        # Create extraction output directory
        extract_dir = filepath.parent / f"{filepath.stem}_extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)

        # Build 7z command
        cmd = ["7z", "x", str(filepath), f"-o{extract_dir}", "-y"]
        if password:
            cmd.append(f"-p{password}")
        else:
            cmd.append("-p")  # empty password = no password prompt hang

        await tg.send_message(chat_id, "📦 Extracting archive...")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            output = stdout.decode(errors="replace")[-500:]
            raise RuntimeError(f"7z extraction failed (code {proc.returncode}):\n{output}")

        # List extracted files
        extracted_files = []
        total_size = 0
        for p in extract_dir.rglob("*"):
            if p.is_file():
                sz = p.stat().st_size
                total_size += sz
                extracted_files.append({"name": str(p.relative_to(extract_dir)), "size": sz})

        # Register extracted files in DB
        registered = []
        for ef in extracted_files[:50]:  # cap to avoid DB spam
            child_id = str(uuid.uuid4())
            child_path = extract_dir / ef["name"]
            child_bkey = bucket.bucket_key(user_id, child_id, ef["name"].replace("/", "_"))

            await bucket.upload_file(child_path, child_bkey)

            child = await crud.create_file(
                user_id=user_id,
                original_name=ef["name"],
                size_bytes=ef["size"],
                parent_id=uuid.UUID(file_id),
            )
            await crud.update_file(
                child.id,
                status=FileStatus.READY,
                local_path=str(child_path),
                bucket_key=child_bkey,
            )
            registered.append(f"📄 `{ef['name']}` ({_human(ef['size'])})")

        await crud.update_job(job_id, status=JobStatus.COMPLETED, progress=100)

        # Build result message
        msg_lines = [
            f"✅ **Archive extracted!**",
            f"📦 {len(extracted_files)} file(s), {_human(total_size)} total\n",
        ]
        if registered:
            msg_lines.append("**Extracted files:**")
            msg_lines.extend(registered[:20])
            if len(registered) > 20:
                msg_lines.append(f"... and {len(registered) - 20} more")

        msg_lines.append(f"\nUse `/search <file_id> <pattern>` to search extracted files")
        await tg.send_message(chat_id, "\n".join(msg_lines))

    except Exception as e:
        log.exception("Extraction failed: %s", e)
        await crud.update_job(job_id, status=JobStatus.FAILED, error_message=str(e))
        try:
            from bot.telegram.bot import app as tg
            await tg.send_message(chat_id, f"❌ Extraction failed: `{e}`")
        except Exception:
            pass


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore
    return f"{n:.1f} PB"
