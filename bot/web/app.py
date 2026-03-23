"""FastAPI web application — health check and upload portal."""

import os
import time
import uuid
from pathlib import Path

import aiofiles
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from bot.config import PROCESSING_DIR, UPLOAD_CHUNK_SIZE
from bot.db import crud
from bot.db.models import FileStatus, JobType
from bot.storage import bucket, local

web_app = FastAPI(title="File Bot Upload Portal")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ── Health check ─────────────────────────────────────────────────────


@web_app.get("/health")
async def health():
    return {"status": "ok"}


# ── Upload page ──────────────────────────────────────────────────────


@web_app.get("/upload/{token}", response_class=HTMLResponse)
async def upload_page(request: Request, token: str):
    """Serve the upload HTML page if token is valid."""
    from bot.telegram.handlers import upload_tokens

    entry = upload_tokens.get(token)
    if not entry or entry["expires"] < time.time():
        raise HTTPException(status_code=403, detail="Invalid or expired upload link.")

    return templates.TemplateResponse(
        "upload.html",
        {"request": request, "token": token},
    )


# ── Chunked upload endpoint ──────────────────────────────────────────


@web_app.post("/api/upload/{token}")
async def upload_file_api(token: str, file: UploadFile):
    """
    Receive a file upload via multipart form.
    Streams to disk in chunks to handle multi-GB files without memory issues.
    """
    from bot.telegram.handlers import upload_tokens

    entry = upload_tokens.get(token)
    if not entry or entry["expires"] < time.time():
        raise HTTPException(status_code=403, detail="Invalid or expired upload link.")

    user_id = entry["user_id"]
    original_name = file.filename or "upload"
    file_id = str(uuid.uuid4())

    dest_dir = local.user_dir(user_id, file_id)
    dest = dest_dir / original_name

    # Stream to disk in chunks
    total_written = 0
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            await out.write(chunk)
            total_written += len(chunk)

    # Register in DB
    file_record = await crud.create_file(
        user_id=user_id,
        original_name=original_name,
        size_bytes=total_written,
    )

    # Upload to bucket
    bkey = bucket.bucket_key(user_id, file_id, original_name)
    await bucket.upload_file(dest, bkey)

    await crud.update_file(
        file_record.id,
        status=FileStatus.READY,
        local_path=str(dest),
        bucket_key=bkey,
    )

    # Notify user via Telegram
    try:
        from bot.telegram.bot import app as tg

        await tg.send_message(
            user_id,
            f"✅ **Web upload complete!**\n"
            f"📄 `{original_name}`\n"
            f"📦 Size: {_human(total_written)}\n"
            f"🔑 File ID: `{file_record.id}`\n\n"
            f"Use `/search {file_record.id} <pattern>` to search\n"
            f"Use `/unzip {file_record.id}` to extract archives",
        )
    except Exception:
        pass

    # Remove used token
    upload_tokens.pop(token, None)

    return JSONResponse(
        {"status": "ok", "file_id": str(file_record.id), "size": total_written}
    )


# ── Resumable chunked upload (for very large files) ──────────────────


# Track in-progress chunked uploads: upload_session_id → {path, offset, ...}
_chunked_sessions: dict[str, dict] = {}


@web_app.post("/api/upload/{token}/init")
async def init_chunked_upload(token: str, request: Request):
    """Initialize a resumable chunked upload session."""
    from bot.telegram.handlers import upload_tokens

    entry = upload_tokens.get(token)
    if not entry or entry["expires"] < time.time():
        raise HTTPException(status_code=403, detail="Invalid or expired upload link.")

    body = await request.json()
    filename = body.get("filename", "upload")
    total_size = body.get("totalSize", 0)

    session_id = str(uuid.uuid4())
    user_id = entry["user_id"]
    file_id = str(uuid.uuid4())
    dest_dir = local.user_dir(user_id, file_id)
    dest = dest_dir / filename

    _chunked_sessions[session_id] = {
        "user_id": user_id,
        "file_id": file_id,
        "filename": filename,
        "dest": str(dest),
        "total_size": total_size,
        "offset": 0,
        "token": token,
    }

    return JSONResponse({"sessionId": session_id, "fileId": file_id})


@web_app.post("/api/upload/{token}/chunk/{session_id}")
async def upload_chunk(token: str, session_id: str, request: Request):
    """Receive a single chunk of a resumable upload."""
    session = _chunked_sessions.get(session_id)
    if not session or session["token"] != token:
        raise HTTPException(status_code=404, detail="Upload session not found.")

    data = await request.body()
    dest = Path(session["dest"])

    async with aiofiles.open(dest, "ab") as f:
        await f.write(data)

    session["offset"] += len(data)
    progress = (
        int(session["offset"] * 100 / session["total_size"])
        if session["total_size"]
        else 0
    )

    return JSONResponse({"offset": session["offset"], "progress": progress})


@web_app.post("/api/upload/{token}/complete/{session_id}")
async def complete_chunked_upload(token: str, session_id: str):
    """Finalize a chunked upload."""
    session = _chunked_sessions.pop(session_id, None)
    if not session:
        raise HTTPException(status_code=404, detail="Upload session not found.")

    from bot.telegram.handlers import upload_tokens

    user_id = session["user_id"]
    file_id = session["file_id"]
    filename = session["filename"]
    dest = Path(session["dest"])
    total = session["offset"]

    file_record = await crud.create_file(
        user_id=user_id,
        original_name=filename,
        size_bytes=total,
    )

    bkey = bucket.bucket_key(user_id, file_id, filename)
    await bucket.upload_file(dest, bkey)

    await crud.update_file(
        file_record.id,
        status=FileStatus.READY,
        local_path=str(dest),
        bucket_key=bkey,
    )

    # Notify user
    try:
        from bot.telegram.bot import app as tg

        await tg.send_message(
            user_id,
            f"✅ **Web upload complete!**\n"
            f"📄 `{filename}`\n"
            f"📦 Size: {_human(total)}\n"
            f"🔑 File ID: `{file_record.id}`",
        )
    except Exception:
        pass

    upload_tokens.pop(token, None)
    return JSONResponse({"status": "ok", "file_id": str(file_record.id), "size": total})


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore
    return f"{n:.1f} PB"
