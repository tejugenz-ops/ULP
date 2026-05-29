import os
from pathlib import Path


# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8627850287:AAGSNZx7Ku2mCi6u_1DyJXd7BHV2P0TvmAk",
)
TELEGRAM_API_ID: int = int(os.environ.get("TELEGRAM_API_ID", "25621841"))
TELEGRAM_API_HASH: str = os.environ.get(
    "TELEGRAM_API_HASH",
    "083efc80016252b6b88bc476bb4ea724",
)

# --- Database ---
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:jiZolpzaXofeqmIdBTeDtJNatjFKMLZb@postgres.railway.internal:5432/railway",
)

# --- Redis ---
REDIS_URL: str = os.environ.get(
    "REDIS_URL",
    "redis://default:AkEyubENAEOqrSziAIkpLFTiwqdtkQeq@redis.railway.internal:6379",
)

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
    os.environ.get("MAX_CONCURRENT_TRANSMISSIONS", "10")
)
TG_DOWNLOAD_WORKERS: int = int(os.environ.get("TG_DOWNLOAD_WORKERS", "8"))

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
ARQ_MAX_JOBS: int = int(os.environ.get("ARQ_MAX_JOBS", "20"))

# --- Scan workers (parallel ripgrep processes) ---
SCAN_WORKERS: int = int(os.environ.get("SCAN_WORKERS", "64"))

# --- Multi-worker scaling ---
# WORKER_ID=0  → bot service (polls Telegram + light ARQ worker on arq:w0)
# WORKER_ID=1..N → dedicated download workers (own Pyrogram session, queue arq:wN)
# *** Set WORKER_ID manually per Railway service (0 for bot, 1-10 for workers) ***
WORKER_ID: int = int(os.environ.get("WORKER_ID", "0"))

# WORKER_COUNT hardcoded to 10 — all services use the same value.
# Override with WORKER_COUNT env var only if you change the number of workers.
WORKER_COUNT: int = int(os.environ.get("WORKER_COUNT", "10"))
