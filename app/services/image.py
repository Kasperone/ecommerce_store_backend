"""Image upload validation + R2 transport.

Pure helpers used by the upload router. Each function does one thing and
raises ``HTTPException`` with the appropriate 4xx status when input is
not acceptable.

Validation order is deliberate: magic-byte sniff happens on the FIRST
chunk so a spoofed extension + spoofed Content-Type + 1 GiB HTML payload
gets rejected after reading 8 KiB, not after buffering the whole thing.
"""

from io import BytesIO

import filetype
from botocore.client import BaseClient
from botocore.exceptions import ClientError
from fastapi import HTTPException, UploadFile, status
from PIL import Image, UnidentifiedImageError

from app.core.storage import build_public_url

ALLOWED_MIMES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})
MIME_TO_EXTENSION: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
_CHUNK_BYTES = 8 * 1024
_FILETYPE_HEAD_BYTES = 261  # `filetype.guess` reads at most this many bytes


def validate_magic_bytes(head: bytes) -> str:
    """Return the detected MIME type, or raise 400 if unsupported.

    ``head`` should be at least the first 261 bytes of the file; ``filetype``
    inspects the leading bytes and ignores the rest.
    """
    kind = filetype.guess(head)
    if kind is None or kind.mime not in ALLOWED_MIMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported image format. Allowed: JPEG, PNG, WebP.",
        )
    return kind.mime


async def read_with_size_cap(
    file: UploadFile,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> tuple[bytes, str]:
    """Stream ``file`` into memory, validating magic bytes on the first chunk
    and aborting at ``max_bytes`` before the whole payload lands in RAM.

    Returns ``(buffered_bytes, detected_mime)``.
    """
    body = bytearray()
    detected_mime: str | None = None
    while True:
        chunk = await file.read(_CHUNK_BYTES)
        if not chunk:
            break
        if detected_mime is None:
            detected_mime = validate_magic_bytes(chunk[:_FILETYPE_HEAD_BYTES])
        body.extend(chunk)
        if len(body) > max_bytes:
            mib = max_bytes / (1024 * 1024)
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Image exceeds {mib:.0f} MiB.",
            )
    if detected_mime is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty upload.",
        )
    return bytes(body), detected_mime


def capture_dimensions(buffered: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` in pixels. 400 if Pillow can't decode."""
    try:
        with Image.open(BytesIO(buffered)) as img:
            return img.size
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not read image dimensions.",
        ) from exc


def upload_to_r2(
    client: BaseClient,
    *,
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
) -> str:
    """Upload ``body`` to R2 and return the public URL."""
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable",
    )
    return build_public_url(key)


def delete_from_r2(client: BaseClient, *, bucket: str, key: str) -> bool:
    """Best-effort delete. Returns False on R2 error so the caller can log
    and proceed with the DB row removal — an R2 outage shouldn't strand the
    row."""
    try:
        client.delete_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False
