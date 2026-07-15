"""Capability gating for the base product CRUD in the public products router.

``app/api/v1/products.py`` mounts at ``/api/v1/products`` and carries three
**admin-only write routes** — ``POST /``, ``PUT /{id}``, ``DELETE /{id}``.
The A1 back-office program fenced every admin write behind
``require_capability(catalog:write)``; the gating script targeted
``admin_products.py`` and originally **missed** these three because they live in
the storefront-facing router. This module locks the fence in place:

- a scoped staffer WITHOUT ``catalog:write`` (only ``orders:write``) → 403 on
  all three (the reported gap)
- an owner AND a scoped staffer WITH ``catalog:write`` → the routes work
  (regression floor)
- a **structural tripwire**: every ``POST/PUT/PATCH/DELETE`` route whose
  dependency tree includes ``get_current_admin`` must also (transitively)
  require ``require_capability`` — so the next admin write dropped into a
  "public" router can't slip the fence again.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.core.capabilities import CATALOG_WRITE, ORDERS_WRITE
from app.core.security import create_access_token
from app.main import app
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _product_body(slug: str) -> dict:
    return {"name": f"Product {slug}", "slug": slug, "price_usd": 10.0}


# ---------------------------------------------------------------------------
# The reported gap: scoped staffer without catalog:write is fenced out
# ---------------------------------------------------------------------------


async def test_scoped_staffer_without_catalog_write_cannot_create(client, user_factory):
    staffer = user_factory(
        email="prodcap-orders-only-create@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[ORDERS_WRITE],  # no catalog:write
    )
    resp = await client.post(
        "/api/v1/products/", json=_product_body("cap-gap-create"), headers=_headers(staffer)
    )
    assert resp.status_code == 403, resp.text


async def test_scoped_staffer_without_catalog_write_cannot_update(
    client, user_factory, product_factory
):
    staffer = user_factory(
        email="prodcap-orders-only-update@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[ORDERS_WRITE],
    )
    product = product_factory(slug="cap-gap-update")
    resp = await client.put(
        f"/api/v1/products/{product.id}",
        json={"name": "Renamed"},
        headers=_headers(staffer),
    )
    assert resp.status_code == 403, resp.text


async def test_scoped_staffer_without_catalog_write_cannot_delete(
    client, user_factory, product_factory
):
    staffer = user_factory(
        email="prodcap-orders-only-delete@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[ORDERS_WRITE],
    )
    product = product_factory(slug="cap-gap-delete")
    resp = await client.delete(f"/api/v1/products/{product.id}", headers=_headers(staffer))
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Regression floor: the routes still work for callers who SHOULD reach them
# ---------------------------------------------------------------------------


async def test_owner_can_create_update_delete(client, user_factory):
    owner = user_factory(email="prodcap-owner@example.com", role=UserRole.ADMIN)  # factory = owner
    assert owner.is_owner is True

    created = await client.post(
        "/api/v1/products/", json=_product_body("cap-owner-crud"), headers=_headers(owner)
    )
    assert created.status_code == 201, created.text
    product_id = created.json()["id"]

    updated = await client.put(
        f"/api/v1/products/{product_id}",
        json={"name": "Owner Renamed"},
        headers=_headers(owner),
    )
    assert updated.status_code == 200, updated.text

    deleted = await client.delete(f"/api/v1/products/{product_id}", headers=_headers(owner))
    assert deleted.status_code == 204, deleted.text


async def test_scoped_staffer_with_catalog_write_can_create_update_delete(client, user_factory):
    staffer = user_factory(
        email="prodcap-catalog@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE],
    )

    created = await client.post(
        "/api/v1/products/", json=_product_body("cap-catalog-crud"), headers=_headers(staffer)
    )
    assert created.status_code == 201, created.text
    product_id = created.json()["id"]

    updated = await client.put(
        f"/api/v1/products/{product_id}",
        json={"name": "Catalog Renamed"},
        headers=_headers(staffer),
    )
    assert updated.status_code == 200, updated.text

    deleted = await client.delete(f"/api/v1/products/{product_id}", headers=_headers(staffer))
    assert deleted.status_code == 204, deleted.text


# ---------------------------------------------------------------------------
# Structural tripwire — the real fix for the class of bug
# ---------------------------------------------------------------------------

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _dependency_call_qualnames(dependant) -> list[str]:
    """Every callable reachable in a route's dependency tree, by ``__qualname__``."""
    names: list[str] = []

    def _walk(dep) -> None:
        if dep.call is not None:
            names.append(getattr(dep.call, "__qualname__", "") or "")
        for sub in dep.dependencies:
            _walk(sub)

    _walk(dependant)
    return names


def test_every_admin_write_route_is_capability_gated():
    """Enumerate every write/DELETE route whose dependency tree includes
    ``get_current_admin`` and assert each also (transitively) requires a
    ``require_capability`` gate. Fails loudly if a new admin write route lands
    ungated — the exact class of bug that let the product CRUD slip the fence.
    """
    ungated: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = (route.methods or set()) & _WRITE_METHODS
        if not methods:
            continue
        qualnames = _dependency_call_qualnames(route.dependant)
        is_admin = any("get_current_admin" in name for name in qualnames)
        if not is_admin:
            continue
        is_gated = any("require_capability.<locals>._dep" in name for name in qualnames)
        if not is_gated:
            ungated.append(f"{sorted(methods)} {route.path}")

    assert not ungated, (
        "Admin write routes missing require_capability() — add a capability gate:\n"
        + "\n".join(ungated)
    )
