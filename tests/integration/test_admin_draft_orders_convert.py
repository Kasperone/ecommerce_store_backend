"""Integration tests for POST /api/v1/admin/draft-orders/{id}/convert.

Covers the happy path (customer owner, override + catalog pricing, vat_rate
snapshot, totals = subtotal - discount + shipping, stock decrement + movement
ledger, audit rows, draft completed + marker), the guest-owner path
(guest_email + generated guest_token), every eligibility 422/409 (no items,
stale line, no owner, missing address, missing shipping method, insufficient
stock, variant-required, no rate for the country, InPost locker method),
double-convert 409, ``mark_as_paid`` (PAID + faktura row in the same txn),
``send_email`` (confirmation + paid enqueues), and the discount clamp.

Reuses the draft-composition helpers from ``test_admin_draft_orders`` and the
variant author from ``test_admin_quotes``. The email background task is
stubbed like ``test_order_emails`` (the real task opens its own session that
cannot see the savepoint-isolated rows).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.admin_audit_event import AdminAuditEvent
from app.models.inventory_movement import InventoryMovement, InventoryMovementReason
from app.models.invoice import Invoice
from app.models.order import Order, OrderItem
from app.models.shipping import ShippingMethod, ShippingRate, ShippingZone
from app.models.user import UserRole
from app.services import order_email
from tests.integration.test_admin_draft_orders import _add_item, _create_draft, _headers
from tests.integration.test_admin_quotes import _add_one_variant

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin/draft-orders"

ADDRESS = {
    "shipping_address": "ul. Testowa 1",
    "shipping_city": "Kraków",
    "shipping_postal_code": "30-001",
    "shipping_country": "PL",
}


@pytest.fixture
def scheduled_emails(monkeypatch) -> list[tuple[int, str]]:
    """Capture (order_id, event) the route schedules, without opening a session."""
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        order_email,
        "send_order_event_in_background",
        lambda order_id, event: calls.append((order_id, event)),
    )
    return calls


def _audit_count(db: Session, *, resource_type: str, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == resource_type, AdminAuditEvent.action == action)
        .count()
    )


def _seed_rated_method(
    db: Session, *, code: str, amount: float, currency: str = "USD"
) -> ShippingMethod:
    """Add a method + rate to the session-seeded "Test Zone" (savepoint-scoped)."""
    zone = db.query(ShippingZone).filter(ShippingZone.name == "Test Zone").one()
    method = ShippingMethod(name=f"Method {code}", code=code, is_active=True, sort_order=50)
    db.add(method)
    db.flush()
    db.add(ShippingRate(zone_id=zone.id, method_id=method.id, currency=currency, amount=amount))
    db.commit()
    return method


async def _ready_draft(
    client,
    admin,
    *,
    shipping_method_id: int,
    owner: dict,
    items: list[dict],
    header: dict | None = None,
) -> dict:
    """Compose a fully convertible draft: owner + items + address + method."""
    draft = await _create_draft(client, admin, **owner)
    for line in items:
        await _add_item(client, admin, draft["id"], **line)
    patch = {**ADDRESS, "shipping_method_id": shipping_method_id, **(header or {})}
    resp = await client.patch(f"{BASE}/{draft['id']}", json=patch, headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _convert(client, admin, draft_id: int, body: dict | None = None):
    return await client.post(f"{BASE}/{draft_id}/convert", json=body or {}, headers=_headers(admin))


# ---------------------------------------------------------------------------
# authz / 404
# ---------------------------------------------------------------------------


async def test_convert_requires_admin(client, user_factory):
    customer = user_factory(email="doc-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.post(f"{BASE}/1/convert", json={})).status_code == 401
    assert (
        await client.post(f"{BASE}/1/convert", json={}, headers=_headers(customer))
    ).status_code == 403


async def test_convert_404(client, user_factory):
    admin = user_factory(email="doc-admin404@example.com", role=UserRole.ADMIN)
    assert (await _convert(client, admin, 999999)).status_code == 404


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------


async def test_convert_happy_path_customer_owner(
    client, user_factory, product_factory, db_session, scheduled_emails
):
    admin = user_factory(email="doc-admin1@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner1@example.com", role=UserRole.CUSTOMER)
    catalog_prod = product_factory(stock=10, price_usd=10.0, name="CatalogProd", vat_rate=8)
    override_prod = product_factory(stock=10, price_usd=20.0, name="OverrideProd")
    # Non-zero rate so the total provably includes the shipping line.
    method = _seed_rated_method(db_session, code="courier-x", amount=7.5)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=method.id,
        owner={"user_id": customer.id},
        items=[
            {"product_id": catalog_prod.id, "quantity": 2},
            {"product_id": override_prod.id, "quantity": 1, "unit_price_override": 15.0},
        ],
        header={"discount": 5.0},
    )

    order_creates = _audit_count(db_session, resource_type="order", action="create")
    draft_updates = _audit_count(db_session, resource_type="draft_order", action="update")
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["status"] == "pending"
    assert body["user_id"] == customer.id
    assert body["guest_email"] is None
    assert body["currency"] == "USD"
    # subtotal = 2x10 (catalog) + 1x15 (override) = 35; total = 35 - 5 + 7.50
    assert body["subtotal"] == 35.0
    assert body["discount"] == 5.0
    assert body["shipping"] == 7.5
    assert body["total"] == 37.5
    assert body["payment_method"] == "manual"
    assert body["shipping_method_id"] == method.id
    assert body["shipping_address"] == ADDRESS["shipping_address"]
    assert body["shipping_country"] == "PL"
    assert f"Converted from draft order #{draft['id']}" in body["notes"]
    by_product = {item["product_id"]: item for item in body["items"]}
    assert by_product[catalog_prod.id]["unit_price"] == 10.0  # live catalog price
    assert by_product[override_prod.id]["unit_price"] == 15.0  # override honoured
    assert by_product[catalog_prod.id]["product_name"] == "CatalogProd"

    # vat_rate snapshotted from the product (8% reduced rate; default is 23).
    vat_item = (
        db_session.query(OrderItem)
        .filter(OrderItem.order_id == body["id"], OrderItem.product_id == catalog_prod.id)
        .one()
    )
    assert vat_item.vat_rate == 8

    # Audit: one order-create + one draft_order-update row.
    assert _audit_count(db_session, resource_type="order", action="create") == order_creates + 1
    assert (
        _audit_count(db_session, resource_type="draft_order", action="update") == draft_updates + 1
    )

    # Draft completed + conversion marker set; further edits are locked (409).
    detail = (await client.get(f"{BASE}/{draft['id']}", headers=_headers(admin))).json()
    assert detail["status"] == "completed"
    assert detail["converted_order_id"] == body["id"]
    locked = await client.patch(
        f"{BASE}/{draft['id']}", json={"notes": "x"}, headers=_headers(admin)
    )
    assert locked.status_code == 409

    # send_email defaulted to False → nothing enqueued.
    assert scheduled_emails == []


async def test_convert_guest_owner_generates_token(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    admin = user_factory(email="doc-admin2@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"guest_email": "gosc@example.com"},
        items=[{"product_id": product.id, "quantity": 1}],
    )
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["user_id"] is None
    assert body["guest_email"] == "gosc@example.com"
    order = db_session.get(Order, body["id"])
    assert order is not None
    assert order.guest_token  # non-empty opaque tracking token
    assert order.guest_locale is None


async def test_convert_writes_inventory_movements_and_decrements_stock(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    admin = user_factory(email="doc-admin3@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner3@example.com", role=UserRole.CUSTOMER)
    p1 = product_factory(stock=10, price_usd=10.0)
    p2 = product_factory(stock=10, price_usd=4.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"user_id": customer.id},
        items=[
            {"product_id": p1.id, "quantity": 3},
            {"product_id": p2.id, "quantity": 2},
        ],
    )
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 201, resp.text
    order_id = resp.json()["id"]

    movements = (
        db_session.query(InventoryMovement)
        .filter(
            InventoryMovement.order_id == order_id,
            InventoryMovement.reason == InventoryMovementReason.ORDER_PLACED.value,
        )
        .all()
    )
    assert len(movements) == 2  # one ORDER_PLACED row per line
    deltas = {m.product_id: m.delta for m in movements}
    assert deltas[p1.id] == -3
    assert deltas[p2.id] == -2
    assert {m.actor_user_id for m in movements} == {admin.id}
    db_session.refresh(p1)
    db_session.refresh(p2)
    assert p1.stock == 7
    assert p2.stock == 8


async def test_convert_discount_larger_than_subtotal_clamps(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="doc-admin4@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner4@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"user_id": customer.id},
        items=[{"product_id": product.id, "quantity": 2}],
        header={"discount": 999.0},
    )
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # discount clamps to the subtotal, never below-zero goods before shipping.
    assert body["subtotal"] == 20.0
    assert body["discount"] == 20.0
    assert body["total"] == 0.0  # seeded test rate is 0.00


# ---------------------------------------------------------------------------
# mark_as_paid / send_email flags
# ---------------------------------------------------------------------------


async def test_convert_mark_as_paid_issues_faktura(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    admin = user_factory(email="doc-admin5@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner5@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"user_id": customer.id},
        items=[{"product_id": product.id, "quantity": 1}],
    )
    resp = await _convert(
        client, admin, draft["id"], {"payment_method": "cod", "mark_as_paid": True}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "paid"
    assert body["payment_method"] == "cod"
    invoice = db_session.query(Invoice).filter(Invoice.order_id == body["id"]).one_or_none()
    assert invoice is not None  # gap-free faktura issued in the same txn
    assert invoice.invoice_number


async def test_convert_send_email_enqueues_events(
    client, user_factory, product_factory, scheduled_emails, test_shipping_method_id
):
    admin = user_factory(email="doc-admin6@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"guest_email": "mailme@example.com"},
        items=[{"product_id": product.id, "quantity": 1}],
    )
    resp = await _convert(client, admin, draft["id"], {"send_email": True, "mark_as_paid": True})
    assert resp.status_code == 201, resp.text
    order_id = resp.json()["id"]
    assert (order_id, "confirmation") in scheduled_emails
    assert (order_id, "paid") in scheduled_emails


# ---------------------------------------------------------------------------
# eligibility errors
# ---------------------------------------------------------------------------


async def test_convert_double_convert_409(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="doc-admin7@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner7@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"user_id": customer.id},
        items=[{"product_id": product.id, "quantity": 1}],
    )
    assert (await _convert(client, admin, draft["id"])).status_code == 201
    second = await _convert(client, admin, draft["id"])
    assert second.status_code == 409
    assert "converted" in second.json()["detail"].lower()


async def test_convert_no_items_422(client, user_factory, test_shipping_method_id):
    admin = user_factory(email="doc-admin8@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner8@example.com", role=UserRole.CUSTOMER)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"user_id": customer.id},
        items=[],
    )
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 422
    assert "line items" in resp.json()["detail"].lower()


async def test_convert_stale_line_422(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    admin = user_factory(email="doc-admin9@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner9@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"user_id": customer.id},
        items=[{"product_id": product.id, "quantity": 1}],
    )
    db_session.delete(product)  # SET NULL → visibly stale line
    db_session.commit()
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 422
    assert "deleted product" in resp.json()["detail"].lower()


async def test_convert_no_owner_422(client, user_factory, product_factory, test_shipping_method_id):
    admin = user_factory(email="doc-admin10@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={},  # neither user_id nor guest_email
        items=[{"product_id": product.id, "quantity": 1}],
    )
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 422
    assert "customer" in resp.json()["detail"].lower()


async def test_convert_missing_address_422(client, user_factory, product_factory):
    admin = user_factory(email="doc-admin11@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner11@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _create_draft(client, admin, user_id=customer.id)
    await _add_item(client, admin, draft["id"], product_id=product.id, quantity=1)
    # Partial address (no city/postal/country) — conversion must refuse.
    resp = await client.patch(
        f"{BASE}/{draft['id']}",
        json={"shipping_address": "ul. Testowa 1"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 422
    assert "address" in resp.json()["detail"].lower()


async def test_convert_missing_shipping_method_422(client, user_factory, product_factory):
    admin = user_factory(email="doc-admin12@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner12@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _create_draft(client, admin, user_id=customer.id)
    await _add_item(client, admin, draft["id"], product_id=product.id, quantity=1)
    resp = await client.patch(f"{BASE}/{draft['id']}", json=ADDRESS, headers=_headers(admin))
    assert resp.status_code == 200
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 422
    assert "shipping method" in resp.json()["detail"].lower()


async def test_convert_insufficient_stock_422(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="doc-admin13@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner13@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=1, allow_backorder=False, price_usd=10.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"user_id": customer.id},
        items=[{"product_id": product.id, "quantity": 9}],
    )
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 422
    assert "stock" in resp.json()["detail"].lower()


async def test_convert_variant_required_422(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    """A product that gains variants after the line was drafted must 422, never
    silently convert at product grain (same posture as quote conversion)."""
    admin = user_factory(email="doc-admin14@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner14@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"user_id": customer.id},
        items=[{"product_id": product.id, "quantity": 1}],
    )
    _add_one_variant(db_session, product)  # product now REQUIRES a variant
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 422
    assert "variant" in resp.json()["detail"].lower()


async def test_convert_no_rate_for_country_422(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="doc-admin15@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner15@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=5, price_usd=10.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=test_shipping_method_id,
        owner={"user_id": customer.id},
        items=[{"product_id": product.id, "quantity": 1}],
        header={"shipping_country": "SE"},  # not in the seeded Test Zone
    )
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 422


async def test_convert_inpost_locker_method_unsupported_422(
    client, user_factory, product_factory, db_session
):
    """Drafts carry no Paczkomat point, so an InPost locker method must map to
    a clear 422 rather than the point-required error checkout raises."""
    admin = user_factory(email="doc-admin16@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="doc-owner16@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=5, price_usd=10.0)
    inpost = _seed_rated_method(db_session, code="inpost_paczkomat", amount=3.0)
    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=inpost.id,
        owner={"user_id": customer.id},
        items=[{"product_id": product.id, "quantity": 1}],
    )
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 422
    assert "inpost" in resp.json()["detail"].lower()
