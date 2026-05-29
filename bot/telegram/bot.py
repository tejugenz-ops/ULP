"""Pyrogram MTProto client setup."""

from pyrogram import Client

from bot.config import (
    MAX_CONCURRENT_TRANSMISSIONS,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_BOT_TOKEN,
    WORKER_ID,
)

# Each service (bot + every dedicated worker) gets its own session file so
# Telegram treats them as independent MTProto connections with separate bandwidth.
# Bot service: file_bot_w0.session
# Worker N:    file_bot_wN.session
app = Client(
    name=f"file_bot_w{WORKER_ID}",
    api_id=TELEGRAM_API_ID,
    api_hash=TELEGRAM_API_HASH,
    bot_token=TELEGRAM_BOT_TOKEN,
    workers=4,
    max_concurrent_transmissions=MAX_CONCURRENT_TRANSMISSIONS,
    workdir="/data",
)
