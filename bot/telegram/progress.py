"""Real-time progress tracking for downloads and processing."""

import asyncio
import time

from pyrogram.types import Message


class ProgressTracker:
    """Edits a Telegram message with live progress updates."""

    def __init__(self, message: Message, action: str = "Downloading"):
        self.message = message
        self.action = action
        self._last_edit: float = 0
        self._edit_interval: float = 5.0  # seconds between message edits

    async def __call__(self, current: int, total: int) -> None:
        """Pyrogram-compatible progress callback."""
        now = time.monotonic()
        if now - self._last_edit < self._edit_interval and current < total:
            return

        self._last_edit = now
        pct = current * 100 / total if total else 0
        bar = self._bar(pct)
        size_cur = _human_size(current)
        size_tot = _human_size(total)

        text = (
            f"**{self.action}**\n"
            f"{bar} {pct:.1f}%\n"
            f"`{size_cur} / {size_tot}`"
        )
        try:
            await self.message.edit_text(text)
        except Exception:
            pass  # rate limited or message deleted

    @staticmethod
    def _bar(pct: float, length: int = 20) -> str:
        filled = int(length * pct / 100)
        return "█" * filled + "░" * (length - filled)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore
    return f"{n:.1f} PB"
