"""HTTP-level acceptance for product Polish content (``name_pl`` / ``description_pl``).

BE-A of the storefront search plan. Products gained two nullable Polish-locale
columns mirroring the category ones. Coverage:

* POST create round-trips both fields (response + GET).
* Omitting them on create leaves both NULL.
* PUT sets them on a product that had none.
* A partial PATCH that omits the PL keys leaves existing values untouched.
* PUT can clear them back to NULL.
* A blank ``name_pl`` is rejected (``min_length=1``) — same as ``name``.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _admin_headers(user_factory) -> dict:
    admin = user_factory(role=UserRole.ADMIN, email="admin-products-i18n@test")
    token, _, _ = create_access_token(subject=str(admin.id))
    return {"Authorization": f"Bearer {token}"}


def _create_body(**overrides) -> dict:
    body = {
        "name": "Wireless Headphones",
        "slug": "wireless-headphones-i18n",
        "price_usd": 99.0,
    }
    body.update(overrides)
    return body


async def test_create_with_polish_content_round_trips(client, user_factory):
    headers = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/products/",
        json=_create_body(
            name_pl="Słuchawki bezprzewodowe",
            description_pl="Najlepsze słuchawki w sklepie",
        ),
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name_pl"] == "Słuchawki bezprzewodowe"
    assert body["description_pl"] == "Najlepsze słuchawki w sklepie"

    # Round-trips on GET too (the admin edit form reads it there).
    fetched = await client.get(f"/api/v1/products/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name_pl"] == "Słuchawki bezprzewodowe"
    assert fetched.json()["description_pl"] == "Najlepsze słuchawki w sklepie"


async def test_create_without_polish_content_defaults_null(client, user_factory):
    headers = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/products/",
        json=_create_body(slug="no-polish-content"),
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name_pl"] is None
    assert body["description_pl"] is None


async def test_put_sets_polish_content(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory(name_pl=None, description_pl=None)

    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"name_pl": "Telefon", "description_pl": "Świetny telefon"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["name_pl"] == "Telefon"
    assert response.json()["description_pl"] == "Świetny telefon"


async def test_put_partial_patch_leaves_polish_content_alone(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory(name_pl="Telefon", description_pl="Opis")

    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"name": "Renamed EN only"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Renamed EN only"
    assert body["name_pl"] == "Telefon"
    assert body["description_pl"] == "Opis"


async def test_put_clears_polish_content(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory(name_pl="Telefon", description_pl="Opis")

    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"name_pl": None, "description_pl": None},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["name_pl"] is None
    assert response.json()["description_pl"] is None


async def test_create_rejects_blank_name_pl(client, user_factory):
    headers = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/products/",
        json=_create_body(slug="blank-name-pl", name_pl=""),
        headers=headers,
    )
    assert response.status_code == 422, response.text
