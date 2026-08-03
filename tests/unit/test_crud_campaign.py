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
from app.crud import email_suppression as crud_email_suppression
from app.crud import user_consent as crud_user_consent
from app.crud.segment import segment as crud_segment
from app.models.campaign import Campaign
from app.models.segment import Segment
from app.models.user import UserRole
from app.schemas.campaign import CampaignCreate, CampaignUpdate
from app.schemas.segment import SegmentCreate, SegmentFilters
from tests.factories.campaign import CampaignFactory
from tests.factories.user import UserFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def user_factory(db_session: Session):
    UserFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    UserFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return UserFactory(**kwargs)

    return _make


def _grant_marketing(db_session: Session, user, *, consent_type: str = "marketing_email") -> None:
    """Give ``user`` an explicit marketing opt-in.

    Every audience test needs this now: the resolver mails nobody who has not
    opted in, so a user built by the factory alone is (correctly) unreachable.
    """
    crud_user_consent.record(
        db_session, user_id=user.id, consent_type=consent_type, granted=True, source="test"
    )
    db_session.commit()


def _revoke_marketing(db_session: Session, user, *, consent_type: str = "marketing_email") -> None:
    crud_user_consent.record(
        db_session, user_id=user.id, consent_type=consent_type, granted=False, source="test"
    )
    db_session.commit()


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


# ---------------------------------------------------------------------------
# segment audience: create persists segment_id + resolver branch + on-delete
# ---------------------------------------------------------------------------


def _make_segment(db_session: Session, **filter_kwargs) -> Segment:
    seg = crud_segment.create(
        db_session,
        obj_in=SegmentCreate(name="Seg", filters=SegmentFilters(**filter_kwargs)),
    )
    db_session.commit()
    db_session.refresh(seg)
    return seg


def test_create_persists_segment_id(db_session: Session):
    seg = _make_segment(db_session, role="customer")
    obj_in = CampaignCreate(name="SegCamp", subject="hi", audience="segment", segment_id=seg.id)
    created = crud_campaign.create(db_session, obj_in=obj_in)
    assert created.audience == "segment"
    assert created.segment_id == seg.id


def test_resolve_segment_returns_exactly_member_emails(db_session: Session, user_factory):
    """A role=customer segment resolves to the matching customers' emails only."""
    cust = user_factory(email="seg-resolve-cust@example.com", role=UserRole.CUSTOMER)
    admin = user_factory(email="seg-resolve-admin@example.com", role=UserRole.ADMIN)
    _grant_marketing(db_session, cust)
    _grant_marketing(db_session, admin)
    seg = _make_segment(db_session, role="customer")

    emails = crud_campaign.resolve_recipient_emails(
        db_session, audience="segment", segment_id=seg.id
    )
    assert cust.email in emails
    # The admin opted in too, so their absence is the segment filter at work
    # rather than the consent gate silently hiding them.
    assert admin.email not in emails


# ---------------------------------------------------------------------------
# who may be mailed: consent, active state, suppression
# ---------------------------------------------------------------------------


def test_resolve_excludes_user_without_marketing_consent(db_session: Session, user_factory):
    """No consent row at all = never mailed. Silence is not consent."""
    opted_in = user_factory(email="mk-in@example.com", role=UserRole.CUSTOMER)
    never_asked = user_factory(email="mk-silent@example.com", role=UserRole.CUSTOMER)
    _grant_marketing(db_session, opted_in)

    emails = crud_campaign.resolve_recipient_emails(db_session, audience="all")
    assert opted_in.email in emails
    assert never_asked.email not in emails


def test_resolve_excludes_user_who_revoked_consent(db_session: Session, user_factory):
    """The LATEST row wins — a grant followed by a withdrawal means no."""
    user = user_factory(email="mk-revoked@example.com", role=UserRole.CUSTOMER)
    _grant_marketing(db_session, user)
    assert user.email in crud_campaign.resolve_recipient_emails(db_session, audience="all")

    _revoke_marketing(db_session, user)
    assert user.email not in crud_campaign.resolve_recipient_emails(db_session, audience="all")


