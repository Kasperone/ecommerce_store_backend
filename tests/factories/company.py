"""Factory-boy factory for Company.

Session binding follows the same pattern as ``CampaignFactory`` — populated
dynamically by a per-test fixture. Defaults to a pending company with no contact
details; pass ``status="approved"`` etc. to override.
"""

from __future__ import annotations

import factory
from factory.alchemy import SQLAlchemyModelFactory

from app.models.company import Company


class CompanyFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = Company
        sqlalchemy_session = None  # populated by the company_factory fixture
        sqlalchemy_session_persistence = "commit"

    name = factory.Sequence(lambda n: f"Company {n}")
    tax_id = None
    email = None
    phone = None
    status = "pending"
    account_manager = None
    notes = None
