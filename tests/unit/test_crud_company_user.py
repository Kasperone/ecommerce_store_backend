"""Unit tests for ``crud_company_user`` (B2B membership resolution).

Covers (Wave D / B2B-2 slice 3):

- ``get_active_membership`` resolves an ``active`` ``company_users`` row → company
  + role; honours the membership role (not always COMPANY_ADMIN).
- ``get_active_membership`` falls back to the denormalized
  ``companies.primary_user_id`` pointer (synthesized COMPANY_ADMIN) when no
  explicit membership row exists — this is the Wave-C backward-compat path.
- An explicit membership row takes precedence over / shadows the pointer.
- ``invited`` / ``suspended`` memberships do NOT resolve (only ``active``).
- A user who is neither a member nor a primary contact → ``None``.
- ``get_membership`` / ``list_for_company`` / ``count_active_admins`` /
  ``create`` foundational helpers + the unique ``(company_id, user_id)``
  constraint.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crud import company_user as crud_company_user
from app.models.company_user import CompanyMemberStatus, CompanyRole
from app.models.user import UserRole
from tests.factories.company import CompanyFactory
from tests.factories.company_user import CompanyUserFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


@pytest.fixture
def company_user_factory(db_session: Session):
    CompanyUserFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyUserFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyUserFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# get_active_membership
# ---------------------------------------------------------------------------


def test_active_membership_resolves_row(
    db_session, user_factory, company_factory, company_user_factory
):
    user = user_factory(email="member@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Acme")
    company_user_factory(company_id=company.id, user_id=user.id, role=CompanyRole.BUYER.value)

    membership = crud_company_user.get_active_membership(db_session, user_id=user.id)
    assert membership is not None
    assert membership.company.id == company.id
    # The stored role is honoured (NOT coerced to COMPANY_ADMIN).
    assert membership.role == CompanyRole.BUYER.value
    assert membership.company_user is not None


def test_active_membership_falls_back_to_primary_contact(db_session, user_factory, company_factory):
    """A primary contact with NO membership row resolves as COMPANY_ADMIN.

    This is the Wave-C backward-compat path — the quotes router + the existing
    customer tests create B2B accounts via ``primary_user_id`` only.
    """
    user = user_factory(email="primary@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Contact Co", primary_user_id=user.id)

    membership = crud_company_user.get_active_membership(db_session, user_id=user.id)
    assert membership is not None
    assert membership.company.id == company.id
    assert membership.role == CompanyRole.COMPANY_ADMIN.value
    # No concrete row — synthesized from the pointer.
    assert membership.company_user is None


def test_active_membership_row_takes_precedence_over_pointer(
    db_session, user_factory, company_factory, company_user_factory
):
    """An explicit membership row wins over the denormalized pointer's role."""
    user = user_factory(email="both@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Both Co", primary_user_id=user.id)
    company_user_factory(company_id=company.id, user_id=user.id, role=CompanyRole.VIEWER.value)

    membership = crud_company_user.get_active_membership(db_session, user_id=user.id)
    assert membership is not None
    assert membership.company_user is not None
    assert membership.role == CompanyRole.VIEWER.value


@pytest.mark.parametrize(
    "bad_status", [CompanyMemberStatus.INVITED.value, CompanyMemberStatus.SUSPENDED.value]
)
def test_non_active_membership_does_not_resolve(
    db_session, user_factory, company_factory, company_user_factory, bad_status
):
    user = user_factory(email=f"{bad_status}@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name=f"{bad_status} Co")
    company_user_factory(company_id=company.id, user_id=user.id, status=bad_status)

    membership = crud_company_user.get_active_membership(db_session, user_id=user.id)
    assert membership is None


def test_active_membership_none_for_non_b2b(db_session, user_factory):
    user = user_factory(email="nob2b@example.com", role=UserRole.CUSTOMER)
    assert crud_company_user.get_active_membership(db_session, user_id=user.id) is None


# ---------------------------------------------------------------------------
# get_membership / list_for_company / count_active_admins / create
# ---------------------------------------------------------------------------


def test_get_membership_returns_row_regardless_of_status(
    db_session, user_factory, company_factory, company_user_factory
):
    user = user_factory(email="getm@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="GetM Co")
    company_user_factory(
        company_id=company.id, user_id=user.id, status=CompanyMemberStatus.SUSPENDED.value
    )

    row = crud_company_user.get_membership(db_session, company_id=company.id, user_id=user.id)
    assert row is not None
    assert row.status == CompanyMemberStatus.SUSPENDED.value
    # Non-member → None.
    assert (
        crud_company_user.get_membership(db_session, company_id=company.id, user_id=999999) is None
    )


def test_list_for_company(db_session, user_factory, company_factory, company_user_factory):
    company = company_factory(name="ListCo")
    other = company_factory(name="OtherCo")
    u1 = user_factory(email="l1@example.com", role=UserRole.CUSTOMER)
    u2 = user_factory(email="l2@example.com", role=UserRole.CUSTOMER)
    u3 = user_factory(email="l3@example.com", role=UserRole.CUSTOMER)
    company_user_factory(company_id=company.id, user_id=u1.id)
    company_user_factory(company_id=company.id, user_id=u2.id, role=CompanyRole.BUYER.value)
    company_user_factory(company_id=other.id, user_id=u3.id)  # must NOT appear

    rows = crud_company_user.list_for_company(db_session, company_id=company.id)
    assert len(rows) == 2
    assert {r.user_id for r in rows} == {u1.id, u2.id}


def test_count_active_admins(db_session, user_factory, company_factory, company_user_factory):
    company = company_factory(name="AdminCount")
    u1 = user_factory(email="a1@example.com", role=UserRole.CUSTOMER)
    u2 = user_factory(email="a2@example.com", role=UserRole.CUSTOMER)
    u3 = user_factory(email="a3@example.com", role=UserRole.CUSTOMER)
    company_user_factory(company_id=company.id, user_id=u1.id)  # active admin
    company_user_factory(
        company_id=company.id,
        user_id=u2.id,
        status=CompanyMemberStatus.SUSPENDED.value,  # suspended admin — not counted
    )
    company_user_factory(
        company_id=company.id, user_id=u3.id, role=CompanyRole.BUYER.value
    )  # active but not admin

    assert crud_company_user.count_active_admins(db_session, company_id=company.id) == 1


def test_create_membership(db_session, user_factory, company_factory):
    user = user_factory(email="create@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="CreateCo")

    membership = crud_company_user.create(
        db_session,
        company_id=company.id,
        user_id=user.id,
        role=CompanyRole.APPROVER.value,
    )
    db_session.commit()
    assert membership.id is not None
    assert membership.role == CompanyRole.APPROVER.value
    assert membership.status == CompanyMemberStatus.ACTIVE.value


def test_unique_company_user_constraint(
    db_session, user_factory, company_factory, company_user_factory
):
    user = user_factory(email="dup@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="DupCo")
    company_user_factory(company_id=company.id, user_id=user.id)

    with pytest.raises(IntegrityError):
        # Second membership for the same (company, user) violates the unique key.
        CompanyUserFactory(company_id=company.id, user_id=user.id)
