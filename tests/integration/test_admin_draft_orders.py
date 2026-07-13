"""Integration tests for the admin draft-orders API (/api/v1/admin/draft-orders).

Covers authz (401/403), create (pinned currency + defaults + unknown-user 404
+ audit), item add/patch/remove with server-recomputed totals (catalog-price
AND override paths, explicit-null override clear, variant-required 422),
header PATCH (shipping fields, discount clamp at 0, unknown shipping method
404), the completed-status lock (409 on every mutation), delete cascade, and
the deleted-product stale-line view. Every mutation writes an audit row
(resource_type ``draft_order``).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.draft_order import DraftOrder, DraftOrderItem
from app.models.user import UserRole

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin/draft-orders"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "draft_order", AdminAuditEvent.action == action)
        .count()
    )


async def _create_draft(client, admin, **overrides) -> dict:
    body = {"currency": "USD"}
    body.update(overrides)
    resp = await client.post(f"{BASE}/", json=body, headers=_headers(admin))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _add_item(client, admin, draft_id: int, **line) -> dict:
    resp = await client.post(f"{BASE}/{draft_id}/items", json=line, headers=_headers(admin))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _complete(db_session: Session, draft_id: int) -> None:
    """Flip a draft to ``completed`` directly in the DB (conversion is PR 3)."""
    draft = db_session.get(DraftOrder, draft_id)
    assert draft is not None
    draft.status = "completed"
    db_session.commit()


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="do-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get(f"{BASE}/")).status_code == 401
    assert (await client.get(f"{BASE}/", headers=_headers(customer))).status_code == 403


async def test_create_requires_admin(client, user_factory):
    customer = user_factory(email="do-cust2@example.com", role=UserRole.CUSTOMER)
    body = {"currency": "USD"}
    assert (await client.post(f"{BASE}/", json=body)).status_code == 401
    assert (await client.post(f"{BASE}/", json=body, headers=_headers(customer))).status_code == 403


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_pins_currency_and_defaults(client, user_factory, db_session):
    admin = user_factory(email="do-admin1@example.com", role=UserRole.ADMIN)
    before = _audit_count(db_session, "create")
    body = await _create_draft(
        client, admin, currency="PLN", guest_email="klient@example.com", notes="phone order"
    )
    assert body["currency"] == "PLN"
    assert body["status"] == "open"
    assert body["user_id"] is None
    assert body["guest_email"] == "klient@example.com"
    assert body["customer_email"] == "klient@example.com"
    assert body["notes"] == "phone order"
    assert body["subtotal"] == 0.0
    assert body["total"] == 0.0
    assert body["discount"] == 0.0
    assert body["items"] == []
    assert body["converted_order_id"] is None
    assert _audit_count(db_session, "create") == before + 1


async def test_create_with_customer_and_unknown_user_404(client, user_factory):
    admin = user_factory(email="do-admin2@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="do-owner@example.com", role=UserRole.CUSTOMER)
    body = await _create_draft(client, admin, user_id=customer.id)
    assert body["user_id"] == customer.id
    assert body["customer_email"] == "do-owner@example.com"
    resp = await client.post(
        f"{BASE}/", json={"currency": "USD", "user_id": 999999}, headers=_headers(admin)
    )
    assert resp.status_code == 404


async def test_create_unknown_currency_422(client, user_factory):
    admin = user_factory(email="do-admin3@example.com", role=UserRole.ADMIN)
    resp = await client.post(f"{BASE}/", json={"currency": "GBP"}, headers=_headers(admin))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# items — add / catalog price / override / recompute
# ---------------------------------------------------------------------------


async def test_add_item_catalog_price_recomputes_totals(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="do-admin4@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=10.0, name="CatalogProd")
    draft = await _create_draft(client, admin)
    before = _audit_count(db_session, "update")
    body = await _add_item(client, admin, draft["id"], product_id=product.id, quantity=2)
    assert len(body["items"]) == 1
    line = body["items"][0]
    assert line["product_id"] == product.id
    assert line["product_name"] == "CatalogProd"
    assert line["unit_price_override"] is None
    assert line["effective_unit_price"] == 10.0  # live catalog price in USD
    assert line["line_total"] == 20.0
    assert body["subtotal"] == 20.0
    assert body["total"] == 20.0
    assert _audit_count(db_session, "update") == before + 1


async def test_add_item_override_price(client, user_factory, product_factory):
    admin = user_factory(email="do-admin5@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=10.0)
    draft = await _create_draft(client, admin)
    body = await _add_item(
        client, admin, draft["id"], product_id=product.id, quantity=2, unit_price_override=7.5
    )
    line = body["items"][0]
    assert line["unit_price_override"] == 7.5
    assert line["effective_unit_price"] == 7.5
    assert line["line_total"] == 15.0
    assert body["subtotal"] == 15.0
    assert body["total"] == 15.0


async def test_add_item_unknown_or_inactive_product_422(client, user_factory, product_factory):
    admin = user_factory(email="do-admin6@example.com", role=UserRole.ADMIN)
    draft = await _create_draft(client, admin)
    resp = await client.post(
        f"{BASE}/{draft['id']}/items",
        json={"product_id": 999999, "quantity": 1},
        headers=_headers(admin),
    )
    assert resp.status_code == 422
    inactive = product_factory(is_active=False)
    resp = await client.post(
        f"{BASE}/{draft['id']}/items",
        json={"product_id": inactive.id, "quantity": 1},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_add_item_variant_product_requires_variant_id(
    client, user_factory, product_factory, db_session
):
    from tests.integration.test_admin_quotes import _add_one_variant

    admin = user_factory(email="do-admin7@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0, price_usd=20.0, name="VarProd")
    red = _add_one_variant(db_session, product)
    draft = await _create_draft(client, admin)
    # No variant_id on a variant product → 422 (variant_required).
    resp = await client.post(
        f"{BASE}/{draft['id']}/items",
        json={"product_id": product.id, "quantity": 1},
        headers=_headers(admin),
    )
    assert resp.status_code == 422
    assert "variant" in resp.json()["detail"].lower()
    # With the variant → 201; price inherits the product (variant price NULL).
    body = await _add_item(
        client, admin, draft["id"], product_id=product.id, variant_id=red.id, quantity=1
    )
    line = body["items"][0]
    assert line["variant_id"] == red.id
    assert line["variant_title"] == "Red"
    assert line["effective_unit_price"] == 20.0


async def test_add_item_zero_quantity_422(client, user_factory, product_factory):
    admin = user_factory(email="do-admin8@example.com", role=UserRole.ADMIN)
    product = product_factory()
    draft = await _create_draft(client, admin)
    resp = await client.post(
        f"{BASE}/{draft['id']}/items",
        json={"product_id": product.id, "quantity": 0},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# items — patch / override clear / remove
# ---------------------------------------------------------------------------


async def test_update_item_quantity_recomputes(client, user_factory, product_factory):
    admin = user_factory(email="do-admin9@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=10.0)
    draft = await _create_draft(client, admin)
    body = await _add_item(client, admin, draft["id"], product_id=product.id, quantity=1)
    item_id = body["items"][0]["id"]
    resp = await client.patch(
        f"{BASE}/{draft['id']}/items/{item_id}",
        json={"quantity": 5},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["quantity"] == 5
    assert body["subtotal"] == 50.0
    assert body["total"] == 50.0


async def test_update_item_clears_override_with_explicit_null(
    client, user_factory, product_factory
):
    admin = user_factory(email="do-admin10@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=10.0)
    draft = await _create_draft(client, admin)
    body = await _add_item(
        client, admin, draft["id"], product_id=product.id, quantity=2, unit_price_override=3.0
    )
    item_id = body["items"][0]["id"]
    assert body["subtotal"] == 6.0
    resp = await client.patch(
        f"{BASE}/{draft['id']}/items/{item_id}",
        json={"unit_price_override": None},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    line = body["items"][0]
    assert line["unit_price_override"] is None
    assert line["effective_unit_price"] == 10.0  # back to live catalog price
    assert body["subtotal"] == 20.0
    assert body["total"] == 20.0


async def test_remove_item_recomputes_totals(client, user_factory, product_factory, db_session):
    admin = user_factory(email="do-admin11@example.com", role=UserRole.ADMIN)
    p1 = product_factory(price_usd=10.0)
    p2 = product_factory(price_usd=4.0)
    draft = await _create_draft(client, admin)
    await _add_item(client, admin, draft["id"], product_id=p1.id, quantity=1)
    body = await _add_item(client, admin, draft["id"], product_id=p2.id, quantity=1)
    assert body["subtotal"] == 14.0
    victim = next(i for i in body["items"] if i["product_id"] == p2.id)
    resp = await client.delete(
        f"{BASE}/{draft['id']}/items/{victim['id']}", headers=_headers(admin)
    )
    assert resp.status_code == 204
    detail = (await client.get(f"{BASE}/{draft['id']}", headers=_headers(admin))).json()
    assert len(detail["items"]) == 1
    assert detail["subtotal"] == 10.0
    assert detail["total"] == 10.0
    assert (
        db_session.query(DraftOrderItem).filter(DraftOrderItem.id == victim["id"]).first() is None
    )


async def test_item_404s(client, user_factory, product_factory):
    admin = user_factory(email="do-admin12@example.com", role=UserRole.ADMIN)
    draft = await _create_draft(client, admin)
    resp = await client.patch(
        f"{BASE}/{draft['id']}/items/999999", json={"quantity": 1}, headers=_headers(admin)
    )
    assert resp.status_code == 404
    resp = await client.post(
        f"{BASE}/999999/items", json={"product_id": 1, "quantity": 1}, headers=_headers(admin)
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# header patch / discount / totals
# ---------------------------------------------------------------------------


async def test_patch_header_fields(client, user_factory, db_session):
    admin = user_factory(email="do-admin13@example.com", role=UserRole.ADMIN)
    draft = await _create_draft(client, admin, currency="EUR")
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        f"{BASE}/{draft['id']}",
        json={
            "guest_email": "gast@example.com",
            "shipping_address": "ul. Testowa 1",
            "shipping_city": "Kraków",
            "shipping_postal_code": "30-001",
            "shipping_country": "PL",
            "notes": "leave at door",
            "currency": "USD",  # unknown field on the update schema — ignored
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["guest_email"] == "gast@example.com"
    assert body["shipping_address"] == "ul. Testowa 1"
    assert body["shipping_city"] == "Kraków"
    assert body["shipping_postal_code"] == "30-001"
    assert body["shipping_country"] == "PL"
    assert body["notes"] == "leave at door"
    assert body["currency"] == "EUR"  # pinned at create — immutable via API
    assert _audit_count(db_session, "update") == before + 1


async def test_patch_unknown_shipping_method_404(client, user_factory):
    admin = user_factory(email="do-admin14@example.com", role=UserRole.ADMIN)
    draft = await _create_draft(client, admin)
    resp = await client.patch(
        f"{BASE}/{draft['id']}", json={"shipping_method_id": 999999}, headers=_headers(admin)
    )
    assert resp.status_code == 404


async def test_discount_reduces_total_and_clamps_at_zero(client, user_factory, product_factory):
    admin = user_factory(email="do-admin15@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=10.0)
    draft = await _create_draft(client, admin)
    await _add_item(client, admin, draft["id"], product_id=product.id, quantity=2)
    resp = await client.patch(
        f"{BASE}/{draft['id']}", json={"discount": 5.0}, headers=_headers(admin)
    )
    body = resp.json()
    assert body["discount"] == 5.0
    assert body["subtotal"] == 20.0
    assert body["total"] == 15.0
    # discount > subtotal → total clamps at 0 (subtotal untouched)
    resp = await client.patch(
        f"{BASE}/{draft['id']}", json={"discount": 999.0}, headers=_headers(admin)
    )
    body = resp.json()
    assert body["subtotal"] == 20.0
    assert body["total"] == 0.0


async def test_patch_404(client, user_factory):
    admin = user_factory(email="do-admin16@example.com", role=UserRole.ADMIN)
    resp = await client.patch(f"{BASE}/999999", json={"notes": "x"}, headers=_headers(admin))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# completed-status lock (conversion is PR 3 — status flipped directly in DB)
# ---------------------------------------------------------------------------


async def test_completed_draft_locks_all_mutations(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="do-admin17@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=10.0)
    draft = await _create_draft(client, admin)
    body = await _add_item(client, admin, draft["id"], product_id=product.id, quantity=1)
    item_id = body["items"][0]["id"]
    _complete(db_session, draft["id"])

    h = _headers(admin)
    assert (
        await client.patch(f"{BASE}/{draft['id']}", json={"notes": "x"}, headers=h)
    ).status_code == 409
    assert (
        await client.post(
            f"{BASE}/{draft['id']}/items",
            json={"product_id": product.id, "quantity": 1},
            headers=h,
        )
    ).status_code == 409
    assert (
        await client.patch(f"{BASE}/{draft['id']}/items/{item_id}", json={"quantity": 2}, headers=h)
    ).status_code == 409
    assert (
        await client.delete(f"{BASE}/{draft['id']}/items/{item_id}", headers=h)
    ).status_code == 409
    assert (await client.delete(f"{BASE}/{draft['id']}", headers=h)).status_code == 409
    # Still readable, totals untouched.
    detail = await client.get(f"{BASE}/{draft['id']}", headers=h)
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert detail.json()["subtotal"] == 10.0


# ---------------------------------------------------------------------------
# delete / cascade / stale product line
# ---------------------------------------------------------------------------


async def test_delete_cascades_items_and_writes_audit(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="do-admin18@example.com", role=UserRole.ADMIN)
    product = product_factory()
    draft = await _create_draft(client, admin)
    await _add_item(client, admin, draft["id"], product_id=product.id, quantity=1)
    before = _audit_count(db_session, "delete")
    resp = await client.delete(f"{BASE}/{draft['id']}", headers=_headers(admin))
    assert resp.status_code == 204
    assert _audit_count(db_session, "delete") == before + 1
    assert db_session.get(DraftOrder, draft["id"]) is None
    assert (
        db_session.query(DraftOrderItem).filter(DraftOrderItem.draft_order_id == draft["id"]).all()
        == []
    )


async def test_deleted_product_leaves_visibly_stale_line(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="do-admin19@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=10.0)
    draft = await _create_draft(client, admin)
    await _add_item(client, admin, draft["id"], product_id=product.id, quantity=2)
    db_session.delete(product)
    db_session.commit()

    detail = (await client.get(f"{BASE}/{draft['id']}", headers=_headers(admin))).json()
    line = detail["items"][0]
    assert line["product_id"] is None  # SET NULL
    assert line["product_name"] is None  # visibly stale
    assert line["effective_unit_price"] is None
    assert line["line_total"] is None
    # Stored totals still show the last computed values; the next mutation
    # recomputes them without the stale line's contribution.
    resp = await client.patch(
        f"{BASE}/{draft['id']}", json={"discount": 0.0}, headers=_headers(admin)
    )
    assert resp.json()["subtotal"] == 0.0
    assert resp.json()["total"] == 0.0


# ---------------------------------------------------------------------------
# list / filters / pagination / get
# ---------------------------------------------------------------------------


async def test_list_filters_status_and_paginates(client, user_factory, db_session):
    admin = user_factory(email="do-admin20@example.com", role=UserRole.ADMIN)
    d1 = await _create_draft(client, admin)
    d2 = await _create_draft(client, admin)
    d3 = await _create_draft(client, admin)
    _complete(db_session, d3["id"])

    resp = await client.get(f"{BASE}/", params={"status": "open"}, headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    ids = {row["id"] for row in body["items"]}
    assert d1["id"] in ids and d2["id"] in ids
    assert d3["id"] not in ids
    # list rows are lightweight: totals + item_count, no nested items
    assert "items" not in body["items"][0]
    assert body["items"][0]["item_count"] == 0

    resp = await client.get(f"{BASE}/", params={"page": 1, "page_size": 1}, headers=_headers(admin))
    body = resp.json()
    assert body["page_size"] == 1
    assert len(body["items"]) == 1
    assert body["total"] >= 3
    assert body["pages"] == body["total"]


async def test_get_404(client, user_factory):
    admin = user_factory(email="do-admin21@example.com", role=UserRole.ADMIN)
    assert (await client.get(f"{BASE}/999999", headers=_headers(admin))).status_code == 404
