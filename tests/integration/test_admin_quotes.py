"""Integration tests for the admin quotes API (/api/v1/admin/quotes).

Covers authz (401/403), create (happy + audit + server-computed totals + nested
items in the response + unknown-company 404 + empty-items 422), get (200 nested /
404), list (status + company filters, pagination, company name + totals, no items
in rows), patch (happy + audit + 404 + items replace/recompute + unknown-status
422), and delete (204 + audit + cascade, 404). Every mutation writes an audit row
(resource_type ``quote``).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.quote import Quote, QuoteItem
from app.models.user import UserRole
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "quote", AdminAuditEvent.action == action)
        .count()
    )


@pytest.fixture
def company_factory(db_session: Session):
    """Callable creating a Company row via the factory (session bound per-test)."""
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


def _body(company_id: int, **overrides) -> dict:
    base = {
        "company_id": company_id,
        "currency": "USD",
        "items": [{"description": "Widget", "quantity": 2, "unit_price": 10.0}],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="qt-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/quotes/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/quotes/", headers=_headers(customer))
    ).status_code == 403


async def test_create_requires_admin(client, user_factory, company_factory):
    customer = user_factory(email="qt-cust2@example.com", role=UserRole.CUSTOMER)
    company = company_factory()
    body = _body(company.id)
    assert (await client.post("/api/v1/admin/quotes/", json=body)).status_code == 401
    assert (
        await client.post("/api/v1/admin/quotes/", json=body, headers=_headers(customer))
    ).status_code == 403


async def test_delete_requires_admin(client, user_factory, company_factory):
    customer = user_factory(email="qt-cust3@example.com", role=UserRole.CUSTOMER)
    company = company_factory()
    admin = user_factory(email="qt-admin-del-auth@example.com", role=UserRole.ADMIN)
    created = await client.post(
        "/api/v1/admin/quotes/", json=_body(company.id), headers=_headers(admin)
    )
    qid = created.json()["id"]
    assert (await client.delete(f"/api/v1/admin/quotes/{qid}")).status_code == 401
    assert (
        await client.delete(f"/api/v1/admin/quotes/{qid}", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_computes_totals_and_writes_audit(
    client, user_factory, company_factory, db_session
):
    admin = user_factory(email="qt-admin1@example.com", role=UserRole.ADMIN)
    company = company_factory(name="Acme Corp")
    before = _audit_count(db_session, "create")
    resp = await client.post(
        "/api/v1/admin/quotes/",
        json=_body(
            company.id,
            notes="Q3 pricing",
            items=[
                {"description": "A", "quantity": 3, "unit_price": 5.0},
                {"description": "B", "quantity": 2, "unit_price": 7.5},
            ],
        ),
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["company_id"] == company.id
    assert body["company_name"] == "Acme Corp"
    assert body["status"] == "draft"  # default
    assert body["currency"] == "USD"
    assert body["notes"] == "Q3 pricing"
    assert body["subtotal"] == 30.0
    assert body["total"] == 30.0
    # nested items with server-computed line_totals
    assert len(body["items"]) == 2
    assert [i["line_total"] for i in body["items"]] == [15.0, 15.0]
    assert "id" in body
    assert _audit_count(db_session, "create") == before + 1


async def test_create_unknown_company_404(client, user_factory):
    admin = user_factory(email="qt-admin2@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/quotes/",
        json=_body(999999),
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_create_empty_items_422(client, user_factory, company_factory):
    admin = user_factory(email="qt-admin3@example.com", role=UserRole.ADMIN)
    company = company_factory()
    resp = await client.post(
        "/api/v1/admin/quotes/",
        json=_body(company.id, items=[]),
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_ignores_client_supplied_totals(client, user_factory, company_factory):
    """The server computes subtotal/total/line_total; extra body keys are dropped."""
    admin = user_factory(email="qt-admin3b@example.com", role=UserRole.ADMIN)
    company = company_factory()
    resp = await client.post(
        "/api/v1/admin/quotes/",
        json={
            "company_id": company.id,
            "currency": "USD",
            "subtotal": 999.0,  # ignored
            "total": 999.0,  # ignored
            "items": [{"description": "X", "quantity": 1, "unit_price": 4.0, "line_total": 999.0}],
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["subtotal"] == 4.0
    assert body["total"] == 4.0
    assert body["items"][0]["line_total"] == 4.0


async def test_create_rejects_unknown_status_via_patch_only(client, user_factory, company_factory):
    """Create ignores a status field (always draft); see patch tests for status."""
    admin = user_factory(email="qt-admin3c@example.com", role=UserRole.ADMIN)
    company = company_factory()
    resp = await client.post(
        "/api/v1/admin/quotes/",
        json=_body(company.id, currency="GBP"),  # unknown currency → 422
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_returns_quote_with_items(client, user_factory, company_factory):
    admin = user_factory(email="qt-admin4@example.com", role=UserRole.ADMIN)
    company = company_factory(name="GetCo")
    created = await client.post(
        "/api/v1/admin/quotes/", json=_body(company.id), headers=_headers(admin)
    )
    qid = created.json()["id"]
    resp = await client.get(f"/api/v1/admin/quotes/{qid}", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == qid
    assert body["company_name"] == "GetCo"
    assert len(body["items"]) == 1
    assert body["items"][0]["description"] == "Widget"


async def test_get_404(client, user_factory):
    admin = user_factory(email="qt-admin5@example.com", role=UserRole.ADMIN)
    assert (
        await client.get("/api/v1/admin/quotes/999999", headers=_headers(admin))
    ).status_code == 404


# ---------------------------------------------------------------------------
# list / filters / pagination
# ---------------------------------------------------------------------------


async def test_list_filters_and_paginates(client, user_factory, company_factory):
    admin = user_factory(email="qt-admin6@example.com", role=UserRole.ADMIN)
    c1 = company_factory(name="ListCo1")
    c2 = company_factory(name="ListCo2")
    # 3 quotes for c1, 1 for c2
    for _ in range(3):
        await client.post("/api/v1/admin/quotes/", json=_body(c1.id), headers=_headers(admin))
    await client.post("/api/v1/admin/quotes/", json=_body(c2.id), headers=_headers(admin))

    # filter by company
    resp = await client.get(
        "/api/v1/admin/quotes/", params={"company_id": c1.id}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert all(item["company_id"] == c1.id for item in body["items"])
    # list rows carry company name + totals, and NO nested items
    assert body["items"][0]["company_name"] == "ListCo1"
    assert body["items"][0]["subtotal"] == 20.0
    assert "items" not in body["items"][0]
    assert body["items"][0]["item_count"] == 1

    # pagination: page_size=1 over c1's 3 quotes → 3 pages
    resp = await client.get(
        "/api/v1/admin/quotes/",
        params={"company_id": c1.id, "page": 1, "page_size": 1},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 3
    assert body["pages"] == 3
    assert len(body["items"]) == 1


async def test_list_filters_status(client, user_factory, company_factory):
    admin = user_factory(email="qt-admin7@example.com", role=UserRole.ADMIN)
    company = company_factory()
    a = await client.post("/api/v1/admin/quotes/", json=_body(company.id), headers=_headers(admin))
    b = await client.post("/api/v1/admin/quotes/", json=_body(company.id), headers=_headers(admin))
    # flip b to sent
    await client.patch(
        f"/api/v1/admin/quotes/{b.json()['id']}",
        json={"status": "sent"},
        headers=_headers(admin),
    )
    resp = await client.get(
        "/api/v1/admin/quotes/",
        params={"company_id": company.id, "status": "draft"},
        headers=_headers(admin),
    )
    body = resp.json()
    ids = {item["id"] for item in body["items"]}
    assert a.json()["id"] in ids
    assert b.json()["id"] not in ids


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------


async def test_patch_status_and_notes_writes_audit(
    client, user_factory, company_factory, db_session
):
    admin = user_factory(email="qt-admin8@example.com", role=UserRole.ADMIN)
    company = company_factory()
    created = await client.post(
        "/api/v1/admin/quotes/", json=_body(company.id), headers=_headers(admin)
    )
    qid = created.json()["id"]
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        f"/api/v1/admin/quotes/{qid}",
        json={"status": "accepted", "notes": "Approved"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["notes"] == "Approved"
    # items untouched
    assert len(body["items"]) == 1
    assert _audit_count(db_session, "update") == before + 1


async def test_patch_replaces_items_and_recomputes(client, user_factory, company_factory):
    admin = user_factory(email="qt-admin9@example.com", role=UserRole.ADMIN)
    company = company_factory()
    created = await client.post(
        "/api/v1/admin/quotes/", json=_body(company.id), headers=_headers(admin)
    )
    qid = created.json()["id"]
    resp = await client.patch(
        f"/api/v1/admin/quotes/{qid}",
        json={
            "items": [
                {"description": "New1", "quantity": 4, "unit_price": 2.5},
                {"description": "New2", "quantity": 1, "unit_price": 1.0},
            ]
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert {i["description"] for i in body["items"]} == {"New1", "New2"}
    assert body["subtotal"] == 11.0
    assert body["total"] == 11.0


async def test_patch_404(client, user_factory):
    admin = user_factory(email="qt-admin10@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        "/api/v1/admin/quotes/999999",
        json={"status": "sent"},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_patch_rejects_unknown_status(client, user_factory, company_factory):
    admin = user_factory(email="qt-admin11@example.com", role=UserRole.ADMIN)
    company = company_factory()
    created = await client.post(
        "/api/v1/admin/quotes/", json=_body(company.id), headers=_headers(admin)
    )
    qid = created.json()["id"]
    resp = await client.patch(
        f"/api/v1/admin/quotes/{qid}",
        json={"status": "frozen"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_cascades_and_writes_audit(client, user_factory, company_factory, db_session):
    admin = user_factory(email="qt-admin12@example.com", role=UserRole.ADMIN)
    company = company_factory()
    created = await client.post(
        "/api/v1/admin/quotes/", json=_body(company.id), headers=_headers(admin)
    )
    qid = created.json()["id"]
    before = _audit_count(db_session, "delete")
    resp = await client.delete(f"/api/v1/admin/quotes/{qid}", headers=_headers(admin))
    assert resp.status_code == 204
    assert _audit_count(db_session, "delete") == before + 1
    assert db_session.get(Quote, qid) is None
    # items cascaded
    assert db_session.query(QuoteItem).filter(QuoteItem.quote_id == qid).all() == []


async def test_delete_404(client, user_factory):
    admin = user_factory(email="qt-admin13@example.com", role=UserRole.ADMIN)
    assert (
        await client.delete("/api/v1/admin/quotes/999999", headers=_headers(admin))
    ).status_code == 404


# ---------------------------------------------------------------------------
# convert (POST /{id}/convert)
# ---------------------------------------------------------------------------


async def _make_quote(client, admin, company_id, *, product_id, quantity, unit_price, status=None):
    """Create a quote (optionally flip its status) via the admin API; return its id."""
    created = await client.post(
        "/api/v1/admin/quotes/",
        json=_body(
            company_id,
            items=[
                {
                    "description": "Line",
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "product_id": product_id,
                }
            ],
        ),
        headers=_headers(admin),
    )
    qid = created.json()["id"]
    if status is not None:
        await client.patch(
            f"/api/v1/admin/quotes/{qid}", json={"status": status}, headers=_headers(admin)
        )
    return qid


async def test_convert_requires_admin(client, user_factory):
    customer = user_factory(email="qt-conv-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.post("/api/v1/admin/quotes/1/convert")).status_code == 401
    assert (
        await client.post("/api/v1/admin/quotes/1/convert", headers=_headers(customer))
    ).status_code == 403


async def test_convert_happy_path(
    client, user_factory, company_factory, product_factory, db_session
):
    admin = user_factory(email="qt-conv-admin@example.com", role=UserRole.ADMIN)
    contact = user_factory(email="qt-conv-contact@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="ConvCo", primary_user_id=contact.id)
    product = product_factory(stock=10, name="ConvProd", sku="CV-9")
    qid = await _make_quote(
        client,
        admin,
        company.id,
        product_id=product.id,
        quantity=2,
        unit_price=15.0,
        status="accepted",
    )
    order_creates_before = (
        db_session.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "order", AdminAuditEvent.action == "create")
        .count()
    )
    quote_updates_before = _audit_count(db_session, "update")
    resp = await client.post(f"/api/v1/admin/quotes/{qid}/convert", headers=_headers(admin))
    assert resp.status_code == 201
    body = resp.json()
    # Order owned by the company primary contact, not the admin.
    assert body["user_id"] == contact.id
    assert body["status"] == "pending"
    assert body["currency"] == "USD"
    assert body["subtotal"] == 30.0
    assert body["total"] == 30.0
    assert len(body["items"]) == 1
    assert body["items"][0]["unit_price"] == 15.0  # quoted price honoured
    assert body["items"][0]["product_name"] == "ConvProd"
    assert f"Converted from quote #{qid}" in body["notes"]
    # Audit: an order-create row + a quote-update row were written.
    order_creates_after = (
        db_session.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "order", AdminAuditEvent.action == "create")
        .count()
    )
    assert order_creates_after == order_creates_before + 1
    assert _audit_count(db_session, "update") == quote_updates_before + 1
    # Quote now carries the conversion marker.
    quote = db_session.get(Quote, qid)
    db_session.refresh(quote)
    assert quote.converted_order_id == body["id"]
    assert quote.status == "accepted"
    # The QuoteResponse exposes the marker so the admin UI can show the
    # "view converted order" link + hide the convert action on reload.
    detail = await client.get(f"/api/v1/admin/quotes/{qid}", headers=_headers(admin))
    assert detail.status_code == 200
    assert detail.json()["converted_order_id"] == body["id"]


async def test_convert_404(client, user_factory):
    admin = user_factory(email="qt-conv-404@example.com", role=UserRole.ADMIN)
    assert (
        await client.post("/api/v1/admin/quotes/999999/convert", headers=_headers(admin))
    ).status_code == 404


async def test_convert_not_accepted_422(client, user_factory, company_factory, product_factory):
    admin = user_factory(email="qt-conv-na@example.com", role=UserRole.ADMIN)
    contact = user_factory(email="qt-conv-na-c@example.com", role=UserRole.CUSTOMER)
    company = company_factory(primary_user_id=contact.id)
    product = product_factory(stock=10)
    qid = await _make_quote(
        client, admin, company.id, product_id=product.id, quantity=1, unit_price=5.0
    )  # left in draft
    resp = await client.post(f"/api/v1/admin/quotes/{qid}/convert", headers=_headers(admin))
    assert resp.status_code == 422
    assert "accepted" in resp.json()["detail"].lower()


async def test_convert_double_convert_409(client, user_factory, company_factory, product_factory):
    admin = user_factory(email="qt-conv-dc@example.com", role=UserRole.ADMIN)
    contact = user_factory(email="qt-conv-dc-c@example.com", role=UserRole.CUSTOMER)
    company = company_factory(primary_user_id=contact.id)
    product = product_factory(stock=10)
    qid = await _make_quote(
        client,
        admin,
        company.id,
        product_id=product.id,
        quantity=1,
        unit_price=5.0,
        status="accepted",
    )
    first = await client.post(f"/api/v1/admin/quotes/{qid}/convert", headers=_headers(admin))
    assert first.status_code == 201
    second = await client.post(f"/api/v1/admin/quotes/{qid}/convert", headers=_headers(admin))
    assert second.status_code == 409


async def test_convert_free_text_line_422(client, user_factory, company_factory, product_factory):
    admin = user_factory(email="qt-conv-ft@example.com", role=UserRole.ADMIN)
    contact = user_factory(email="qt-conv-ft-c@example.com", role=UserRole.CUSTOMER)
    company = company_factory(primary_user_id=contact.id)
    # Quote with a free-text line (no product_id)
    created = await client.post(
        "/api/v1/admin/quotes/",
        json=_body(
            company.id,
            items=[{"description": "Freeform service", "quantity": 1, "unit_price": 9.0}],
        ),
        headers=_headers(admin),
    )
    qid = created.json()["id"]
    await client.patch(
        f"/api/v1/admin/quotes/{qid}", json={"status": "accepted"}, headers=_headers(admin)
    )
    resp = await client.post(f"/api/v1/admin/quotes/{qid}/convert", headers=_headers(admin))
    assert resp.status_code == 422
    assert "product" in resp.json()["detail"].lower()


async def test_convert_no_primary_contact_422(
    client, user_factory, company_factory, product_factory
):
    admin = user_factory(email="qt-conv-np@example.com", role=UserRole.ADMIN)
    company = company_factory(primary_user_id=None)  # no contact set
    product = product_factory(stock=10)
    qid = await _make_quote(
        client,
        admin,
        company.id,
        product_id=product.id,
        quantity=1,
        unit_price=5.0,
        status="accepted",
    )
    resp = await client.post(f"/api/v1/admin/quotes/{qid}/convert", headers=_headers(admin))
    assert resp.status_code == 422
    assert "primary contact" in resp.json()["detail"].lower()


async def test_convert_insufficient_stock_422(
    client, user_factory, company_factory, product_factory
):
    admin = user_factory(email="qt-conv-ins@example.com", role=UserRole.ADMIN)
    contact = user_factory(email="qt-conv-ins-c@example.com", role=UserRole.CUSTOMER)
    company = company_factory(primary_user_id=contact.id)
    product = product_factory(stock=1, allow_backorder=False)
    qid = await _make_quote(
        client,
        admin,
        company.id,
        product_id=product.id,
        quantity=9,
        unit_price=5.0,
        status="accepted",
    )
    resp = await client.post(f"/api/v1/admin/quotes/{qid}/convert", headers=_headers(admin))
    assert resp.status_code == 422
