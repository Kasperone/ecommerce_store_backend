"""Integration tests for the public store-config API (/api/v1/store-config).

The customer-facing read side of the store-settings singleton: unauthenticated,
returns the slim ``StoreConfigPublic`` projection the storefront footer needs
(company-identification + social fields). It deliberately excludes
``invoice_vat_rate`` and the internal ``tax_id``/audit/timestamp noise. There is
always exactly one row (``get_singleton`` get-or-creates it), so it never 404s.

Also asserts the admin Settings PATCH accepts + persists the new company/social
fields and that the public read reflects them.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration

PUBLIC_URL = "/api/v1/store-config"
SETTINGS_URL = "/api/v1/admin/settings"

PUBLIC_KEYS = {
    "store_name",
    "brand_tagline",
    "logo_url",
    "legal_name",
    "address_line",
    "address_line2",
    "postal_city",
    "country",
    "nip",
    "regon",
    "krs",
    "email",
    "phone",
    "social_instagram",
    "social_facebook",
    "social_tiktok",
    "social_youtube",
}


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# public read
# ---------------------------------------------------------------------------


async def test_public_read_is_unauthenticated_and_never_404(client):
    resp = await client.get(PUBLIC_URL)
    assert resp.status_code == 200
    body = resp.json()
    # Seeded singleton present.
    assert body["store_name"] == "E-commerce Store"


async def test_public_shape_excludes_internal_fields(client):
    resp = await client.get(PUBLIC_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == PUBLIC_KEYS
    # Internal-only fields must not leak.
    assert "invoice_vat_rate" not in body
    assert "tax_id" not in body
    assert "id" not in body
    assert "updated_at" not in body


# ---------------------------------------------------------------------------
# admin PATCH of new fields reflected publicly
# ---------------------------------------------------------------------------


async def test_admin_patch_new_fields_reflected_in_public_read(client, user_factory):
    admin = user_factory(email="sc-pub-admin1@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        SETTINGS_URL,
        json={
            "legal_name": "Acme Sp. z o.o.",
            "address_line2": "skr. poczt. 5",
            "nip": "PL1234567890",
            "regon": "123456789",
            "krs": "0000123456",
            "phone": "+48 22 000 00 00",
            "social_instagram": "https://instagram.com/acme",
            "social_facebook": "https://facebook.com/acme",
            "social_tiktok": "https://tiktok.com/@acme",
            "social_youtube": "https://youtube.com/@acme",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    abody = resp.json()
    assert abody["legal_name"] == "Acme Sp. z o.o."
    assert abody["nip"] == "PL1234567890"

    pub = await client.get(PUBLIC_URL)
    assert pub.status_code == 200
    pbody = pub.json()
    assert pbody["legal_name"] == "Acme Sp. z o.o."
    assert pbody["address_line2"] == "skr. poczt. 5"
    assert pbody["nip"] == "PL1234567890"
    assert pbody["regon"] == "123456789"
    assert pbody["krs"] == "0000123456"
    assert pbody["phone"] == "+48 22 000 00 00"
    assert pbody["social_instagram"] == "https://instagram.com/acme"
    assert pbody["social_facebook"] == "https://facebook.com/acme"
    assert pbody["social_tiktok"] == "https://tiktok.com/@acme"
    assert pbody["social_youtube"] == "https://youtube.com/@acme"


async def test_admin_get_includes_new_fields(client, user_factory):
    admin = user_factory(email="sc-pub-admin2@example.com", role=UserRole.ADMIN)
    resp = await client.get(SETTINGS_URL, headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "legal_name",
        "address_line2",
        "nip",
        "regon",
        "krs",
        "phone",
        "social_instagram",
        "social_facebook",
        "social_tiktok",
        "social_youtube",
    ):
        assert key in body
    # Admin shape still carries the internal field.
    assert "invoice_vat_rate" in body
