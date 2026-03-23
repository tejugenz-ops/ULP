"""Text search/extraction worker using ripgrep for blazing-fast search."""

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

from bot.config import PROCESSING_DIR
from bot.db import crud
from bot.db.models import FileStatus, JobStatus

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
    status_message_id: int | None = None,
) -> None:
    """
    Search a file using ripgrep.
    Streams results back to user as paginated Telegram messages.
    """
    from bot.telegram.bot import app as tg

    await crud.update_job(job_id, status=JobStatus.RUNNING, progress=0)

    async def _set_progress(text: str) -> None:
        if status_message_id:
            try:
                await tg.edit_message_text(chat_id, status_message_id, text)
                return
            except Exception:
                pass
        await tg.send_message(chat_id, text)

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

        await _set_progress(
            f"🔍 Searching started\n"
            f"Pattern: `{pattern}`\n"
            f"File: `{file_record.original_name}`\n"
            "Progress: 20%"
        )

        # Run ripgrep — JSON output, max results cap, case-insensitive
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "rg", "--json",
                "--max-count", str(MAX_RESULTS),
                "-i",
                "--no-messages",
                pattern,
                str(filepath),
            ],
            capture_output=True,
        )

        matches: list[str] = []
        for line in result.stdout.decode(errors="replace").splitlines():
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
        await _set_progress(
            f"✅ Search completed\n"
            f"Pattern: `{pattern}`\n"
            f"File: `{file_record.original_name}`\n"
            f"Progress: 100% | Matches: {len(matches)}"
        )

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


