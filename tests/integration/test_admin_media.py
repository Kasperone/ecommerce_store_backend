"""Integration tests for the stateless admin media upload endpoint.

``POST /api/v1/admin/media`` stores an image to R2 and returns its public URL
plus metadata — no DB row (CMS page-builder image picker, findings plan F2).

The R2 transport is mocked with ``moto[s3]`` (no network), mirroring the
product-image upload tests. Each test:
1. enters ``mock_aws()`` so boto3 calls are intercepted,
2. creates the test bucket,
3. monkeypatches ``settings.R2_*`` so the route sees usable config,
4. monkeypatches ``get_r2_client`` so the route's inline call reaches the mock.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

import boto3
import pytest
from botocore.client import BaseClient
from moto import mock_aws
from PIL import Image

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import UserRole

TEST_BUCKET = "test-bucket"
TEST_PUBLIC_URL = "https://cdn.test.example.com"

MEDIA_URL = "/api/v1/admin/media"


@pytest.fixture
def r2_mock(monkeypatch) -> Iterator[BaseClient]:
    """Mock R2 via moto + monkeypatch the inline ``get_r2_client`` call.

    The route calls ``get_r2_client()`` inline after magic-byte validation so a
    spoofed extension surfaces as 400 (not 503) when R2 is unconfigured;
    monkeypatching the imported reference is the equivalent hook.
    """
    monkeypatch.setattr("app.core.config.settings.R2_BUCKET_NAME", TEST_BUCKET)
    monkeypatch.setattr("app.core.config.settings.R2_PUBLIC_URL", TEST_PUBLIC_URL)
    monkeypatch.setattr("app.core.config.settings.R2_ACCOUNT_ID", "test-account")
    monkeypatch.setattr("app.core.config.settings.R2_ACCESS_KEY_ID", "test-key")
    monkeypatch.setattr("app.core.config.settings.R2_SECRET_ACCESS_KEY", "x" * 64)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=TEST_BUCKET)
        monkeypatch.setattr("app.api.v1.admin_media.get_r2_client", lambda: client)
        yield client


def _admin_headers(user_factory, email: str = "admin@media.test") -> dict:
    admin = user_factory(role=UserRole.ADMIN, email=email)
    token, _, _ = create_access_token(subject=str(admin.id))
    return {"Authorization": f"Bearer {token}"}


def _png_bytes(width: int = 100, height: int = 100, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(width: int = 100, height: int = 100, color: str = "blue") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


async def test_media_upload_happy_path_returns_url_dims_bytes_mime(
    client, db_session, user_factory, r2_mock
):
    headers = _admin_headers(user_factory)

    response = await client.post(
        MEDIA_URL,
        files={"file": ("hero.png", _png_bytes(120, 80), "image/png")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["width"] == 120
    assert body["height"] == 80
    assert body["mime"] == "image/png"
    assert body["bytes"] > 0
    assert body["url"].startswith(TEST_PUBLIC_URL + "/cms/")
    assert body["url"].endswith(".png")

    # Stateless: the object lives in R2 under the cms/ prefix.
    key = body["url"].removeprefix(TEST_PUBLIC_URL + "/")
    assert key.startswith("cms/")
    assert r2_mock.head_object(Bucket=TEST_BUCKET, Key=key)["ContentLength"] == body["bytes"]


async def test_media_upload_writes_audit_event(client, db_session, user_factory, r2_mock):
    headers = _admin_headers(user_factory)

    response = await client.post(
        MEDIA_URL,
        files={"file": ("pic.jpg", _jpeg_bytes(64, 64), "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    url = response.json()["url"]

    events = (
        db_session.query(AdminAuditEvent).filter(AdminAuditEvent.resource_type == "media").all()
    )
    assert len(events) == 1
    assert events[0].action == "create"
    assert events[0].payload["url"] == url


async def test_media_upload_persists_dimensions(client, user_factory, r2_mock):
    headers = _admin_headers(user_factory)
    response = await client.post(
        MEDIA_URL,
        files={"file": ("size.jpg", _jpeg_bytes(800, 600), "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["width"], body["height"]) == (800, 600)
    assert body["mime"] == "image/jpeg"


# ---------------------------------------------------------------------------
# rejection paths
# ---------------------------------------------------------------------------


async def test_media_upload_rejects_spoofed_extension(client, user_factory, r2_mock):
    """A spoofed extension + bad magic bytes is a 400, never reaching R2."""
    headers = _admin_headers(user_factory)
    response = await client.post(
        MEDIA_URL,
        files={"file": ("evil.png", b"<html>not really a png</html>", "image/png")},
        headers=headers,
    )
    assert response.status_code == 400
    assert "Unsupported image format" in response.json()["detail"]


async def test_media_upload_magic_byte_check_runs_before_r2_unconfigured(
    client, user_factory, monkeypatch
):
    """Spoofed bytes surface as 400 even with no R2 creds (400-before-503)."""
    for field in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
        "R2_PUBLIC_URL",
    ):
        monkeypatch.setattr(f"app.core.config.settings.{field}", "")

    headers = _admin_headers(user_factory)
    response = await client.post(
        MEDIA_URL,
        files={"file": ("evil.jpg", b"<html>not really a jpeg</html>", "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 400
    assert "Unsupported image format" in response.json()["detail"]


async def test_media_upload_valid_bytes_yields_503_when_r2_unconfigured(
    client, user_factory, monkeypatch
):
    """A valid image with no R2 creds surfaces 503 — after validation."""
    for field in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
        "R2_PUBLIC_URL",
    ):
        monkeypatch.setattr(f"app.core.config.settings.{field}", "")

    headers = _admin_headers(user_factory)
    response = await client.post(
        MEDIA_URL,
        files={"file": ("ok.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    assert response.status_code == 503
    assert "R2 not configured" in response.json()["detail"]


async def test_media_upload_rejects_oversized(client, user_factory, r2_mock):
    """6 MiB payload trips the streaming size cap with 413 and stores nothing."""
    headers = _admin_headers(user_factory)
    body = _png_bytes(8, 8) + b"\x00" * (6 * 1024 * 1024)

    response = await client.post(
        MEDIA_URL,
        files={"file": ("big.png", body, "image/png")},
        headers=headers,
    )
    assert response.status_code == 413
    assert "MiB" in response.json()["detail"]

    objs = r2_mock.list_objects_v2(Bucket=TEST_BUCKET).get("Contents", [])
    assert objs == []


# ---------------------------------------------------------------------------
# auth gating
# ---------------------------------------------------------------------------


async def test_media_upload_unauthenticated_rejected(client, r2_mock):
    response = await client.post(
        MEDIA_URL,
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 401


async def test_media_upload_non_admin_rejected(client, user_factory, r2_mock):
    customer = user_factory(role=UserRole.CUSTOMER, email="c@media.test")
    token, _, _ = create_access_token(subject=str(customer.id))
    response = await client.post(
        MEDIA_URL,
        files={"file": ("p.png", _png_bytes(), "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
