"""Local volume storage operations (Railway NVMe SSD)."""

import asyncio
import os
import shutil
from pathlib import Path

from bot.config import PROCESSING_DIR, VOLUME_MAX_GB, VOLUME_WARN_GB


def user_dir(user_id: int, file_id: str) -> Path:
    """Return the processing directory for a specific user/file."""
    p = PROCESSING_DIR / str(user_id) / file_id
    p.mkdir(parents=True, exist_ok=True)
    return p


async def volume_usage_gb() -> float:
    """Return current volume usage in GB (runs in thread to avoid blocking)."""
    loop = asyncio.get_running_loop()
    usage = await loop.run_in_executor(None, shutil.disk_usage, str(PROCESSING_DIR))
    return usage.used / (1024**3)


async def volume_free_gb() -> float:
    usage = await loop_disk_usage()
    return usage.free / (1024**3)


async def loop_disk_usage():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, shutil.disk_usage, str(PROCESSING_DIR))


async def is_volume_critical() -> bool:
    return (await volume_usage_gb()) >= VOLUME_MAX_GB


async def is_volume_warning() -> bool:
    return (await volume_usage_gb()) >= VOLUME_WARN_GB


async def delete_path(path: Path) -> None:
    """Delete a file or directory tree."""
    loop = asyncio.get_running_loop()
    if path.is_dir():
        await loop.run_in_executor(None, shutil.rmtree, str(path))
    elif path.is_file():
        await loop.run_in_executor(None, os.remove, str(path))


async def get_file_size(path: Path) -> int:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, os.path.getsize, str(path))


async def list_dir_contents(path: Path) -> list[dict]:
    """List files in a directory with sizes."""
    loop = asyncio.get_running_loop()

    def _list():
        items = []
        for entry in path.iterdir():
            items.append(
                {
                    "name": entry.name,
                    "size": entry.stat().st_size if entry.is_file() else 0,
                    "is_dir": entry.is_dir(),
                }
            )
        return items

    return await loop.run_in_executor(None, _list)
