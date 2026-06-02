"""Unit tests for the team-management ``crud_company_user`` helpers
(TM-be / Wave D slice 5): ``invite`` / ``update_member`` / ``remove``.

Kept in a separate file from ``test_crud_company_user.py`` (CU-be's, which covers
the foundational resolution helpers) so the parallel-run boundary is clean.

Covers:

- ``invite`` stages an ``invited`` membership linked to an existing user, with
  ``invited_email`` / ``invited_at`` stamped.
- ``update_member`` applies role and/or status partially (only provided fields).
- ``remove`` deletes the row.
"""

from __future__ import annotations

import pytest
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


def test_invite_stages_invited_membership(db_session, user_factory, company_factory):
    user = user_factory(email="inviteme@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="InviteCrudCo")

    membership = crud_company_user.invite(
        db_session,
        company_id=company.id,
        user_id=user.id,
        email="inviteme@example.com",
        role=CompanyRole.BUYER.value,
    )
    db_session.commit()

    assert membership.id is not None
    assert membership.user_id == user.id
    assert membership.role == CompanyRole.BUYER.value
    assert membership.status == CompanyMemberStatus.INVITED.value
    assert membership.invited_email == "inviteme@example.com"
    assert membership.invited_at is not None


def test_update_member_changes_role_only(
    db_session, user_factory, company_factory, company_user_factory
):
    user = user_factory(email="updrole@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="UpdRoleCo")
    member = company_user_factory(
        company_id=company.id, user_id=user.id, role=CompanyRole.BUYER.value
    )

    updated = crud_company_user.update_member(
        db_session, membership=member, role=CompanyRole.COMPANY_ADMIN.value
    )
    db_session.commit()

    assert updated.role == CompanyRole.COMPANY_ADMIN.value
    # Status untouched (None means "leave as-is").
    assert updated.status == CompanyMemberStatus.ACTIVE.value


def test_update_member_changes_status_only(
    db_session, user_factory, company_factory, company_user_factory
):
    user = user_factory(email="updstatus@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="UpdStatusCo")
    member = company_user_factory(
        company_id=company.id, user_id=user.id, role=CompanyRole.BUYER.value
    )

    updated = crud_company_user.update_member(
        db_session, membership=member, status=CompanyMemberStatus.SUSPENDED.value
    )
    db_session.commit()

    assert updated.status == CompanyMemberStatus.SUSPENDED.value
    # Role untouched.
    assert updated.role == CompanyRole.BUYER.value


def test_remove_deletes_membership(db_session, user_factory, company_factory, company_user_factory):
    user = user_factory(email="removeme@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="RemoveCrudCo")
    member = company_user_factory(
        company_id=company.id, user_id=user.id, role=CompanyRole.BUYER.value
    )
    member_id = member.id

    crud_company_user.remove(db_session, membership=member)
    db_session.commit()

    assert (
        crud_company_user.get_membership(db_session, company_id=company.id, user_id=user.id) is None
    )
    assert (
        db_session.query(CompanyUserFactory._meta.model)  # pyright: ignore[reportAttributeAccessIssue]
        .filter_by(id=member_id)
        .first()
        is None
    )
