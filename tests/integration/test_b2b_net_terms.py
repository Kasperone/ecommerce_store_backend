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
from app.crud import store_config as crud_store_config
from app.crud.invoice import invoice as crud_invoice
from app.models.company_user import CompanyMemberStatus, CompanyRole
from app.models.order import Order, OrderStatus
from app.models.payment_term import PaymentTerm
from app.models.user import UserRole
from app.services.b2b_net_terms import outstanding_balance
from tests.factories.company import CompanyFactory
from tests.factories.company_user import CompanyUserFactory

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


@pytest.fixture
def payment_term_factory(db_session: Session):
    """Create + commit a PaymentTerm (defaults to a Net 30 net_days term)."""

    def _make(
        term_type: str = "net_days", *, net_days=30, due_date=None, is_active=True, name=None
    ):
        term = PaymentTerm(
            name=name or f"Term {term_type}",
            term_type=term_type,
            net_days=net_days if term_type == "net_days" else None,
            due_date=due_date if term_type == "fixed_date" else None,
            is_active=is_active,
        )
        db_session.add(term)
        db_session.commit()
        return term

    return _make


@pytest.fixture
def company_user_factory(db_session: Session):
    """Create + commit a CompanyUser membership (defaults to active COMPANY_ADMIN)."""
    CompanyUserFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyUserFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyUserFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# Happy path + placement effects
# ---------------------------------------------------------------------------


