"""Archive decompression worker using 7z (handles encrypted ZIPs, RAR, etc.)."""

import asyncio
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db import crud
from bot.db.models import FileStatus, JobStatus, JobType
from bot.storage import bucket, local

log = logging.getLogger(__name__)

# Pending password requests: file_id → {"job_id", "user_id", "chat_id", "filepath", "extract_dir"}
pending_passwords: dict[str, dict] = {}


def _needs_password(output: str) -> bool:
    """Detect if 7z output indicates the archive needs a password."""
    low = output.lower()
    return any(s in low for s in (
        "wrong password",
        "enter password",
        "encrypted",
        "can not open encrypted archive",
        "headers error",
    ))


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
            await crud.update_file(file_id, status=FileStatus.ERROR)
            await crud.update_job(job_id, status=JobStatus.FAILED, error_message="File missing")
            return

        if filepath.stat().st_size == 0:
            log.warning("Skipping 0-byte archive: %s", filepath)
            await crud.update_file(file_id, status=FileStatus.ERROR)
            await crud.update_job(job_id, status=JobStatus.FAILED, error_message="Archive is 0 bytes")
            try:
                await tg.send_message(chat_id, f"⚠️ `{file_record.original_name}` is 0 bytes — skipped.")
            except Exception:
                pass
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

        await tg.send_message(
            chat_id,
            f"📦 Extracting `{file_record.original_name}`...",
        )

        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
        )
        output = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")

        if proc.returncode != 0:
            # Check if password is needed
            if _needs_password(output):
                # Clean up failed extraction attempt
                if extract_dir.exists():
                    shutil.rmtree(extract_dir, ignore_errors=True)

                # Store pending state so handler can retry with password
                pending_passwords[file_id] = {
                    "job_id": job_id,
                    "user_id": user_id,
                    "chat_id": chat_id,
                }

                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "❌ Cancel & Delete",
                        callback_data=f"archcancel:{file_id}",
                    )],
                ])
                await tg.send_message(
                    chat_id,
                    f"🔒 **Password required** for `{file_record.original_name}`\n\n"
                    f"Reply with the password, or tap Cancel to delete this archive.",
                    reply_markup=kb,
                )
                # Mark job as waiting (don't fail it)
                await crud.update_job(job_id, status=JobStatus.QUEUED, progress=0)
                return

            raise RuntimeError(f"7z extraction failed (code {proc.returncode}):\n{output[-500:]}")

        # List extracted files
        extracted_files = []
        total_size = 0
        for p in extract_dir.rglob("*"):
            if p.is_file():
                sz = p.stat().st_size
                total_size += sz
                extracted_files.append({"name": str(p.relative_to(extract_dir)), "size": sz})

        # Register extracted files in DB (no cap — register all)
        registered = []
        for ef in extracted_files:
            child_path = extract_dir / ef["name"]

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
            )

            # Upload to bucket in background
            child_id_str = str(child.id)
            child_bkey = bucket.bucket_key(user_id, child_id_str, ef["name"].replace("/", "_"))

            async def _bg_child_upload(p=child_path, k=child_bkey, cid=child_id_str):
                try:
                    await bucket.upload_file(p, k)
                    await crud.update_file(cid, bucket_key=k)
                except Exception as exc:
                    log.warning("Child bucket upload failed: %s", exc)
            asyncio.create_task(_bg_child_upload())

            registered.append(f"📄 `{ef['name']}` ({_human(ef['size'])})")

        await crud.update_job(job_id, status=JobStatus.COMPLETED, progress=100)

        # Delete the original archive from disk, bucket, and DB
        await _delete_archive(file_id, filepath, file_record.bucket_key)

        # Build result message
        msg_lines = [
            f"✅ **Archive extracted & deleted!**",
            f"📦 {len(extracted_files)} file(s), {_human(total_size)} total\n",
        ]
        if registered:
            msg_lines.append("**Extracted files:**")
            msg_lines.extend(registered[:20])
            if len(registered) > 20:
                msg_lines.append(f"... and {len(registered) - 20} more")

        msg_lines.append(f"\nUse `/scan` to search all files")
        await tg.send_message(chat_id, "\n".join(msg_lines))

    except Exception as e:
        log.exception("Extraction failed: %s", e)
        await crud.update_file(file_id, status=FileStatus.ERROR)
        await crud.update_job(job_id, status=JobStatus.FAILED, error_message=str(e))
        try:
            from bot.telegram.bot import app as tg
            await tg.send_message(chat_id, f"❌ Extraction failed: `{e}`")
        except Exception:
            pass


async def _delete_archive(file_id: str, filepath: Path, bucket_key: str | None) -> None:
    """Remove the original archive file from disk, bucket and DB."""
    try:
        if filepath.exists():
            filepath.unlink()
        # Also remove the parent dir if now empty
        parent = filepath.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except Exception as exc:
        log.warning("Failed to delete archive from disk: %s", exc)

    if bucket_key:
        try:
            await bucket.delete_object(bucket_key)
        except Exception as exc:
            log.warning("Failed to delete archive from bucket: %s", exc)

    try:
        await crud.delete_children(file_id)
        await crud.delete_file(file_id)
    except Exception as exc:
        log.warning("Failed to delete archive DB record: %s", exc)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore
    return f"{n:.1f} PB"
