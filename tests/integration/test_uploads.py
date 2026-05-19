"""Integration tests for the R2 image upload pipeline.

Acceptance coverage for PR1 of the admin-catalog foundation plan
(``ecommerce_store/.claude/notes/plans/admin/in-progress/2026-05-18-admin-catalog-foundation.md``).

The R2 transport is mocked with ``moto[s3]`` — no network. Each test:
1. enters ``mock_aws()`` so boto3 calls are intercepted,
2. creates the test bucket,
3. monkeypatches ``settings.R2_*`` so the upload route sees usable config,
4. monkeypatches ``get_r2_client`` so the route's inline call reaches into the mock.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

import boto3
import pytest
from botocore.client import BaseClient
from botocore.exceptions import ClientError
from moto import mock_aws
from PIL import Image

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.product_image import ProductImage
from app.models.user import UserRole

TEST_BUCKET = "test-bucket"
TEST_PUBLIC_URL = "https://cdn.test.example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def r2_mock(monkeypatch) -> Iterator[BaseClient]:
    """Mock R2 via moto + monkeypatch the inline ``get_r2_client`` call.

    Yields the S3 client so tests can assert on stored objects directly.

    The upload route used to receive R2 via ``Depends(get_r2_client)`` (and
    tests overrode it via ``app.dependency_overrides``). Now the route
    calls ``get_r2_client()`` inline after magic-byte validation so a
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
        monkeypatch.setattr("app.api.v1.uploads.get_r2_client", lambda: client)
        yield client


def _admin_headers(user_factory, email: str = "admin@upload.test") -> dict:
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
# POST /products/{id}/images
# ---------------------------------------------------------------------------


