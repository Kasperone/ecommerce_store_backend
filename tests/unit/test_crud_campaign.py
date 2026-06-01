"""Unit tests for ``CRUDCampaign`` (DB-backed, no HTTP).

Covers create + flush-assigned id, the ``status`` / ``audience`` / ``search``
filters, pagination ordering (newest first), partial update, and delete.
Validation (the ``scheduled``-requires-``scheduled_at`` rule) is enforced by the
Pydantic schema, so it's exercised in the schema/endpoint tests rather than here
— the CRUD layer trusts a validated ``CampaignCreate`` / ``CampaignUpdate``.
``sent_at`` is never written by the CRUD layer (future send-layer concern).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.crud import campaign as crud_campaign
from app.models.campaign import Campaign
from app.schemas.campaign import CampaignCreate, CampaignUpdate
from tests.factories.campaign import CampaignFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def campaign_factory(db_session: Session):
    """Callable creating a Campaign row via the factory (session bound per-test)."""
    CampaignFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    CampaignFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Campaign:
        return CampaignFactory(**kwargs)

    return _make


def test_create_assigns_id_and_persists(db_session: Session):
    obj_in = CampaignCreate(name="Spring Sale", subject="50% off", audience="all")
    created = crud_campaign.create(db_session, obj_in=obj_in)
    assert created.id is not None
    assert created.name == "Spring Sale"
    assert created.subject == "50% off"
    assert created.audience == "all"
    assert created.status == "draft"  # default
    assert created.body is None
    assert created.scheduled_at is None
    # sent_at is never set by the CRUD layer.
    assert created.sent_at is None


def test_create_scheduled_keeps_scheduled_at(db_session: Session):
    when = datetime(2026, 7, 1, 9, 0, 0)
    obj_in = CampaignCreate(
        name="Launch",
        subject="We are live",
        audience="customers",
        status="scheduled",
        scheduled_at=when,
    )
    created = crud_campaign.create(db_session, obj_in=obj_in)
    assert created.status == "scheduled"
    assert created.scheduled_at == when


def test_create_never_sets_sent_at(db_session: Session):
    """``CampaignCreate`` has no ``sent_at`` field, so it can never be set on create."""
    obj_in = CampaignCreate(name="NoSend", subject="x", audience="b2b")
    created = crud_campaign.create(db_session, obj_in=obj_in)
    assert created.sent_at is None
    assert "sent_at" not in obj_in.model_fields


def test_get_multi_filters_status(db_session: Session, campaign_factory):
    campaign_factory(name="DraftOne", status="draft")
    campaign_factory(name="SentOne", status="sent")
    drafts = crud_campaign.get_multi(db_session, status="draft")
    assert all(c.status == "draft" for c in drafts)
    sent = crud_campaign.get_multi(db_session, status="sent")
    assert all(c.status == "sent" for c in sent)
    assert {"DraftOne"}.issubset({c.name for c in drafts})
    assert {"SentOne"}.issubset({c.name for c in sent})


def test_get_multi_filters_audience(db_session: Session, campaign_factory):
    campaign_factory(name="AllAud", audience="all")
    campaign_factory(name="B2BAud", audience="b2b")
    b2b = crud_campaign.get_multi(db_session, audience="b2b")
    assert all(c.audience == "b2b" for c in b2b)
    assert {"B2BAud"}.issubset({c.name for c in b2b})
    assert "AllAud" not in {c.name for c in b2b}


def test_get_multi_search_by_name_substring(db_session: Session, campaign_factory):
    campaign_factory(name="Black Friday Blast")
    campaign_factory(name="Cyber Monday Mailer")
    campaign_factory(name="New Year Note")
    hits = crud_campaign.get_multi(db_session, search="friday")
    names = {c.name for c in hits}
    assert "Black Friday Blast" in names
    assert "Cyber Monday Mailer" not in names


def test_count_respects_filters(db_session: Session, campaign_factory):
    campaign_factory(name="CNT-A", status="draft", audience="all")
    campaign_factory(name="CNT-B", status="draft", audience="b2b")
    campaign_factory(name="CNT-C", status="sent", audience="all")
    assert crud_campaign.count(db_session) >= 3
    assert crud_campaign.count(db_session, search="CNT-") == 3
    assert crud_campaign.count(db_session, search="CNT-", status="draft") == 2
    assert crud_campaign.count(db_session, search="CNT-", audience="all") == 2


def test_get_multi_pagination_newest_first(db_session: Session, campaign_factory):
    first = campaign_factory(name="PAGE-1")
    second = campaign_factory(name="PAGE-2")
    page = crud_campaign.get_multi(db_session, search="PAGE-", skip=0, limit=1)
    assert len(page) == 1
    # Newest (highest id) first.
    assert page[0].id == max(first.id, second.id)


def test_update_applies_only_sent_fields(db_session: Session, campaign_factory):
    c = campaign_factory(name="UPD1", subject="old subject")
    crud_campaign.update(db_session, campaign=c, obj_in=CampaignUpdate(subject="new subject"))
    db_session.commit()
    db_session.refresh(c)
    assert c.subject == "new subject"
    assert c.name == "UPD1"  # untouched


def test_delete_removes_row(db_session: Session, campaign_factory):
    c = campaign_factory(name="DEL1")
    cid = c.id
    crud_campaign.delete(db_session, campaign=c)
    db_session.commit()
    assert db_session.get(Campaign, cid) is None
