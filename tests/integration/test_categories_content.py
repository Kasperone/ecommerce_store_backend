"""Integration tests for category per-locale content, SEO meta, and hero image.

PR2 of the categories overhaul plan
(``ecommerce_store/.claude/notes/plans/admin/in-progress/2026-05-21-categories-overhaul.md``).
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
from app.models.category import Category
from app.models.user import UserRole

pytestmark = pytest.mark.integration

TEST_BUCKET = "test-bucket"
TEST_PUBLIC_URL = "https://cdn.test.example.com"


@pytest.fixture
def r2_mock(monkeypatch) -> Iterator[BaseClient]:
    """Mock R2 + monkeypatch the categories module's inline ``get_r2_client``.

    Mirrors ``tests/integration/test_uploads.py::r2_mock`` — the category
    hero route reuses the same inline-after-validation pattern that the
    product uploads route does.
    """
    monkeypatch.setattr("app.core.config.settings.R2_BUCKET_NAME", TEST_BUCKET)
    monkeypatch.setattr("app.core.config.settings.R2_PUBLIC_URL", TEST_PUBLIC_URL)
    monkeypatch.setattr("app.core.config.settings.R2_ACCOUNT_ID", "test-account")
    monkeypatch.setattr("app.core.config.settings.R2_ACCESS_KEY_ID", "test-key")
    monkeypatch.setattr("app.core.config.settings.R2_SECRET_ACCESS_KEY", "x" * 64)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=TEST_BUCKET)
        monkeypatch.setattr("app.api.v1.categories.get_r2_client", lambda: client)
        yield client


def _admin_headers(user_factory, email: str = "admin@cat-content.test") -> dict:
    admin = user_factory(role=UserRole.ADMIN, email=email)
    token, _, _ = create_access_token(subject=str(admin.id))
    return {"Authorization": f"Bearer {token}"}


def _png_bytes(width: int = 100, height: int = 100, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Per-locale fields round-trip
# ---------------------------------------------------------------------------


async def test_create_with_locale_and_seo_round_trips(client, user_factory):
    headers = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/categories/",
        json={
            "name": "Books",
            "slug": "books",
            "description": "Printed reads",
            "name_pl": "Książki",
            "slug_pl": "ksiazki",
            "description_pl": "Druki",
            "meta_title": "Books — best of",
            "meta_description": "Curated selection of books",
            "meta_title_pl": "Książki — najlepsze",
            "meta_description_pl": "Wybór książek",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name_pl"] == "Książki"
    assert body["slug_pl"] == "ksiazki"
    assert body["meta_title_pl"] == "Książki — najlepsze"


async def test_update_locale_fields_in_isolation(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    cat = category_factory(slug="upd-locale", name="Furniture")
    response = await client.put(
        f"/api/v1/categories/{cat.id}",
        json={"name_pl": "Meble", "slug_pl": "meble"},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Furniture"  # untouched
    assert body["name_pl"] == "Meble"
    assert body["slug_pl"] == "meble"


# ---------------------------------------------------------------------------
# slug / slug_pl uniqueness
# ---------------------------------------------------------------------------


async def test_create_with_duplicate_slug_pl_rejected(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    category_factory(slug="first-en", slug_pl="kolizja")
    response = await client.post(
        "/api/v1/categories/",
        json={"name": "Other", "slug": "second-en", "slug_pl": "kolizja"},
        headers=headers,
    )
    assert response.status_code == 400
    assert "slug_pl" in response.json()["detail"]


async def test_two_rows_with_null_slug_pl_are_allowed(client, user_factory, category_factory):
    """Partial unique index allows many NULLs — admins fill PL in over time."""
    headers = _admin_headers(user_factory)
    category_factory(slug="no-pl-1")
    response = await client.post(
        "/api/v1/categories/",
        json={"name": "Second", "slug": "no-pl-2"},  # slug_pl omitted → NULL
        headers=headers,
    )
    assert response.status_code == 201


async def test_get_by_slug_resolves_pl_slug(client, user_factory, category_factory):
    category_factory(slug="en-only", slug_pl="pl-side")
    response = await client.get("/api/v1/categories/slug/pl-side")
    assert response.status_code == 200
    assert response.json()["slug"] == "en-only"


async def test_get_by_slug_prefers_en_on_collision(client, user_factory, category_factory):
    """When ``foo`` appears as ``slug`` on row A AND ``slug_pl`` on row B,
    the EN row wins — matches the default-locale convention."""
    en_row = category_factory(slug="ambig", name="EN row")
    category_factory(slug="other-en", slug_pl="ambig", name="PL row")
    response = await client.get("/api/v1/categories/slug/ambig")
    assert response.status_code == 200
    assert response.json()["id"] == en_row.id


async def test_update_to_existing_slug_pl_rejected(client, user_factory, category_factory):
    headers = _admin_headers(user_factory)
    category_factory(slug="taken-en", slug_pl="taken-pl")
    other = category_factory(slug="other-en-2", slug_pl="free-pl")
    response = await client.put(
        f"/api/v1/categories/{other.id}",
        json={"slug_pl": "taken-pl"},
        headers=headers,
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# SEO length validators
# ---------------------------------------------------------------------------


async def test_meta_title_too_long_rejected_by_pydantic(client, user_factory):
    headers = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/categories/",
        json={
            "name": "Long",
            "slug": "long",
            "meta_title": "x" * 71,
        },
        headers=headers,
    )
    assert response.status_code == 422


async def test_meta_description_too_long_rejected_by_pydantic(client, user_factory):
    headers = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/categories/",
        json={
            "name": "Long",
            "slug": "long-desc",
            "meta_description": "y" * 161,
        },
        headers=headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Hero upload
# ---------------------------------------------------------------------------


async def test_hero_upload_happy_path(client, db_session, user_factory, category_factory, r2_mock):
    category = category_factory(slug="hero-happy")
    headers = _admin_headers(user_factory)

    response = await client.post(
        f"/api/v1/categories/{category.id}/hero",
        files={"file": ("hero.png", _png_bytes(120, 80), "image/png")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["hero_width"] == 120
    assert body["hero_height"] == 80
    assert body["hero_mime"] == "image/png"
    assert body["hero_image_url"].startswith(TEST_PUBLIC_URL + "/categories/")

    db_session.expire_all()
    row = db_session.query(Category).filter(Category.id == category.id).one()
    assert row.hero_width == 120
    assert row.hero_image_url is not None

    # Audit row for the update
    audit = (
        db_session.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "category")
        .filter(AdminAuditEvent.action == "update")
        .all()
    )
    assert len(audit) == 1
    assert audit[0].resource_id == category.id


async def test_hero_upload_rejects_magic_byte_mismatch(
    client, user_factory, category_factory, r2_mock
):
    category = category_factory(slug="hero-magic")
    headers = _admin_headers(user_factory)
    response = await client.post(
        f"/api/v1/categories/{category.id}/hero",
        files={"file": ("evil.jpg", b"<html>not a jpeg</html>", "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 400
    assert "Unsupported image format" in response.json()["detail"]


async def test_hero_upload_magic_byte_check_runs_before_r2_unconfigured(
    client, user_factory, category_factory, monkeypatch
):
    """Mirrors test_upload_magic_byte_check_runs_before_r2_unconfigured for
    products — a spoofed extension surfaces as 400 even without R2 creds."""
    for field in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
        "R2_PUBLIC_URL",
    ):
        monkeypatch.setattr(f"app.core.config.settings.{field}", "")

    category = category_factory(slug="hero-no-r2")
    headers = _admin_headers(user_factory)
    response = await client.post(
        f"/api/v1/categories/{category.id}/hero",
        files={"file": ("evil.jpg", b"<html>not a jpeg</html>", "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 400


async def test_hero_upload_valid_bytes_503_when_r2_unconfigured(
    client, user_factory, category_factory, monkeypatch
):
    for field in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
        "R2_PUBLIC_URL",
    ):
        monkeypatch.setattr(f"app.core.config.settings.{field}", "")
    category = category_factory(slug="hero-503")
    headers = _admin_headers(user_factory)
    response = await client.post(
        f"/api/v1/categories/{category.id}/hero",
        files={"file": ("ok.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    assert response.status_code == 503
    assert "R2 not configured" in response.json()["detail"]


async def test_hero_upload_oversize_rejected(client, user_factory, category_factory, r2_mock):
    category = category_factory(slug="hero-big")
    headers = _admin_headers(user_factory)
    body = _png_bytes(8, 8) + b"\x00" * (6 * 1024 * 1024)
    response = await client.post(
        f"/api/v1/categories/{category.id}/hero",
        files={"file": ("big.png", body, "image/png")},
        headers=headers,
    )
    assert response.status_code == 413
    assert r2_mock.list_objects_v2(Bucket=TEST_BUCKET).get("Contents", []) == []


async def test_hero_upload_replaces_previous_object(
    client, db_session, user_factory, category_factory, r2_mock
):
    """A second upload overwrites the row and best-effort deletes the old key."""
    category = category_factory(slug="hero-replace")
    headers = _admin_headers(user_factory)

    first = await client.post(
        f"/api/v1/categories/{category.id}/hero",
        files={"file": ("a.png", _png_bytes(50, 50, "red"), "image/png")},
        headers=headers,
    )
    assert first.status_code == 201
    first_url = first.json()["hero_image_url"]
    first_key = first_url.removeprefix(TEST_PUBLIC_URL + "/")
    assert r2_mock.head_object(Bucket=TEST_BUCKET, Key=first_key)["ContentLength"] > 0

    second = await client.post(
        f"/api/v1/categories/{category.id}/hero",
        files={"file": ("b.jpg", _png_bytes(60, 40, "blue"), "image/png")},
        headers=headers,
    )
    assert second.status_code == 201
    second_url = second.json()["hero_image_url"]
    assert second_url != first_url

    # Old object gone
    with pytest.raises(ClientError) as err:
        r2_mock.head_object(Bucket=TEST_BUCKET, Key=first_key)
    assert err.value.response["Error"]["Code"] in {"404", "NoSuchKey"}


async def test_hero_upload_404_for_missing_category(client, user_factory, r2_mock):
    headers = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/categories/999999/hero",
        files={"file": ("p.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /hero — alt-text only
# ---------------------------------------------------------------------------


async def test_hero_patch_alt_text(client, db_session, user_factory, category_factory, r2_mock):
    category = category_factory(slug="hero-patch")
    headers = _admin_headers(user_factory)
    await client.post(
        f"/api/v1/categories/{category.id}/hero",
        files={"file": ("p.png", _png_bytes(), "image/png")},
        headers=headers,
    )

    response = await client.patch(
        f"/api/v1/categories/{category.id}/hero",
        json={"hero_image_alt": "A nice hero"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["hero_image_alt"] == "A nice hero"
    db_session.expire_all()
    row = db_session.query(Category).filter(Category.id == category.id).one()
    assert row.hero_image_alt == "A nice hero"


async def test_hero_patch_without_image_rejected(client, user_factory, category_factory):
    category = category_factory(slug="hero-patch-empty")
    headers = _admin_headers(user_factory)
    response = await client.patch(
        f"/api/v1/categories/{category.id}/hero",
        json={"hero_image_alt": "won't stick"},
        headers=headers,
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /hero
# ---------------------------------------------------------------------------


async def test_hero_delete_clears_columns_and_object(
    client, db_session, user_factory, category_factory, r2_mock
):
    category = category_factory(slug="hero-del")
    headers = _admin_headers(user_factory)
    upload = await client.post(
        f"/api/v1/categories/{category.id}/hero",
        files={"file": ("p.png", _png_bytes(), "image/png")},
        headers=headers,
    )
    key = upload.json()["hero_image_url"].removeprefix(TEST_PUBLIC_URL + "/")
    assert r2_mock.head_object(Bucket=TEST_BUCKET, Key=key)["ContentLength"] > 0

    response = await client.delete(
        f"/api/v1/categories/{category.id}/hero",
        headers=headers,
    )
    assert response.status_code == 204
    db_session.expire_all()
    row = db_session.query(Category).filter(Category.id == category.id).one()
    assert row.hero_image_url is None
    assert row.hero_width is None
    assert row.hero_mime is None
    with pytest.raises(ClientError) as err:
        r2_mock.head_object(Bucket=TEST_BUCKET, Key=key)
    assert err.value.response["Error"]["Code"] in {"404", "NoSuchKey"}


async def test_hero_delete_idempotent_when_no_image(client, user_factory, category_factory):
    """No hero set → DELETE still succeeds (no R2 call needed)."""
    category = category_factory(slug="hero-del-idemp")
    headers = _admin_headers(user_factory)
    response = await client.delete(
        f"/api/v1/categories/{category.id}/hero",
        headers=headers,
    )
    assert response.status_code == 204


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


async def test_hero_upload_non_admin_rejected(client, user_factory, category_factory, r2_mock):
    category = category_factory(slug="hero-cust")
    customer = user_factory(role=UserRole.CUSTOMER, email="c@cat-hero.test")
    token, _, _ = create_access_token(subject=str(customer.id))
    response = await client.post(
        f"/api/v1/categories/{category.id}/hero",
        files={"file": ("p.png", _png_bytes(), "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
