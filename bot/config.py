import os
from pathlib import Path


# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_API_ID: int = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH: str = os.environ["TELEGRAM_API_HASH"]

# --- Database ---
DATABASE_URL: str = os.environ["DATABASE_URL"]

# --- Redis ---
REDIS_URL: str = os.environ["REDIS_URL"]

# --- S3 / Railway Storage Bucket ---
BUCKET_ENDPOINT: str = os.environ.get("BUCKET_ENDPOINT", "")
BUCKET_ACCESS_KEY: str = os.environ.get("BUCKET_ACCESS_KEY", "")
BUCKET_SECRET_KEY: str = os.environ.get("BUCKET_SECRET_KEY", "")
BUCKET_NAME: str = os.environ.get("BUCKET_NAME", "file-bot")
BUCKET_REGION: str = os.environ.get("BUCKET_REGION", "us-east-1")

# --- Storage paths ---
DATA_DIR: Path = Path(os.environ.get("DATA_DIR", "/data"))
PROCESSING_DIR: Path = DATA_DIR / "processing"
PROCESSING_DIR.mkdir(parents=True, exist_ok=True)

# --- Limits ---
MAX_FILE_SIZE_GB: int = int(os.environ.get("MAX_FILE_SIZE_GB", "50"))
VOLUME_WARN_GB: int = int(os.environ.get("VOLUME_WARN_GB", "200"))
VOLUME_MAX_GB: int = int(os.environ.get("VOLUME_MAX_GB", "240"))

# --- Pyrogram tuning ---
MAX_CONCURRENT_TRANSMISSIONS: int = int(
    os.environ.get("MAX_CONCURRENT_TRANSMISSIONS", "5")
)
TG_DOWNLOAD_WORKERS: int = int(os.environ.get("TG_DOWNLOAD_WORKERS", "4"))

# --- Web upload ---
UPLOAD_CHUNK_SIZE: int = int(os.environ.get("UPLOAD_CHUNK_SIZE", str(50 * 1024 * 1024)))  # 50 MB
UPLOAD_TOKEN_TTL: int = int(os.environ.get("UPLOAD_TOKEN_TTL", "3600"))  # 1 hour

# --- Access control ---
# Comma-separated Telegram user IDs; empty = allow all
_allowed = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = (
    {int(uid.strip()) for uid in _allowed.split(",") if uid.strip()}
    if _allowed
    else set()
)

# --- ARQ workers ---
ARQ_MAX_JOBS: int = int(os.environ.get("ARQ_MAX_JOBS", "50"))

# --- Scan workers (parallel ripgrep processes) ---
SCAN_WORKERS: int = int(os.environ.get("SCAN_WORKERS", "64"))