async def test_upload_happy_path_writes_row_and_audit(
    client, db_session, product_factory, user_factory, r2_mock
):
    product = product_factory(slug="upload-happy")
    headers = _admin_headers(user_factory)

    response = await client.post(
        f"/api/v1/products/{product.id}/images",
        files={"file": ("photo.png", _png_bytes(120, 80), "image/png")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["product_id"] == product.id
    assert body["width"] == 120
    assert body["height"] == 80
    assert body["mime"] == "image/png"
    assert body["bytes"] > 0
    assert body["url"].startswith(TEST_PUBLIC_URL + "/products/")

    row = db_session.query(ProductImage).filter(ProductImage.id == body["id"]).one()
    assert row.width == 120
    assert row.mime == "image/png"

    audit = (
        db_session.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "product_image")
        .all()
    )
    assert len(audit) == 1
    assert audit[0].action == "create"
    assert audit[0].resource_id == row.id


async def test_upload_rejects_magic_byte_mismatch(client, product_factory, user_factory, r2_mock):
    product = product_factory(slug="upload-magic-bytes")
    headers = _admin_headers(user_factory)

    response = await client.post(
        f"/api/v1/products/{product.id}/images",
        files={"file": ("evil.jpg", b"<html>not really a jpeg</html>", "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 400
    assert "Unsupported image format" in response.json()["detail"]


async def test_upload_magic_byte_check_runs_before_r2_unconfigured(
    client,
    product_factory,
    user_factory,
    monkeypatch,
):
    """A spoofed extension surfaces as 400 even without R2 creds in the env.

    Regression: the upload route used to receive ``Depends(get_r2_client)``,
    which would raise 503 before the route body ran. Magic-byte validation
    happens in the body, so a 400 was unreachable in unconfigured envs.
    The route now calls ``get_r2_client()`` inline after validation.
    """
    # Force an "R2 not configured" state — clear every setting the
    # storage helper checks.
    for field in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
        "R2_PUBLIC_URL",
    ):
        monkeypatch.setattr(f"app.core.config.settings.{field}", "")

    product = product_factory(slug="upload-magic-bytes-no-r2")
    headers = _admin_headers(user_factory)

    response = await client.post(
        f"/api/v1/products/{product.id}/images",
        files={"file": ("evil.jpg", b"<html>not really a jpeg</html>", "image/jpeg")},
        headers=headers,
    )
    # 400 from magic-byte sniff, NOT 503 from missing R2 config.
    assert response.status_code == 400
    assert "Unsupported image format" in response.json()["detail"]


async def test_upload_valid_bytes_yields_503_when_r2_unconfigured(
    client,
    product_factory,
    user_factory,
    monkeypatch,
):
    """A valid image with no R2 creds still surfaces 503 — after validation.

    Confirms the inline ``get_r2_client()`` still raises when settings are
    absent; only the *ordering* relative to validation changed.
    """
    for field in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
        "R2_PUBLIC_URL",
    ):
        monkeypatch.setattr(f"app.core.config.settings.{field}", "")

    product = product_factory(slug="upload-valid-bytes-no-r2")
    headers = _admin_headers(user_factory)

    response = await client.post(
        f"/api/v1/products/{product.id}/images",
        files={"file": ("ok.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    assert response.status_code == 503
    assert "R2 not configured" in response.json()["detail"]


async def test_upload_rejects_oversized_pre_buffer(client, product_factory, user_factory, r2_mock):
    """6 MiB of valid PNG bytes — server must bail with 413 and not put_object."""
    product = product_factory(slug="upload-oversize")
    headers = _admin_headers(user_factory)
    # 6 MiB image: 2000x2000 RGB PNG comes out roughly that size, but to guarantee
    # the size, pad with bytes appended after a valid PNG header. The magic-byte
    # check passes on the first chunk, then the streaming cap trips.
    body = _png_bytes(8, 8) + b"\x00" * (6 * 1024 * 1024)

    response = await client.post(
        f"/api/v1/products/{product.id}/images",
        files={"file": ("big.png", body, "image/png")},
        headers=headers,
    )
    assert response.status_code == 413
    assert "MiB" in response.json()["detail"]

    objs = r2_mock.list_objects_v2(Bucket=TEST_BUCKET).get("Contents", [])
    assert objs == []  # nothing was uploaded


async def test_upload_persists_dimensions(
    client, db_session, product_factory, user_factory, r2_mock
):
    product = product_factory(slug="upload-dimensions")
    headers = _admin_headers(user_factory)

    response = await client.post(
        f"/api/v1/products/{product.id}/images",
        files={"file": ("size.jpg", _jpeg_bytes(800, 600), "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["width"], body["height"]) == (800, 600)
    assert body["mime"] == "image/jpeg"

    row = db_session.query(ProductImage).filter(ProductImage.id == body["id"]).one()
    assert (row.width, row.height) == (800, 600)


async def test_upload_unauthenticated_rejected(client, product_factory, r2_mock):
    product = product_factory(slug="upload-anon")
    response = await client.post(
        f"/api/v1/products/{product.id}/images",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 401


async def test_upload_non_admin_rejected(client, product_factory, user_factory, r2_mock):
    product = product_factory(slug="upload-customer")
    customer = user_factory(role=UserRole.CUSTOMER, email="c@upload.test")
    token, _, _ = create_access_token(subject=str(customer.id))

    response = await client.post(
        f"/api/v1/products/{product.id}/images",
        files={"file": ("p.png", _png_bytes(), "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


async def test_upload_404_for_missing_product(client, user_factory, r2_mock):
    headers = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/products/99999/images",
        files={"file": ("p.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /products/{id}/images/{image_id}
# ---------------------------------------------------------------------------


async def test_patch_set_primary_demotes_siblings(
    client, db_session, product_factory, user_factory, r2_mock
):
    product = product_factory(slug="patch-primary")
    headers = _admin_headers(user_factory)

    # Upload three images via the API so the test exercises the same surface
    # admins do.
    ids: list[int] = []
    for i, color in enumerate(("red", "green", "blue")):
        r = await client.post(
            f"/api/v1/products/{product.id}/images",
            files={"file": (f"img{i}.png", _png_bytes(50, 50, color), "image/png")},
            headers=headers,
        )
        assert r.status_code == 201
        ids.append(r.json()["id"])

    # Promote the third image.
    response = await client.patch(
        f"/api/v1/products/{product.id}/images/{ids[2]}",
        json={"is_primary": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    db_session.expire_all()
    rows = (
        db_session.query(ProductImage)
        .filter(ProductImage.product_id == product.id)
        .order_by(ProductImage.id)
        .all()
    )
    primaries = {row.id: row.is_primary for row in rows}
    assert primaries == {ids[0]: False, ids[1]: False, ids[2]: True}


async def test_patch_image_must_belong_to_product(client, product_factory, user_factory, r2_mock):
    """Image attached to product A cannot be patched via product B's path."""
    p1 = product_factory(slug="patch-mismatch-1")
    p2 = product_factory(slug="patch-mismatch-2")
    headers = _admin_headers(user_factory)

    upload = await client.post(
        f"/api/v1/products/{p1.id}/images",
        files={"file": ("p.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    image_id = upload.json()["id"]

    response = await client.patch(
        f"/api/v1/products/{p2.id}/images/{image_id}",
        json={"alt_text": "stolen"},
        headers=headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /products/{id}/images/{image_id}
# ---------------------------------------------------------------------------


async def test_delete_removes_r2_object(client, db_session, product_factory, user_factory, r2_mock):
    product = product_factory(slug="delete-r2")
    headers = _admin_headers(user_factory)

    upload = await client.post(
        f"/api/v1/products/{product.id}/images",
        files={"file": ("d.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    image_id = upload.json()["id"]
    image_url = upload.json()["url"]
    key = image_url.removeprefix(TEST_PUBLIC_URL + "/")

    # Pre-condition: object exists.
    assert r2_mock.head_object(Bucket=TEST_BUCKET, Key=key)["ContentLength"] > 0

    response = await client.delete(
        f"/api/v1/products/{product.id}/images/{image_id}",
        headers=headers,
    )
    assert response.status_code == 204

    # Row gone.
    assert db_session.query(ProductImage).filter(ProductImage.id == image_id).first() is None
    # Object gone.
    with pytest.raises(ClientError) as err:
        r2_mock.head_object(Bucket=TEST_BUCKET, Key=key)
    assert err.value.response["Error"]["Code"] in {"404", "NoSuchKey"}


async def test_delete_continues_when_r2_unavailable(
    client, db_session, product_factory, user_factory, r2_mock
):
    """An R2 outage during delete should not strand the DB row."""
    product = product_factory(slug="delete-r2-down")
    headers = _admin_headers(user_factory)

    upload = await client.post(
        f"/api/v1/products/{product.id}/images",
        files={"file": ("d.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    image_id = upload.json()["id"]

    # Swap the R2 client for one whose delete_object raises.
    class _BrokenClient:
        def delete_object(self, **_kwargs):
            raise ClientError({"Error": {"Code": "500", "Message": "boom"}}, "DeleteObject")

    import app.api.v1.uploads as uploads_module

    original = uploads_module.get_r2_client
    uploads_module.get_r2_client = lambda: _BrokenClient()
    try:
        response = await client.delete(
            f"/api/v1/products/{product.id}/images/{image_id}",
            headers=headers,
        )
    finally:
        uploads_module.get_r2_client = original

    assert response.status_code == 204
    assert db_session.query(ProductImage).filter(ProductImage.id == image_id).first() is None