def test_resolve_accepts_newsletter_consent_as_marketing_opt_in(db_session: Session, user_factory):
    """The public footer signup records ``newsletter``, not ``marketing_email``.

    Both are genuine opt-ins, so gating on only one would silently drop everyone
    who subscribed through the storefront footer.
    """
    user = user_factory(email="mk-newsletter@example.com", role=UserRole.CUSTOMER)
    _grant_marketing(db_session, user, consent_type="newsletter")

    assert user.email in crud_campaign.resolve_recipient_emails(db_session, audience="all")


def test_resolve_excludes_deactivated_and_anonymized_users(db_session: Session, user_factory):
    """GDPR erasure sets is_active=False and rewrites the address to
    ``deleted-user-<id>@anon.invalid`` — non-empty, so only the is_active check
    keeps us from hard-bouncing every erased account on every campaign."""
    erased = user_factory(email="deleted-user-999@anon.invalid", role=UserRole.CUSTOMER)
    _grant_marketing(db_session, erased)
    erased.is_active = False
    db_session.add(erased)
    db_session.commit()

    assert erased.email not in crud_campaign.resolve_recipient_emails(db_session, audience="all")


def test_resolve_excludes_suppressed_address_despite_consent(db_session: Session, user_factory):
    """Suppression beats consent: an unsubscribe must not be undone by a stale
    grant row that is still the newest one for its type."""
    user = user_factory(email="mk-suppressed@example.com", role=UserRole.CUSTOMER)
    _grant_marketing(db_session, user)
    crud_email_suppression.suppress(db_session, email=user.email)
    db_session.commit()

    assert user.email not in crud_campaign.resolve_recipient_emails(db_session, audience="all")


def test_resolve_suppression_is_case_insensitive(db_session: Session, user_factory):
    """Addresses are one subject regardless of casing — otherwise unsubscribing
    as ``Foo@x.com`` would leave ``foo@x.com`` still subscribed."""
    user = user_factory(email="MixedCase@example.com", role=UserRole.CUSTOMER)
    _grant_marketing(db_session, user)
    crud_email_suppression.suppress(db_session, email="mixedcase@EXAMPLE.com")
    db_session.commit()

    assert user.email not in crud_campaign.resolve_recipient_emails(db_session, audience="all")


def test_resolve_segment_missing_segment_id_is_empty(db_session: Session, user_factory):
    user_factory(email="seg-none@example.com", role=UserRole.CUSTOMER)
    assert crud_campaign.resolve_recipient_emails(db_session, audience="segment") == []


def test_resolve_segment_deleted_segment_is_empty(db_session: Session, user_factory):
    """After the segment is deleted, resolving by its (stale) id yields no one."""
    user_factory(email="seg-deleted@example.com", role=UserRole.CUSTOMER)
    seg = _make_segment(db_session, role="customer")
    seg_id = seg.id
    crud_segment.delete(db_session, segment=seg)
    db_session.commit()
    assert (
        crud_campaign.resolve_recipient_emails(db_session, audience="segment", segment_id=seg_id)
        == []
    )


def test_delete_segment_sets_campaign_segment_id_null(db_session: Session):
    """ON DELETE SET NULL: deleting the segment nulls the campaign's segment_id."""
    seg = _make_segment(db_session, role="customer")
    camp = crud_campaign.create(
        db_session,
        obj_in=CampaignCreate(name="OrphanMe", subject="x", audience="segment", segment_id=seg.id),
    )
    db_session.commit()
    camp_id = camp.id

    crud_segment.delete(db_session, segment=seg)
    db_session.commit()

    db_session.expire_all()
    orphaned = db_session.get(Campaign, camp_id)
    assert orphaned is not None
    assert orphaned.segment_id is None
    # The CHECK still holds: audience stays 'segment' but the row no longer
    # resolves a segment audience (resolver returns []).
    assert (
        crud_campaign.resolve_recipient_emails(
            db_session, audience=orphaned.audience, segment_id=orphaned.segment_id
        )
        == []
    )
