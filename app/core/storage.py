"""Cloudflare R2 storage client.

R2 is S3-compatible; we talk to it via boto3 pointed at Cloudflare's
endpoint. The client is built per-request so each handler gets its own
boto3 client (boto3 clients are not guaranteed thread-safe across event
loops).

``get_r2_client`` is a FastAPI dependency so route handlers receive the
client via ``Depends(get_r2_client)`` — that integration is what lets
tests substitute a moto-mocked client via ``app.dependency_overrides``.
If any of the five R2 settings are empty, the dependency raises
``HTTPException(503)`` so dev environments without R2 configured can
still boot and serve every other endpoint.
"""

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from fastapi import HTTPException, status

from app.core.config import settings

_R2_REQUIRED_FIELDS = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
    "R2_PUBLIC_URL",
)


def get_r2_client() -> BaseClient:
    missing = [name for name in _R2_REQUIRED_FIELDS if not getattr(settings, name)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"R2 not configured — missing settings: {', '.join(missing)}. "
                "Set these in .env to enable product image uploads."
            ),
        )

    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def build_public_url(key: str) -> str:
    return f"{settings.R2_PUBLIC_URL.rstrip('/')}/{key.lstrip('/')}"
