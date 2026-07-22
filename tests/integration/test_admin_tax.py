"""Integration tests for the admin tax API (Phase-0 U2).

Covers tax-category CRUD (create / duplicate-code / default promotion /
can't-unset-only-default / delete guards) and tax-rate CRUD (create /
duplicate-window / bad-window / conflict on update / delete). Gated behind
SETTINGS_WRITE; an ADMIN-role user satisfies it.

The tax-categories migration seeds STANDARD (default) + REDUCED_8/5/ZERO with PL
rates, so a test can rely on a default already existing.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration

CATEGORIES_URL = "/api/v1/admin/tax/categories"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def test_list_includes_seeded_categories_default_first(client, user_factory):
    admin = user_factory(email="tax-a@example.com", role=UserRole.ADMIN)
    resp = await client.get(CATEGORIES_URL, headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    codes = [c["code"] for c in body]
    assert "STANDARD" in codes and "REDUCED_8" in codes
    # Default sorts first, and its PL rate rides along nested.
    assert body[0]["is_default"] is True
    std = next(c for c in body if c["code"] == "STANDARD")
    pl_rate = next(r for r in std["rates"] if r["country_code"] == "PL")
    assert pl_rate["rate_pct"] == "23.00"


async def test_create_category(client, user_factory):
    admin = user_factory(email="tax-b@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        CATEGORIES_URL,
        json={"code": "BOOKS", "name": "Books", "name_pl": "Książki"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["code"] == "BOOKS"
    assert body["is_default"] is False
    assert body["rates"] == []


async def test_duplicate_code_409(client, user_factory):
    admin = user_factory(email="tax-c@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        CATEGORIES_URL,
        json={"code": "STANDARD", "name": "Dup"},
        headers=_headers(admin),
    )
    assert resp.status_code == 409, resp.text


async def test_setting_default_demotes_the_incumbent(client, user_factory, db_session):
    admin = user_factory(email="tax-d@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        CATEGORIES_URL,
        json={"code": "NEWDEF", "name": "New default", "is_default": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    # Exactly one default remains, and it is the new one.
    listing = (await client.get(CATEGORIES_URL, headers=_headers(admin))).json()
    defaults = [c["code"] for c in listing if c["is_default"]]
    assert defaults == ["NEWDEF"]


async def test_cannot_unset_the_only_default(client, user_factory):
    admin = user_factory(email="tax-e@example.com", role=UserRole.ADMIN)
    listing = (await client.get(CATEGORIES_URL, headers=_headers(admin))).json()
    default_id = next(c["id"] for c in listing if c["is_default"])
    resp = await client.patch(
        f"{CATEGORIES_URL}/{default_id}",
        json={"is_default": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 409, resp.text


async def test_delete_default_refused(client, user_factory):
    admin = user_factory(email="tax-f@example.com", role=UserRole.ADMIN)
    listing = (await client.get(CATEGORIES_URL, headers=_headers(admin))).json()
    default_id = next(c["id"] for c in listing if c["is_default"])
    resp = await client.delete(f"{CATEGORIES_URL}/{default_id}", headers=_headers(admin))
    assert resp.status_code == 409, resp.text


async def test_delete_category_in_use_refused(client, user_factory, product_factory):
    admin = user_factory(email="tax-g@example.com", role=UserRole.ADMIN)
    created = (
        await client.post(
            CATEGORIES_URL,
            json={"code": "INUSE", "name": "In use"},
            headers=_headers(admin),
        )
    ).json()
    # Classify a product under it, then deletion must be refused.
    product_factory(tax_category_id=created["id"])
    resp = await client.delete(f"{CATEGORIES_URL}/{created['id']}", headers=_headers(admin))
    assert resp.status_code == 409, resp.text


async def test_delete_unused_category(client, user_factory):
    admin = user_factory(email="tax-h@example.com", role=UserRole.ADMIN)
    created = (
        await client.post(
            CATEGORIES_URL,
            json={"code": "SCRATCH", "name": "Scratch"},
            headers=_headers(admin),
        )
    ).json()
    resp = await client.delete(f"{CATEGORIES_URL}/{created['id']}", headers=_headers(admin))
    assert resp.status_code == 204, resp.text


async def test_add_rate_normalizes_country_and_nests(client, user_factory):
    admin = user_factory(email="tax-i@example.com", role=UserRole.ADMIN)
    cat = (
        await client.post(
            CATEGORIES_URL,
            json={"code": "RATED", "name": "Rated"},
            headers=_headers(admin),
        )
    ).json()
    resp = await client.post(
        f"{CATEGORIES_URL}/{cat['id']}/rates",
        json={"country_code": "de", "rate_pct": "19.00", "valid_from": "2020-01-01"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["country_code"] == "DE"  # normalized upper-case
    assert body["rate_pct"] == "19.00"
    assert body["valid_to"] is None
    assert body["tax_category_id"] == cat["id"]


async def test_duplicate_rate_window_409(client, user_factory):
    admin = user_factory(email="tax-j@example.com", role=UserRole.ADMIN)
    cat = (
        await client.post(
            CATEGORIES_URL, json={"code": "DUPW", "name": "Dup window"}, headers=_headers(admin)
        )
    ).json()
    payload = {"country_code": "FR", "rate_pct": "20.00", "valid_from": "2020-01-01"}
    first = await client.post(
        f"{CATEGORIES_URL}/{cat['id']}/rates", json=payload, headers=_headers(admin)
    )
    assert first.status_code == 201, first.text
    dup = await client.post(
        f"{CATEGORIES_URL}/{cat['id']}/rates", json=payload, headers=_headers(admin)
    )
    assert dup.status_code == 409, dup.text


async def test_bad_window_on_create_422(client, user_factory):
    admin = user_factory(email="tax-k@example.com", role=UserRole.ADMIN)
    cat = (
        await client.post(
            CATEGORIES_URL, json={"code": "BADW", "name": "Bad window"}, headers=_headers(admin)
        )
    ).json()
    resp = await client.post(
        f"{CATEGORIES_URL}/{cat['id']}/rates",
        json={
            "country_code": "PL",
            "rate_pct": "23.00",
            "valid_from": "2021-01-01",
            "valid_to": "2020-01-01",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 422, resp.text


async def test_update_and_delete_rate(client, user_factory):
    admin = user_factory(email="tax-l@example.com", role=UserRole.ADMIN)
    cat = (
        await client.post(
            CATEGORIES_URL, json={"code": "UPD", "name": "Update"}, headers=_headers(admin)
        )
    ).json()
    rate = (
        await client.post(
            f"{CATEGORIES_URL}/{cat['id']}/rates",
            json={"country_code": "ES", "rate_pct": "21.00", "valid_from": "2020-01-01"},
            headers=_headers(admin),
        )
    ).json()
    # Close the window.
    upd = await client.patch(
        f"/api/v1/admin/tax/rates/{rate['id']}",
        json={"valid_to": "2025-01-01"},
        headers=_headers(admin),
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["valid_to"] == "2025-01-01"
    # Bad merged window is refused.
    bad = await client.patch(
        f"/api/v1/admin/tax/rates/{rate['id']}",
        json={"valid_to": "2019-01-01"},
        headers=_headers(admin),
    )
    assert bad.status_code == 422, bad.text
    # Delete it.
    dele = await client.delete(f"/api/v1/admin/tax/rates/{rate['id']}", headers=_headers(admin))
    assert dele.status_code == 204, dele.text


async def test_requires_admin(client, user_factory):
    customer = user_factory(email="tax-cust@example.com", role=UserRole.CUSTOMER)
    resp = await client.get(CATEGORIES_URL, headers=_headers(customer))
    assert resp.status_code == 403, resp.text
