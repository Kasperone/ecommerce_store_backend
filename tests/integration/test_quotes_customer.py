"""Integration tests for the customer-facing quotes + companies API.

Covers (Wave C / B2B-1):

- ``GET /companies/me`` — B2B caller gets their company; non-B2B → 404; auth required.
- ``POST /quotes`` — server re-prices each line from the CURRENT product price (a
  client-sent price is ignored); status=``requested``; ``requested_by_user_id`` =
  caller; non-B2B caller → 403 ``not_b2b``; missing/inactive product → 422; empty
  items → 422.
- ``GET /quotes`` + ``GET /quotes/{id}`` — company-scoped; another company's quote
  is a 404 (existence not leaked); non-B2B list → [].
- ``POST /quotes/{id}/accept`` — ``sent`` → ``accepted``; other source states → 409;
  cross-company → 404.
- ``GET /quotes/{id}/quote.pdf`` — non-empty application/pdf; cross-company → 404.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.quote import Quote
from app.models.user import UserRole
from tests.factories.company import CompanyFactory
from tests.factories.quote import QuoteFactory, QuoteItemFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


@pytest.fixture
def quote_factory(db_session: Session):
    for factory in (QuoteFactory, QuoteItemFactory):
        factory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
        factory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return QuoteFactory(**kwargs)

    return _make


def _b2b_user(user_factory, company_factory, *, email: str):
    """A verified customer who is a company's primary contact (a B2B account)."""
    user = user_factory(email=email, role=UserRole.CUSTOMER)
    company = company_factory(name="Acme B2B", primary_user_id=user.id)
    return user, company


# ---------------------------------------------------------------------------
# GET /companies/me
# ---------------------------------------------------------------------------


async def test_companies_me_requires_auth(client):
    assert (await client.get("/api/v1/companies/me")).status_code == 401


