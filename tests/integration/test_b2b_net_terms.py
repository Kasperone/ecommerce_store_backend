"""Integration tests for B2B net terms — pay on account (Program 2, Shopify-gap P2 #14).

Covers the money path + admin/buyer surfaces:

- Eligibility: an active member of a net-terms-enabled company may place a
  pay-on-account order (201); a non-B2B account, or a B2B account whose company
  has net terms disabled, is refused (409 machine codes).
- Placement effects: the order carries ``payment_method="net_terms"``,
  ``company_id``, a ``payment_due_at`` (placement + net_terms_days), and the
  faktura is issued AT PLACEMENT (gap-free — pay-by-invoice instrument).
- Credit limit (hard block on this option only): an order that would push
  outstanding over the limit is refused (409 ``credit_limit_exceeded``); a
  differing currency is refused (409 ``net_terms_currency_mismatch``).
- Recording payment: an admin stamps ``net_terms_paid_at`` (idempotent; frees
  the credit); the credit summary endpoints reflect outstanding/available.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.crud.invoice import invoice as crud_invoice
from app.models.user import UserRole
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(
    product_id: int, *, shipping_method_id: int, quantity: int = 1, currency: str = "USD"
) -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "currency": currency,
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "PL",
        "shipping_method_id": shipping_method_id,
        "payment_method": "net_terms",
    }


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# Happy path + placement effects
# ---------------------------------------------------------------------------


async def test_net_terms_checkout_places_on_account(
    client, user_factory, product_factory, company_factory, db_session, test_shipping_method_id
):
    buyer = user_factory(email="nt-buyer@example.com")
    product = product_factory(stock=10, price_usd=19.99)
    company_factory(
        name="TermsCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        net_terms_days=30,
        default_currency="USD",
    )

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=2, shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["payment_method"] == "net_terms"
    assert body["company_id"] is not None
    assert body["payment_due_at"] is not None
    assert body["net_terms_paid_at"] is None
    # Placed unpaid (ships while unpaid) — status stays pending.
    assert body["status"] == "pending"

    # Faktura issued AT PLACEMENT (not on a later paid transition).
    invoice = crud_invoice.get_by_order_id(db_session, body["id"])
    assert invoice is not None
    assert invoice.invoice_number


async def test_net_terms_unavailable_for_non_b2b(
    client, user_factory, product_factory, test_shipping_method_id
):
    buyer = user_factory(email="nt-nonb2b@example.com")
    product = product_factory(stock=10, price_usd=19.99)
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "net_terms_unavailable"


async def test_net_terms_unavailable_when_disabled(
    client, user_factory, product_factory, company_factory, test_shipping_method_id
):
    buyer = user_factory(email="nt-disabled@example.com")
    product = product_factory(stock=10, price_usd=19.99)
    # B2B company, but net terms not enabled.
    company_factory(name="NoTermsCo", primary_user_id=buyer.id, net_terms_enabled=False)
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "net_terms_unavailable"


# ---------------------------------------------------------------------------
# Credit limit (hard block on this option only)
# ---------------------------------------------------------------------------


async def test_net_terms_credit_limit_blocks(
    client, user_factory, product_factory, company_factory, test_shipping_method_id
):
    buyer = user_factory(email="nt-overlimit@example.com")
    product = product_factory(stock=10, price_usd=100.00)
    company_factory(
        name="TightCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        net_terms_days=30,
        default_currency="USD",
        credit_limit=Decimal("50.00"),
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "credit_limit_exceeded"


async def test_net_terms_currency_mismatch_blocks(
    client, user_factory, product_factory, company_factory, test_shipping_method_id
):
    buyer = user_factory(email="nt-currency@example.com")
    product = product_factory(stock=10, price_usd=10.00)
    # Credit limit is denominated in PLN; a USD order can't be measured against it.
    company_factory(
        name="PlnCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        net_terms_days=30,
        default_currency="PLN",
        credit_limit=Decimal("1000.00"),
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, currency="USD", shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "net_terms_currency_mismatch"


# ---------------------------------------------------------------------------
# Record payment + credit summary
# ---------------------------------------------------------------------------


async def test_record_payment_and_credit_summary(
    client, user_factory, product_factory, company_factory, db_session, test_shipping_method_id
):
    buyer = user_factory(email="nt-record@example.com")
    admin = user_factory(email="nt-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=40.00)
    company = company_factory(
        name="LedgerCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        net_terms_days=30,
        default_currency="USD",
        credit_limit=Decimal("1000.00"),
    )

    order_resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert order_resp.status_code == 201, order_resp.text
    order_id = order_resp.json()["id"]
    order_total = order_resp.json()["total"]

    # Buyer credit summary shows the order as outstanding, credit reduced.
    credit = await client.get("/api/v1/companies/me/credit", headers=_headers(buyer))
    assert credit.status_code == 200, credit.text
    cbody = credit.json()
    assert cbody["net_terms_enabled"] is True
    assert cbody["outstanding"] == order_total
    assert cbody["available"] == pytest.approx(1000.00 - order_total)

    # Admin records the bank transfer → paid stamped, outstanding cleared.
    rec = await client.post(
        f"/api/v1/orders/{order_id}/record-net-terms-payment", headers=_headers(admin)
    )
    assert rec.status_code == 200, rec.text
    assert rec.json()["net_terms_paid_at"] is not None

    admin_credit = await client.get(
        f"/api/v1/admin/companies/{company.id}/credit", headers=_headers(admin)
    )
    assert admin_credit.status_code == 200, admin_credit.text
    assert admin_credit.json()["outstanding"] == 0.0
    assert admin_credit.json()["available"] == 1000.00

    # Idempotent — a second record is a no-op (still paid).
    rec2 = await client.post(
        f"/api/v1/orders/{order_id}/record-net-terms-payment", headers=_headers(admin)
    )
    assert rec2.status_code == 200, rec2.text


async def test_record_payment_rejects_non_net_terms_order(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="nt-admin2@example.com", role=UserRole.ADMIN)
    buyer = user_factory(email="nt-plain@example.com")
    product = product_factory(stock=10, price_usd=10.00)
    # A plain (non-net-terms) order.
    body = {
        "items": [{"product_id": product.id, "quantity": 1}],
        "currency": "USD",
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "PL",
        "shipping_method_id": test_shipping_method_id,
    }
    order_resp = await client.post("/api/v1/orders/", json=body, headers=_headers(buyer))
    assert order_resp.status_code == 201, order_resp.text
    order_id = order_resp.json()["id"]

    rec = await client.post(
        f"/api/v1/orders/{order_id}/record-net-terms-payment", headers=_headers(admin)
    )
    assert rec.status_code == 409
    assert rec.json()["detail"] == "not_a_net_terms_order"


async def test_credit_endpoint_null_for_non_b2b(client, user_factory):
    user = user_factory(email="nt-nomember@example.com")
    resp = await client.get("/api/v1/companies/me/credit", headers=_headers(user))
    assert resp.status_code == 200, resp.text
    assert resp.json()["net_terms_enabled"] is False
