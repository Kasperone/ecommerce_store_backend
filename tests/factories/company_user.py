"""Factory-boy factory for CompanyUser (B2B membership).

Session binding follows the ``CompanyFactory`` pattern — populated dynamically by
a per-test fixture. Defaults to an active COMPANY_ADMIN membership; pass
``role=`` / ``status=`` to override. ``company`` and ``user`` are supplied by the
test (SubFactories are avoided so the test controls the exact rows being linked).
"""

from __future__ import annotations

from factory.alchemy import SQLAlchemyModelFactory

from app.models.company_user import CompanyMemberStatus, CompanyRole, CompanyUser


class CompanyUserFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = CompanyUser
        sqlalchemy_session = None  # populated by the company_user_factory fixture
        sqlalchemy_session_persistence = "commit"

    role = CompanyRole.COMPANY_ADMIN.value
    status = CompanyMemberStatus.ACTIVE.value
    invited_email = None
    invited_at = None
