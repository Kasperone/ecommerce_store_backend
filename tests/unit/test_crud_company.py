"""Unit tests for ``CRUDCompany`` (DB-backed, no HTTP).

Covers create + flush-assigned id, the ``status`` filter, ``search`` over both
name and tax_id, pagination ordering (newest first), partial update, and delete.
Validation (``status`` in the allowed set, non-blank name) is enforced by the
Pydantic schema, so it's exercised in the endpoint tests rather than here — the
CRUD layer trusts a validated ``CompanyCreate`` / ``CompanyUpdate``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.crud import company as crud_company
from app.models.company import Company
from app.schemas.company import CompanyCreate, CompanyUpdate
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def company_factory(db_session: Session):
    """Callable creating a Company row via the factory (session bound per-test)."""
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Company:
        return CompanyFactory(**kwargs)

    return _make


def test_create_assigns_id_and_persists(db_session: Session):
    obj_in = CompanyCreate(name="Acme Corp")
    created = crud_company.create(db_session, obj_in=obj_in)
    assert created.id is not None
    assert created.name == "Acme Corp"
    assert created.status == "pending"  # default
    assert created.tax_id is None
    assert created.email is None
    assert created.phone is None
    assert created.account_manager is None
    assert created.notes is None


def test_create_keeps_all_optional_fields(db_session: Session):
    obj_in = CompanyCreate(
        name="Globex",
        tax_id="PL1234567890",
        email="contact@globex.example",
        phone="+48 22 000 0000",
        status="approved",
        account_manager="Jane Doe",
        notes="Key B2B account",
    )
    created = crud_company.create(db_session, obj_in=obj_in)
    assert created.tax_id == "PL1234567890"
    assert created.email == "contact@globex.example"
    assert created.phone == "+48 22 000 0000"
    assert created.status == "approved"
    assert created.account_manager == "Jane Doe"
    assert created.notes == "Key B2B account"


def test_get_multi_filters_status(db_session: Session, company_factory):
    company_factory(name="PendingCo", status="pending")
    company_factory(name="ApprovedCo", status="approved")
    pending = crud_company.get_multi(db_session, status="pending")
    assert all(c.status == "pending" for c in pending)
    approved = crud_company.get_multi(db_session, status="approved")
    assert all(c.status == "approved" for c in approved)
    assert {"PendingCo"}.issubset({c.name for c in pending})
    assert {"ApprovedCo"}.issubset({c.name for c in approved})


def test_get_multi_search_by_name_substring(db_session: Session, company_factory):
    company_factory(name="Northwind Traders")
    company_factory(name="Contoso Ltd")
    company_factory(name="Fabrikam Inc")
    hits = crud_company.get_multi(db_session, search="northwind")
    names = {c.name for c in hits}
    assert "Northwind Traders" in names
    assert "Contoso Ltd" not in names


def test_get_multi_search_by_tax_id_substring(db_session: Session, company_factory):
    company_factory(name="TaxA", tax_id="PL9998887776")
    company_factory(name="TaxB", tax_id="DE111222333")
    hits = crud_company.get_multi(db_session, search="999888")
    names = {c.name for c in hits}
    assert "TaxA" in names
    assert "TaxB" not in names


def test_count_respects_filters(db_session: Session, company_factory):
    company_factory(name="CNT-A", status="pending")
    company_factory(name="CNT-B", status="pending")
    company_factory(name="CNT-C", status="approved")
    assert crud_company.count(db_session) >= 3
    assert crud_company.count(db_session, search="CNT-") == 3
    assert crud_company.count(db_session, search="CNT-", status="pending") == 2


def test_get_multi_pagination_newest_first(db_session: Session, company_factory):
    first = company_factory(name="PAGE-1")
    second = company_factory(name="PAGE-2")
    page = crud_company.get_multi(db_session, search="PAGE-", skip=0, limit=1)
    assert len(page) == 1
    # Newest (highest id) first.
    assert page[0].id == max(first.id, second.id)


def test_update_applies_only_sent_fields(db_session: Session, company_factory):
    c = company_factory(name="UPD1", status="pending")
    crud_company.update(db_session, company=c, obj_in=CompanyUpdate(status="approved"))
    db_session.commit()
    db_session.refresh(c)
    assert c.status == "approved"
    assert c.name == "UPD1"  # untouched


def test_delete_removes_row(db_session: Session, company_factory):
    c = company_factory(name="DEL1")
    cid = c.id
    crud_company.delete(db_session, company=c)
    db_session.commit()
    assert db_session.get(Company, cid) is None