def _safe_keyword_filename(keyword: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", keyword.strip())
    return cleaned[:80] or "keyword"


def _human_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024  # type: ignore
    return f"{n:.2f} PB"


def _extract_credential(line: str) -> str | None:
    """
    Extract login:password from a ULP/logs line.

    ULP format: URL:login:password  (or just login:password)
    We take the last two colon-separated segments.
    Returns 'login:password' or None if not enough parts.
    """
    parts = line.split(":")
    if len(parts) < 2:
        return None
    login = parts[-2].strip()
    password = parts[-1].strip()
    if not login or not password:
        return None
    return f"{login}:{password}"


def _bar(pct: float, length: int = 20) -> str:
    filled = int(length * pct / 100)
    return "█" * filled + "░" * (length - filled)


async def extract_keywords_batch(
    ctx: dict,
    *,
    user_id: int,
    file_ids: list[str],
    keywords: list[str],
    job_id: str,
    chat_id: int,
    status_message_id: int | None = None,
    mode: str,
) -> None:
    """Scan multiple files for multiple keywords using ripgrep. Extract credentials, deduplicate."""
    from bot.telegram.bot import app as tg

    await crud.update_job(job_id, status=JobStatus.RUNNING, progress=0)

    current_status_message_id = status_message_id
    last_progress_text = ""

    async def _set_progress(text: str) -> None:
        nonlocal current_status_message_id, last_progress_text

        if text == last_progress_text:
            return

        if current_status_message_id:
            try:
                await tg.edit_message_text(chat_id, current_status_message_id, text)
                last_progress_text = text
                return
            except Exception as e:
                if "MESSAGE_NOT_MODIFIED" in str(e):
                    return

        sent = await tg.send_message(chat_id, text)
        current_status_message_id = sent.id
        last_progress_text = text

    try:
        if not file_ids or not keywords:
            await crud.update_job(
                job_id,
                status=JobStatus.FAILED,
                error_message="No file_ids or keywords provided",
            )
            await tg.send_message(chat_id, "❌ Cannot start: missing files or keywords.")
            return

        # Normalize keywords
        deduped_keywords: list[str] = []
        seen_kw: set[str] = set()
        for kw in keywords:
            norm = kw.strip().lower()
            if not norm or norm in seen_kw:
                continue
            seen_kw.add(norm)
            deduped_keywords.append(kw.strip())

        # keyword -> set of credentials (deduplication)
        keyword_creds: dict[str, set[str]] = {kw: set() for kw in deduped_keywords}

        total_files = len(file_ids)

        await _set_progress(
            f"⬇️ **Downloading**\n"
            f"{_bar(0)} 0%\n"
            f"Files: 0/{total_files} ready\n"
            f"Downloaded: 0 B / ?"
        )

        # ─── Phase 1: Wait for downloads ─────────────────────────
        wait_cycles = 0
        while True:
            wait_cycles += 1
            ready_count = 0
            error_count = 0
            ready_bytes = 0
            total_bytes = 0
            for fid in file_ids:
                fr = await crud.get_file(fid)
                if not fr:
                    continue
                fsize = fr.size_bytes or 0
                total_bytes += fsize
                if fr.status == FileStatus.READY and fr.local_path:
                    ready_count += 1
                    ready_bytes += fsize
                elif fr.status == FileStatus.ERROR:
                    error_count += 1

            if ready_count + error_count >= total_files:
                break

            pct = int((ready_bytes / max(1, total_bytes)) * 100) if total_bytes else 0
            err_line = f"\n⚠️ Failed: {error_count}" if error_count else ""
            await _set_progress(
                f"⬇️ **Downloading**\n"
                f"{_bar(pct)} {pct}%\n"
                f"Files: {ready_count}/{total_files} ready\n"
                f"Downloaded: {_human_bytes(ready_bytes)} / {_human_bytes(total_bytes)}"
                f"{err_line}"
            )
            await asyncio.sleep(2)

            if wait_cycles > 600:  # ~20 min safety stop
                break

        # Gather file info for scanning phase
        scan_files: list[tuple[str, Path, int]] = []  # (file_id, path, size)
        for fid in file_ids:
            fr = await crud.get_file(fid)
            if not fr:
                continue

            filepath = Path(fr.local_path) if fr.local_path else None

            if (not filepath or not filepath.exists()) and fr.bucket_key:
                from bot.storage import bucket, local
                dest_dir = local.user_dir(user_id, str(fr.id))
                filepath = dest_dir / fr.original_name
                await bucket.download_file(fr.bucket_key, filepath)
                await crud.update_file(fr.id, local_path=str(filepath))

            if filepath and filepath.exists():
                scan_files.append((str(fr.id), filepath, fr.size_bytes or 0))

        total_scan_bytes = sum(s for _, _, s in scan_files)

        # ─── Phase 2: Scan with ripgrep ──────────────────────────

        # Write keywords to temp file for ripgrep -f
        import tempfile
        kw_tmp = Path(tempfile.mktemp(suffix=".txt", prefix="kw_"))
        try:
            kw_tmp.write_text("\n".join(deduped_keywords) + "\n", encoding="utf-8")

            scanned_bytes = 0
            for file_idx, (fid, filepath, fsize) in enumerate(scan_files, start=1):
                pct = int((scanned_bytes / max(1, total_scan_bytes)) * 100)
                kw_summary = " | ".join(
                    f"{kw}: {len(keyword_creds[kw])}" for kw in deduped_keywords
                )
                await _set_progress(
                    f"🔎 **Scanning**\n"
                    f"{_bar(pct)} {pct}%\n"
                    f"Scanned: {_human_bytes(scanned_bytes)} / {_human_bytes(total_scan_bytes)}\n"
                    f"File {file_idx}/{len(scan_files)}: {filepath.name}\n"
                    f"{kw_summary}"
                )

                # Run ripgrep: case-insensitive, patterns from file, no heading
                rg_result = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "rg", "-i",
                        "--no-heading",
                        "--no-line-number",
                        "--no-messages",
                        "-f", str(kw_tmp),
                        str(filepath),
                    ],
                    capture_output=True,
                )

                # Categorize each matched line into keywords and extract credentials
                for raw_line in rg_result.stdout.decode(errors="replace").splitlines():
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    line_lower = stripped.lower()
                    cred = _extract_credential(stripped)
                    if not cred:
                        continue
                    for kw in deduped_keywords:
                        if kw.lower() in line_lower:
                            keyword_creds[kw].add(cred)

                scanned_bytes += fsize

            # Final scan progress
            kw_summary = " | ".join(
                f"{kw}: {len(keyword_creds[kw])}" for kw in deduped_keywords
            )
            await _set_progress(
                f"🔎 **Scanning**\n"
                f"{_bar(100)} 100%\n"
                f"Scanned: {_human_bytes(total_scan_bytes)} / {_human_bytes(total_scan_bytes)}\n"
                f"Completed all {len(scan_files)} files\n"
                f"{kw_summary}"
            )
        finally:
            kw_tmp.unlink(missing_ok=True)

        # ─── Phase 3: Generate and send result files ─────────────

        out_dir = PROCESSING_DIR / str(user_id) / "keyword_results"
        out_dir.mkdir(parents=True, exist_ok=True)

        total_hits = 0
        for kw in deduped_keywords:
            creds = sorted(keyword_creds.get(kw, set()))
            total_hits += len(creds)

            out_file = out_dir / f"{_safe_keyword_filename(kw)}.txt"
            with out_file.open("w", encoding="utf-8") as f:
                if creds:
                    f.write("\n".join(creds))
                    f.write("\n")
                else:
                    f.write(f"No matches found for: {kw}\n")

            await tg.send_document(
                chat_id,
                str(out_file),
                caption=f"📄 `{kw}` — {len(creds)} result(s)",
            )
            await asyncio.sleep(0.3)

        # ─── Done: edit progress to final summary ────────────────

        await crud.update_job(
            job_id,
            status=JobStatus.COMPLETED,
            progress=100,
            result=f"keywords={len(deduped_keywords)}, files={len(scan_files)}, hits={total_hits}",
        )

        counts_lines = "\n".join(
            f"• {kw}: {len(keyword_creds.get(kw, set()))}"
            for kw in deduped_keywords
        )
        await _set_progress(
            f"✅ **Done**\n\n"
            f"Scanned **{len(scan_files)}** file(s) ({_human_bytes(total_scan_bytes)})\n"
            f"Keywords: **{len(deduped_keywords)}** | Results: **{total_hits:,}**\n\n"
            f"{counts_lines}"
        )

    except Exception as e:
        log.exception("extract_keywords_batch failed: %s", e)
        await crud.update_job(job_id, status=JobStatus.FAILED, error_message=str(e))
        try:
            await tg.send_message(chat_id, f"❌ Scan failed: `{e}`")
        except Exception:
            pass
