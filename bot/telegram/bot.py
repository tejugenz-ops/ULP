"""Pyrogram MTProto client setup."""

from pyrogram import Client

from bot.config import (
    MAX_CONCURRENT_TRANSMISSIONS,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_BOT_TOKEN,
)

app = Client(
    name="file_bot",
    api_id=TELEGRAM_API_ID,
    api_hash=TELEGRAM_API_HASH,
    bot_token=TELEGRAM_BOT_TOKEN,
    workers=16,
    max_concurrent_transmissions=MAX_CONCURRENT_TRANSMISSIONS,
    workdir="/data",
)
