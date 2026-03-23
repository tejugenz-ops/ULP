"""S3-compatible Railway Storage Bucket operations."""

import asyncio
from pathlib import Path
from functools import lru_cache

import boto3
from botocore.config import Config as BotoConfig

from bot.config import (
    BUCKET_ACCESS_KEY,
    BUCKET_ENDPOINT,
    BUCKET_NAME,
    BUCKET_REGION,
    BUCKET_SECRET_KEY,
)


@lru_cache(maxsize=1)
def _client():
    return boto3.client(
        "s3",
        endpoint_url=BUCKET_ENDPOINT or None,
        aws_access_key_id=BUCKET_ACCESS_KEY,
        aws_secret_access_key=BUCKET_SECRET_KEY,
        region_name=BUCKET_REGION,
        config=BotoConfig(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "adaptive"},
        ),
    )


async def upload_file(local_path: Path, key: str) -> None:
    """Upload a local file to the bucket."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: _client().upload_file(str(local_path), BUCKET_NAME, key),
    )


async def download_file(key: str, local_path: Path) -> None:
    """Download a file from the bucket to local path."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: _client().download_file(BUCKET_NAME, key, str(local_path)),
    )


async def delete_object(key: str) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: _client().delete_object(Bucket=BUCKET_NAME, Key=key),
    )


async def generate_presigned_url(key: str, expires_in: int = 3600) -> str:
    """Generate a presigned download URL."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_NAME, "Key": key},
            ExpiresIn=expires_in,
        ),
    )


async def list_user_objects(user_id: int) -> list[dict]:
    """List all objects for a user."""
    prefix = f"users/{user_id}/"
    loop = asyncio.get_running_loop()

    def _list():
        resp = _client().list_objects_v2(Bucket=BUCKET_NAME, Prefix=prefix)
        return resp.get("Contents", [])

    return await loop.run_in_executor(None, _list)


def bucket_key(user_id: int, file_id: str, filename: str) -> str:
    """Generate a standard bucket key."""
    return f"users/{user_id}/{file_id}/{filename}"