async def test_net_terms_checkout_places_on_account(
    client,
    user_factory,
    product_factory,
    company_factory,
    payment_term_factory,
    db_session,
    test_shipping_method_id,
):
    buyer = user_factory(email="nt-buyer@example.com")
    product = product_factory(stock=10, price_usd=19.99)
    company_factory(
        name="TermsCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        payment_term=payment_term_factory(net_days=30),
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
    client,
    user_factory,
    product_factory,
    company_factory,
    payment_term_factory,
    test_shipping_method_id,
):
    buyer = user_factory(email="nt-overlimit@example.com")
    product = product_factory(stock=10, price_usd=100.00)
    company_factory(
        name="TightCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        payment_term=payment_term_factory(net_days=30),
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
    client,
    user_factory,
    product_factory,
    company_factory,
    payment_term_factory,
    test_shipping_method_id,
):
    buyer = user_factory(email="nt-currency@example.com")
    product = product_factory(stock=10, price_usd=10.00)
    # Credit limit is denominated in PLN; a USD order can't be measured against it.
    company_factory(
        name="PlnCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        payment_term=payment_term_factory(net_days=30),
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
    client,
    user_factory,
    product_factory,
    company_factory,
    payment_term_factory,
    db_session,
    test_shipping_method_id,
):
    buyer = user_factory(email="nt-record@example.com")
    admin = user_factory(email="nt-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=40.00)
    company = company_factory(
        name="LedgerCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        payment_term=payment_term_factory(net_days=30),
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


async def test_admin_create_persists_net_terms_and_currency(
    client, user_factory, payment_term_factory
):
    """A company created through the admin endpoint with net terms + a billing
    currency persists all of it (regression: ``default_currency`` was previously
    dropped at create, leaving a credit-limited company unable to bill on account)."""
    admin = user_factory(email="nt-create-admin@example.com", role=UserRole.ADMIN)
    term = payment_term_factory(net_days=45, name="Net 45")
    resp = await client.post(
        "/api/v1/admin/companies/",
        json={
            "name": "Create NT Co",
            "status": "approved",
            "net_terms_enabled": True,
            "payment_term_id": term.id,
            "net_terms_allowed_roles": ["COMPANY_ADMIN", "BUYER"],
            "credit_limit": 500.0,
            "default_currency": "PLN",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["net_terms_enabled"] is True
    assert body["payment_term"]["id"] == term.id
    assert body["payment_term"]["net_days"] == 45
    assert body["net_terms_allowed_roles"] == ["COMPANY_ADMIN", "BUYER"]
    assert body["credit_limit"] == 500.0
    assert body["default_currency"] == "PLN"


async def test_admin_create_rejects_unknown_payment_term(client, user_factory):
    """Assigning a non-existent payment term at create is a 422."""
    admin = user_factory(email="nt-badterm@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/companies/",
        json={"name": "Bad Term Co", "status": "approved", "payment_term_id": 999999},
        headers=_headers(admin),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Payment term not found"


# ---------------------------------------------------------------------------
# Term types (due-date semantics) + role gating (Program A)
# ---------------------------------------------------------------------------


async def test_due_on_receipt_term_due_today(
    client,
    user_factory,
    product_factory,
    company_factory,
    payment_term_factory,
    test_shipping_method_id,
):
    """A due_on_receipt term places the order due the same day it's placed."""
    from datetime import UTC, datetime

    buyer = user_factory(email="nt-receipt@example.com")
    product = product_factory(stock=10, price_usd=10.00)
    company_factory(
        name="ReceiptCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        payment_term=payment_term_factory("due_on_receipt"),
        default_currency="USD",
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 201, resp.text
    due = datetime.fromisoformat(resp.json()["payment_due_at"])
    assert due.date() == datetime.now(UTC).date()


async def test_fixed_date_term_uses_the_date(
    client,
    user_factory,
    product_factory,
    company_factory,
    payment_term_factory,
    test_shipping_method_id,
):
    """A fixed_date term places the order due on the term's calendar date."""
    from datetime import date, datetime

    buyer = user_factory(email="nt-fixed@example.com")
    product = product_factory(stock=10, price_usd=10.00)
    deadline = date(2099, 12, 31)
    company_factory(
        name="FixedCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        payment_term=payment_term_factory("fixed_date", due_date=deadline),
        default_currency="USD",
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 201, resp.text
    due = datetime.fromisoformat(resp.json()["payment_due_at"])
    assert due.date() == deadline


async def test_role_gating_blocks_viewer(
    client,
    user_factory,
    product_factory,
    company_factory,
    company_user_factory,
    payment_term_factory,
    test_shipping_method_id,
):
    """A VIEWER member of a net-terms company cannot pay on account (option hidden
    → 409 net_terms_unavailable), while the default roles can."""
    viewer = user_factory(email="nt-viewer@example.com")
    product = product_factory(stock=10, price_usd=10.00)
    # No primary_user_id (that fallback would synthesize COMPANY_ADMIN) — the
    # viewer's eligibility must come purely from their VIEWER membership row.
    company = company_factory(
        name="GatedCo",
        net_terms_enabled=True,
        payment_term=payment_term_factory(net_days=30),
        default_currency="USD",
    )
    company_user_factory(
        company=company,
        user=viewer,
        role=CompanyRole.VIEWER.value,
        status=CompanyMemberStatus.ACTIVE.value,
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(viewer),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "net_terms_unavailable"


async def test_role_gating_allows_buyer(
    client,
    user_factory,
    product_factory,
    company_factory,
    company_user_factory,
    payment_term_factory,
    test_shipping_method_id,
):
    """A BUYER member (in the default allowed set) can pay on account."""
    b2b_buyer = user_factory(email="nt-gatedbuyer@example.com")
    product = product_factory(stock=10, price_usd=10.00)
    company = company_factory(
        name="BuyerOkCo",
        net_terms_enabled=True,
        payment_term=payment_term_factory(net_days=30),
        default_currency="USD",
    )
    company_user_factory(
        company=company,
        user=b2b_buyer,
        role=CompanyRole.BUYER.value,
        status=CompanyMemberStatus.ACTIVE.value,
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(b2b_buyer),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["payment_method"] == "net_terms"


# ---------------------------------------------------------------------------
# Cross-currency credit (Program D) — FX conversion of orders into the limit currency
# ---------------------------------------------------------------------------


def _set_fx_rates(db_session, rates: dict) -> None:
    config = crud_store_config.get_singleton(db_session)
    config.fx_rates = rates
    db_session.add(config)
    db_session.commit()


async def test_cross_currency_within_limit_places(
    client,
    user_factory,
    product_factory,
    company_factory,
    payment_term_factory,
    db_session,
    test_shipping_method_id,
):
    """A PLN order against a USD-denominated limit is converted via FX rates and
    placed when it fits (200 PLN x 0.25 = 50 USD ≤ 100 USD limit)."""
    _set_fx_rates(db_session, {"USD": 1, "PLN": 0.25})
    buyer = user_factory(email="fx-ok@example.com")
    product = product_factory(stock=10, price_pln=200.00)
    company_factory(
        name="FxOkCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        payment_term=payment_term_factory(net_days=30),
        default_currency="USD",
        credit_limit=Decimal("100.00"),
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, currency="PLN", shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["payment_method"] == "net_terms"


async def test_cross_currency_over_limit_blocks(
    client,
    user_factory,
    product_factory,
    company_factory,
    payment_term_factory,
    db_session,
    test_shipping_method_id,
):
    """A PLN order whose converted value exceeds the USD limit is refused
    (600 PLN x 0.25 = 150 USD > 100 USD)."""
    _set_fx_rates(db_session, {"USD": 1, "PLN": 0.25})
    buyer = user_factory(email="fx-over@example.com")
    product = product_factory(stock=10, price_pln=200.00)
    company_factory(
        name="FxOverCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        payment_term=payment_term_factory(net_days=30),
        default_currency="USD",
        credit_limit=Decimal("100.00"),
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id, quantity=3, currency="PLN", shipping_method_id=test_shipping_method_id
        ),
        headers=_headers(buyer),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "credit_limit_exceeded"


async def test_cross_currency_no_rate_rejects(
    client,
    user_factory,
    product_factory,
    company_factory,
    payment_term_factory,
    db_session,
    test_shipping_method_id,
):
    """Without an FX rate for the order currency, a mismatched-currency on-account
    order still rejects (pre-Program-D fallback) — 409 net_terms_currency_mismatch."""
    _set_fx_rates(db_session, {})  # no rates configured
    buyer = user_factory(email="fx-none@example.com")
    product = product_factory(stock=10, price_pln=50.00)
    company_factory(
        name="FxNoneCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        payment_term=payment_term_factory(net_days=30),
        default_currency="USD",
        credit_limit=Decimal("1000.00"),
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, currency="PLN", shipping_method_id=test_shipping_method_id),
        headers=_headers(buyer),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "net_terms_currency_mismatch"


def _make_net_terms_order(db, company, *, order_number, currency, total):
    order = Order(
        order_number=order_number,
        status=OrderStatus.PENDING,
        subtotal=total,
        total=total,
        currency=currency,
        shipping_address="1 Main St",
        shipping_city="Warsaw",
        shipping_postal_code="00-001",
        shipping_country="PL",
        guest_email="ar@example.com",
        payment_method="net_terms",
        company_id=company.id,
    )
    db.add(order)
    db.commit()
    return order


def test_outstanding_balance_sums_across_currencies(db_session, company_factory):
    """Outstanding AR converts each currency's sub-total into the billing currency."""
    _set_fx_rates(db_session, {"USD": 1, "PLN": 0.25})
    company = company_factory(name="FxArCo", default_currency="USD", net_terms_enabled=True)
    _make_net_terms_order(db_session, company, order_number="AR-USD", currency="USD", total=40)
    _make_net_terms_order(db_session, company, order_number="AR-PLN", currency="PLN", total=200)
    # 40 USD + (200 PLN x 0.25 = 50 USD) = 90 USD
    assert outstanding_balance(db_session, company=company) == Decimal("90.00")


def test_outstanding_balance_none_when_rate_missing(db_session, company_factory):
    """A currency in the AR with no FX rate makes the balance unmeasurable (None)."""
    _set_fx_rates(db_session, {"USD": 1})  # no PLN rate
    company = company_factory(name="FxArMissing", default_currency="USD", net_terms_enabled=True)
    _make_net_terms_order(db_session, company, order_number="AR2-PLN", currency="PLN", total=200)
    assert outstanding_balance(db_session, company=company) is None