async def test_companies_me_returns_b2b_company(client, user_factory, company_factory):
    user, company = _b2b_user(user_factory, company_factory, email="b2b-me@example.com")
    resp = await client.get("/api/v1/companies/me", headers=_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == company.id
    assert body["name"] == "Acme B2B"
    assert body["status"] == "pending"


async def test_companies_me_null_for_non_b2b(client, user_factory):
    """Non-B2B caller → 200 with a null body (not 404): the FE probes this per
    authed page, so it must not 404-spam the browser console."""
    user = user_factory(email="not-b2b@example.com", role=UserRole.CUSTOMER)
    resp = await client.get("/api/v1/companies/me", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json() is None


# ---------------------------------------------------------------------------
# POST /quotes — request + server re-pricing
# ---------------------------------------------------------------------------


async def test_request_quote_reprices_server_side(
    client, user_factory, company_factory, product_factory, db_session
):
    user, company = _b2b_user(user_factory, company_factory, email="req-price@example.com")
    product = product_factory(price_usd=42.50, price_pln=170.0, price_eur=39.0)

    resp = await client.post(
        "/api/v1/quotes/",
        json={
            "currency": "USD",
            # A bogus client price is sent — it must be ignored entirely.
            "items": [{"product_id": product.id, "quantity": 3, "unit_price": 0.01}],
        },
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "requested"
    assert body["company_id"] == company.id
    assert body["currency"] == "USD"
    assert len(body["items"]) == 1
    line = body["items"][0]
    # Priced from the CURRENT product price in USD — NOT the client's 0.01.
    assert line["unit_price"] == 42.50
    assert line["line_total"] == 127.50
    assert line["product_id"] == product.id
    assert body["subtotal"] == 127.50
    assert body["total"] == 127.50

    # requested_by_user_id stored on the row.
    quote = db_session.query(Quote).filter(Quote.id == body["id"]).first()
    assert quote is not None
    assert quote.requested_by_user_id == user.id


async def test_request_quote_prices_in_requested_currency(
    client, user_factory, company_factory, product_factory
):
    user, _ = _b2b_user(user_factory, company_factory, email="req-pln@example.com")
    product = product_factory(price_usd=42.50, price_pln=170.0, price_eur=39.0)
    resp = await client.post(
        "/api/v1/quotes/",
        json={"currency": "PLN", "items": [{"product_id": product.id, "quantity": 1}]},
        headers=_headers(user),
    )
    assert resp.status_code == 201
    assert resp.json()["items"][0]["unit_price"] == 170.0


async def test_request_quote_non_b2b_forbidden(client, user_factory, product_factory):
    user = user_factory(email="req-nonb2b@example.com", role=UserRole.CUSTOMER)
    product = product_factory()
    resp = await client.post(
        "/api/v1/quotes/",
        json={"currency": "USD", "items": [{"product_id": product.id, "quantity": 1}]},
        headers=_headers(user),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "not_b2b"


async def test_request_quote_unknown_product_422(client, user_factory, company_factory):
    user, _ = _b2b_user(user_factory, company_factory, email="req-noprod@example.com")
    resp = await client.post(
        "/api/v1/quotes/",
        json={"currency": "USD", "items": [{"product_id": 999999, "quantity": 1}]},
        headers=_headers(user),
    )
    assert resp.status_code == 422


async def test_request_quote_inactive_product_422(
    client, user_factory, company_factory, product_factory
):
    user, _ = _b2b_user(user_factory, company_factory, email="req-inactive@example.com")
    product = product_factory(is_active=False)
    resp = await client.post(
        "/api/v1/quotes/",
        json={"currency": "USD", "items": [{"product_id": product.id, "quantity": 1}]},
        headers=_headers(user),
    )
    assert resp.status_code == 422


async def test_request_quote_empty_items_422(client, user_factory, company_factory):
    user, _ = _b2b_user(user_factory, company_factory, email="req-empty@example.com")
    resp = await client.post(
        "/api/v1/quotes/",
        json={"currency": "USD", "items": []},
        headers=_headers(user),
    )
    assert resp.status_code == 422


async def test_request_quote_requires_auth(client):
    assert (
        await client.post("/api/v1/quotes/", json={"currency": "USD", "items": []})
    ).status_code == 401


# ---------------------------------------------------------------------------
# GET /quotes + GET /quotes/{id} — company-scoped
# ---------------------------------------------------------------------------


async def test_list_quotes_company_scoped(client, user_factory, company_factory, quote_factory):
    user, company = _b2b_user(user_factory, company_factory, email="list-mine@example.com")
    other_company = company_factory(name="Other Co")
    quote_factory(company=company, status="requested")
    quote_factory(company=company, status="sent")
    quote_factory(company=other_company, status="sent")  # must NOT appear

    resp = await client.get("/api/v1/quotes/", headers=_headers(user))
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    assert {r["company_id"] for r in rows} == {company.id}


async def test_list_quotes_non_b2b_empty(client, user_factory):
    user = user_factory(email="list-nonb2b@example.com", role=UserRole.CUSTOMER)
    resp = await client.get("/api/v1/quotes/", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_quote_detail(client, user_factory, company_factory, quote_factory):
    user, company = _b2b_user(user_factory, company_factory, email="detail@example.com")
    quote = quote_factory(company=company, status="sent")
    QuoteItemFactory(quote=quote, description="Widget", quantity=2, unit_price=5.0, line_total=10.0)

    resp = await client.get(f"/api/v1/quotes/{quote.id}", headers=_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == quote.id
    assert len(body["items"]) == 1
    assert body["items"][0]["description"] == "Widget"


async def test_get_quote_cross_company_404(client, user_factory, company_factory, quote_factory):
    user, _ = _b2b_user(user_factory, company_factory, email="cross-get@example.com")
    other_company = company_factory(name="Other Co 2")
    other_quote = quote_factory(company=other_company, status="sent")
    resp = await client.get(f"/api/v1/quotes/{other_quote.id}", headers=_headers(user))
    assert resp.status_code == 404


async def test_get_quote_non_b2b_404(client, user_factory, company_factory, quote_factory):
    user = user_factory(email="get-nonb2b@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Detached Co")
    quote = quote_factory(company=company, status="sent")
    resp = await client.get(f"/api/v1/quotes/{quote.id}", headers=_headers(user))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /quotes/{id}/accept
# ---------------------------------------------------------------------------


async def test_accept_sent_quote(client, user_factory, company_factory, quote_factory):
    user, company = _b2b_user(user_factory, company_factory, email="accept-ok@example.com")
    quote = quote_factory(company=company, status="sent")
    resp = await client.post(f"/api/v1/quotes/{quote.id}/accept", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


@pytest.mark.parametrize("bad_status", ["draft", "requested", "accepted", "rejected", "expired"])
async def test_accept_non_sent_409(
    client, user_factory, company_factory, quote_factory, bad_status
):
    user, company = _b2b_user(
        user_factory, company_factory, email=f"accept-{bad_status}@example.com"
    )
    quote = quote_factory(company=company, status=bad_status)
    resp = await client.post(f"/api/v1/quotes/{quote.id}/accept", headers=_headers(user))
    assert resp.status_code == 409


async def test_accept_cross_company_404(client, user_factory, company_factory, quote_factory):
    user, _ = _b2b_user(user_factory, company_factory, email="accept-cross@example.com")
    other_company = company_factory(name="Other Co 3")
    other_quote = quote_factory(company=other_company, status="sent")
    resp = await client.post(f"/api/v1/quotes/{other_quote.id}/accept", headers=_headers(user))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /quotes/{id}/quote.pdf
# ---------------------------------------------------------------------------


async def test_quote_pdf_download(client, user_factory, company_factory, quote_factory):
    user, company = _b2b_user(user_factory, company_factory, email="pdf@example.com")
    quote = quote_factory(company=company, status="sent", subtotal=10.0, total=10.0)
    QuoteItemFactory(
        quote=quote, description="Gadget", quantity=1, unit_price=10.0, line_total=10.0
    )

    resp = await client.get(f"/api/v1/quotes/{quote.id}/quote.pdf", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert len(resp.content) > 0
    assert resp.content[:5] == b"%PDF-"


async def test_quote_pdf_polish(client, user_factory, company_factory, quote_factory):
    user, company = _b2b_user(user_factory, company_factory, email="pdf-pl@example.com")
    quote = quote_factory(company=company, status="sent")
    QuoteItemFactory(quote=quote, description="Gadżet", quantity=1, unit_price=5.0, line_total=5.0)
    resp = await client.get(f"/api/v1/quotes/{quote.id}/quote.pdf?lang=pl", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.content[:5] == b"%PDF-"


async def test_quote_pdf_cross_company_404(client, user_factory, company_factory, quote_factory):
    user, _ = _b2b_user(user_factory, company_factory, email="pdf-cross@example.com")
    other_company = company_factory(name="Other Co 4")
    other_quote = quote_factory(company=other_company, status="sent")
    resp = await client.get(f"/api/v1/quotes/{other_quote.id}/quote.pdf", headers=_headers(user))
    assert resp.status_code == 404
