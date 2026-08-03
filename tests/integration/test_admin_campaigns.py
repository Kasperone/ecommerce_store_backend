"""Integration tests for the admin campaigns API (/api/v1/admin/campaigns).

Covers authz (401/403), list pagination + filters (status, audience, search),
create (happy path + audit + default status + scheduled-requires-scheduled_at
422), get (200/404), update (happy path, merged-state schedule re-validation,
audit, 404), delete (204 + audit, 404), and send (404/401/403, per-audience
recipient resolution, status→sent + sent_at + audit, best-effort one-bad-recipient,
409 re-send). Every mutation writes an audit row (resource_type ``campaign``).
``sent_at`` is read-only on create/update — only the send route stamps it.

OUTWARD-FACING: the send route dispatches real email; the autouse
``captured_emails`` fixture mocks ``app.core.email.send_email`` so no live Resend
calls happen in pytest.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.crud import email_suppression as crud_email_suppression
from app.crud import user_consent as crud_user_consent
from app.crud.segment import segment as crud_segment
from app.models.admin_audit_event import AdminAuditEvent
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.segment import Segment
from app.models.user import UserRole
from app.schemas.segment import SegmentCreate, SegmentFilters
from tests.factories.campaign import CampaignFactory
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "campaign", AdminAuditEvent.action == action)
        .count()
    )


@pytest.fixture
def campaign_factory(db_session: Session):
    """Callable creating a Campaign row via the factory (session bound per-test)."""
    CampaignFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    CampaignFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Campaign:
        return CampaignFactory(**kwargs)

    return _make


@pytest.fixture
def company_factory(db_session: Session):
    """Callable creating a Company row via the factory (session bound per-test)."""
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Company:
        return CompanyFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="cmp-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/campaigns/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/campaigns/", headers=_headers(customer))
    ).status_code == 403


async def test_create_requires_admin(client, user_factory):
    customer = user_factory(email="cmp-cust2@example.com", role=UserRole.CUSTOMER)
    body = {"name": "X1", "subject": "Hi", "audience": "all"}
    assert (await client.post("/api/v1/admin/campaigns/", json=body)).status_code == 401
    assert (
        await client.post("/api/v1/admin/campaigns/", json=body, headers=_headers(customer))
    ).status_code == 403


async def test_delete_requires_admin(client, user_factory, campaign_factory):
    customer = user_factory(email="cmp-cust3@example.com", role=UserRole.CUSTOMER)
    c = campaign_factory(name="DELAUTH")
    assert (await client.delete(f"/api/v1/admin/campaigns/{c.id}")).status_code == 401
    assert (
        await client.delete(f"/api/v1/admin/campaigns/{c.id}", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_writes_audit_and_defaults_draft(client, user_factory, db_session):
    admin = user_factory(email="cmp-admin1@example.com", role=UserRole.ADMIN)
    before = _audit_count(db_session, "create")
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "Spring Sale", "subject": "50% off", "audience": "all"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Spring Sale"
    assert body["subject"] == "50% off"
    assert body["audience"] == "all"
    assert body["status"] == "draft"  # default
    assert body["body"] is None
    assert body["scheduled_at"] is None
    assert body["sent_at"] is None
    assert _audit_count(db_session, "create") == before + 1


async def test_create_scheduled_requires_scheduled_at(client, user_factory):
    admin = user_factory(email="cmp-admin2@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "Launch", "subject": "Live", "audience": "all", "status": "scheduled"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_scheduled_with_scheduled_at_ok(client, user_factory):
    admin = user_factory(email="cmp-admin3@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={
            "name": "Launch",
            "subject": "Live",
            "audience": "customers",
            "status": "scheduled",
            "scheduled_at": "2026-07-01T09:00:00",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "scheduled"
    assert body["scheduled_at"] == "2026-07-01T09:00:00"


async def test_create_rejects_unknown_audience(client, user_factory):
    admin = user_factory(email="cmp-admin4@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "Bad", "subject": "x", "audience": "vips"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_rejects_unknown_status(client, user_factory):
    admin = user_factory(email="cmp-admin5@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "Bad", "subject": "x", "audience": "all", "status": "archived"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_ignores_sent_at_from_client(client, user_factory):
    """``sent_at`` is read-only — supplying it in the body is silently ignored."""
    admin = user_factory(email="cmp-admin6@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={
            "name": "TrySetSent",
            "subject": "x",
            "audience": "all",
            "sent_at": "2026-06-01T00:00:00",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    assert resp.json()["sent_at"] is None


# ---------------------------------------------------------------------------
# list / filters / pagination
# ---------------------------------------------------------------------------


async def test_list_filters_and_paginates(client, user_factory, campaign_factory):
    admin = user_factory(email="cmp-admin7@example.com", role=UserRole.ADMIN)
    campaign_factory(name="SALE-A", status="draft", audience="all")
    campaign_factory(name="SALE-B", status="draft", audience="b2b")
    campaign_factory(name="SALE-C", status="sent", audience="all")
    campaign_factory(name="OTHER-X", status="draft", audience="all")

    # search filter (name contains)
    resp = await client.get(
        "/api/v1/admin/campaigns/", params={"search": "SALE-"}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert {c["name"] for c in body["items"]} == {"SALE-A", "SALE-B", "SALE-C"}

    # combine search + status
    resp = await client.get(
        "/api/v1/admin/campaigns/",
        params={"search": "SALE-", "status": "draft"},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 2
    assert all(c["status"] == "draft" for c in body["items"])

    # combine search + audience
    resp = await client.get(
        "/api/v1/admin/campaigns/",
        params={"search": "SALE-", "audience": "b2b"},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "SALE-B"

    # pagination: page_size=1 over the 3 SALE- campaigns → 3 pages
    resp = await client.get(
        "/api/v1/admin/campaigns/",
        params={"search": "SALE-", "page": 1, "page_size": 1},
        headers=_headers(admin),
    )
    body = resp.json()
    assert body["total"] == 3
    assert body["pages"] == 3
    assert len(body["items"]) == 1
    assert body["page"] == 1
    assert body["page_size"] == 1


# ---------------------------------------------------------------------------
# get / update
# ---------------------------------------------------------------------------


async def test_get_404(client, user_factory):
    admin = user_factory(email="cmp-admin8@example.com", role=UserRole.ADMIN)
    assert (
        await client.get("/api/v1/admin/campaigns/999999", headers=_headers(admin))
    ).status_code == 404


async def test_get_returns_campaign(client, user_factory, campaign_factory):
    admin = user_factory(email="cmp-admin9@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="GETME")
    resp = await client.get(f"/api/v1/admin/campaigns/{c.id}", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["name"] == "GETME"


async def test_update_writes_audit(client, user_factory, campaign_factory, db_session):
    admin = user_factory(email="cmp-admin10@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="UPDME", subject="old")
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        f"/api/v1/admin/campaigns/{c.id}",
        json={"subject": "new", "body": "Hello there"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == "new"
    assert body["body"] == "Hello there"
    assert _audit_count(db_session, "update") == before + 1


async def test_update_404(client, user_factory):
    admin = user_factory(email="cmp-admin11@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        "/api/v1/admin/campaigns/999999",
        json={"subject": "x"},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_update_to_scheduled_without_scheduled_at_422(client, user_factory, campaign_factory):
    """Merged-state validation: flipping a draft campaign to scheduled without
    supplying a scheduled_at must 422 (the schedule rule re-checks the merge)."""
    admin = user_factory(email="cmp-admin12@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="FLIP", status="draft", scheduled_at=None)
    resp = await client.patch(
        f"/api/v1/admin/campaigns/{c.id}",
        json={"status": "scheduled"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_update_to_scheduled_with_scheduled_at_ok(client, user_factory, campaign_factory):
    admin = user_factory(email="cmp-admin13@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="FLIP2", status="draft", scheduled_at=None)
    resp = await client.patch(
        f"/api/v1/admin/campaigns/{c.id}",
        json={"status": "scheduled", "scheduled_at": "2026-07-01T09:00:00"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "scheduled"
    assert body["scheduled_at"] == "2026-07-01T09:00:00"


async def test_update_clear_scheduled_at_on_scheduled_422(client, user_factory, campaign_factory):
    """Clearing scheduled_at on an already-scheduled campaign 422s (merged state)."""
    from datetime import datetime

    admin = user_factory(email="cmp-admin14@example.com", role=UserRole.ADMIN)
    c = campaign_factory(
        name="SCHED", status="scheduled", scheduled_at=datetime(2026, 7, 1, 9, 0, 0)
    )
    resp = await client.patch(
        f"/api/v1/admin/campaigns/{c.id}",
        json={"scheduled_at": None},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_writes_audit(client, user_factory, campaign_factory, db_session):
    admin = user_factory(email="cmp-admin15@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="DELME")
    before = _audit_count(db_session, "delete")
    resp = await client.delete(f"/api/v1/admin/campaigns/{c.id}", headers=_headers(admin))
    assert resp.status_code == 204
    assert _audit_count(db_session, "delete") == before + 1
    assert db_session.get(Campaign, c.id) is None


async def test_delete_404(client, user_factory):
    admin = user_factory(email="cmp-admin16@example.com", role=UserRole.ADMIN)
    assert (
        await client.delete("/api/v1/admin/campaigns/999999", headers=_headers(admin))
    ).status_code == 404


# ---------------------------------------------------------------------------
# send (outward-facing — send_email is mocked by the autouse captured_emails fixture)
# ---------------------------------------------------------------------------


def _sent_to(captured_emails) -> set[str]:
    """Recipient set of the generic send_email calls recorded by the email mock."""
    return {e["email_to"] for e in captured_emails if e["kind"] == "generic"}


def _opt_in(db: Session, *users, consent_type: str = "marketing_email") -> None:
    """Record an explicit marketing opt-in for each user.

    Required by every send test now that the resolver mails only consenting
    recipients — a factory-built user has no consent row and is unreachable.
    """
    for user in users:
        crud_user_consent.record(
            db, user_id=user.id, consent_type=consent_type, granted=True, source="test"
        )
    db.commit()


async def test_send_404(client, user_factory):
    admin = user_factory(email="cmp-send404@example.com", role=UserRole.ADMIN)
    resp = await client.post("/api/v1/admin/campaigns/999999/send", headers=_headers(admin))
    assert resp.status_code == 404


async def test_send_requires_admin(client, user_factory, campaign_factory):
    customer = user_factory(email="cmp-send-cust@example.com", role=UserRole.CUSTOMER)
    c = campaign_factory(name="SENDAUTH", audience="all")
    assert (await client.post(f"/api/v1/admin/campaigns/{c.id}/send")).status_code == 401
    assert (
        await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(customer))
    ).status_code == 403


async def test_send_audience_all_mails_every_user_and_flips_status(
    client, user_factory, campaign_factory, captured_emails, db_session
):
    admin = user_factory(email="cmp-send-all-admin@example.com", role=UserRole.ADMIN)
    cust = user_factory(email="cmp-send-all-cust@example.com", role=UserRole.CUSTOMER)
    _opt_in(db_session, admin, cust)
    c = campaign_factory(name="ALLSEND", subject="Hi all", body="<p>Body</p>", audience="all")
    before_audit = _audit_count(db_session, "update")

    resp = await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    assert body["sent_at"] is not None

    # Every user with an email got mailed (the admin + the customer at minimum).
    recipients = _sent_to(captured_emails)
    assert admin.email in recipients
    assert cust.email in recipients
    # The mailed payload carried the campaign subject.
    assert all(e["subject"] == "Hi all" for e in captured_emails if e["kind"] == "generic")
    # Audit row written for the status flip.
    assert _audit_count(db_session, "update") == before_audit + 1


async def test_send_audience_customers_excludes_admins(
    client, user_factory, campaign_factory, captured_emails, db_session
):
    admin = user_factory(email="cmp-send-cust-admin@example.com", role=UserRole.ADMIN)
    cust = user_factory(email="cmp-send-cust-cust@example.com", role=UserRole.CUSTOMER)
    # Both opt in, so the admin's absence below proves the role filter rather
    # than the consent gate.
    _opt_in(db_session, admin, cust)
    c = campaign_factory(name="CUSTSEND", subject="For customers", audience="customers")

    resp = await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(admin))
    assert resp.status_code == 200

    recipients = _sent_to(captured_emails)
    assert cust.email in recipients
    assert admin.email not in recipients


async def test_send_audience_b2b_mails_distinct_company_emails(
    client, user_factory, campaign_factory, company_factory, captured_emails
):
    admin = user_factory(email="cmp-send-b2b-admin@example.com", role=UserRole.ADMIN)
    company_factory(name="Acme", email="acme@biz.example")
    company_factory(name="Globex", email="globex@biz.example")
    company_factory(name="NoEmail", email=None)  # filtered out
    c = campaign_factory(name="B2BSEND", subject="For B2B", audience="b2b")

    resp = await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(admin))
    assert resp.status_code == 200

    recipients = _sent_to(captured_emails)
    # Only company emails (no users), null-email company excluded.
    assert recipients == {"acme@biz.example", "globex@biz.example"}
    assert admin.email not in recipients


async def test_send_already_sent_409(client, user_factory, campaign_factory, captured_emails):
    admin = user_factory(email="cmp-send-409-admin@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="ALREADYSENT", audience="all", status="sent")

    resp = await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(admin))
    assert resp.status_code == 409
    # No email dispatched for an already-sent campaign.
    assert _sent_to(captured_emails) == set()


async def test_send_failing_recipient_does_not_abort_run(
    client, user_factory, campaign_factory, monkeypatch, db_session
):
    """A bad recipient is counted as failed but the run continues and the campaign
    still flips to ``sent`` (best-effort posture)."""
    import app.core.email as email_mod

    admin = user_factory(email="cmp-send-fail-admin@example.com", role=UserRole.ADMIN)
    cust = user_factory(email="cmp-send-fail-cust@example.com", role=UserRole.CUSTOMER)
    _opt_in(db_session, admin, cust)

    seen: list[str] = []

    def _flaky(*, email_to: str, **kwargs):
        seen.append(email_to)
        if email_to == admin.email:
            raise RuntimeError("simulated transport failure")
        return {"id": "ok"}

    monkeypatch.setattr(email_mod, "send_email", _flaky)

    c = campaign_factory(name="FLAKYSEND", subject="hi", audience="all")
    resp = await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(admin))

    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"  # one failure did not abort
    # Both recipients were attempted (the good one still got mailed after the bad one).
    assert admin.email in seen
    assert cust.email in seen


# ---------------------------------------------------------------------------
# who a campaign may lawfully reach, and what every send must carry
# ---------------------------------------------------------------------------


async def test_send_skips_users_without_marketing_consent(
    client, user_factory, campaign_factory, captured_emails, db_session
):
    """The consent gate at the route level, not just the resolver.

    The opted-in user proves the send actually ran — without that positive
    control an empty recipient set would pass this test even if sending were
    broken outright.
    """
    admin = user_factory(email="cmp-consent-admin@example.com", role=UserRole.ADMIN)
    opted_in = user_factory(email="cmp-consent-yes@example.com", role=UserRole.CUSTOMER)
    never_asked = user_factory(email="cmp-consent-no@example.com", role=UserRole.CUSTOMER)
    _opt_in(db_session, opted_in)

    c = campaign_factory(name="CONSENTSEND", subject="hi", audience="all")
    resp = await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(admin))
    assert resp.status_code == 200

    recipients = _sent_to(captured_emails)
    assert opted_in.email in recipients
    assert never_asked.email not in recipients
    assert admin.email not in recipients  # admin never opted in either


async def test_send_skips_suppressed_address(
    client, user_factory, campaign_factory, captured_emails, db_session
):
    """An unsubscribed address stays unsubscribed even with consent on file."""
    admin = user_factory(email="cmp-supp-admin@example.com", role=UserRole.ADMIN)
    still_in = user_factory(email="cmp-supp-in@example.com", role=UserRole.CUSTOMER)
    opted_out = user_factory(email="cmp-supp-out@example.com", role=UserRole.CUSTOMER)
    _opt_in(db_session, still_in, opted_out)
    crud_email_suppression.suppress(db_session, email=opted_out.email)
    db_session.commit()

    c = campaign_factory(name="SUPPSEND", subject="hi", audience="all")
    resp = await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(admin))
    assert resp.status_code == 200

    recipients = _sent_to(captured_emails)
    assert still_in.email in recipients
    assert opted_out.email not in recipients


async def test_send_attaches_unsubscribe_footer_and_headers(
    client, user_factory, campaign_factory, captured_emails, db_session
):
    """Every marketing send carries a visible opt-out AND the RFC 8058 headers.

    Both are required: the header drives the mail client's native button, the
    footer link is the only route for clients that render neither.
    """
    admin = user_factory(email="cmp-unsub-admin@example.com", role=UserRole.ADMIN)
    cust = user_factory(email="cmp-unsub-cust@example.com", role=UserRole.CUSTOMER)
    _opt_in(db_session, cust)

    c = campaign_factory(
        name="UNSUBSEND", subject="hi", body="<p>Original body</p>", audience="all"
    )
    resp = await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(admin))
    assert resp.status_code == 200

    sent = [e for e in captured_emails if e["kind"] == "generic" and e["email_to"] == cust.email]
    assert len(sent) == 1
    payload = sent[0]

    # The author's body survives intact; the footer is appended, not wrapped.
    assert "<p>Original body</p>" in payload["html_content"]
    assert "/api/v1/newsletter/unsubscribe?token=" in payload["html_content"]

    headers = payload["headers"]
    assert headers["List-Unsubscribe"].startswith("<")
    assert "/api/v1/newsletter/unsubscribe?token=" in headers["List-Unsubscribe"]
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


async def test_send_unsubscribe_token_is_per_recipient(
    client, user_factory, campaign_factory, captured_emails, db_session
):
    """Two recipients must not share a token, or one person's unsubscribe would
    silently opt the other out as well."""
    admin = user_factory(email="cmp-tok-admin@example.com", role=UserRole.ADMIN)
    a = user_factory(email="cmp-tok-a@example.com", role=UserRole.CUSTOMER)
    b = user_factory(email="cmp-tok-b@example.com", role=UserRole.CUSTOMER)
    _opt_in(db_session, a, b)

    c = campaign_factory(name="TOKSEND", subject="hi", audience="all")
    resp = await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(admin))
    assert resp.status_code == 200

    by_recipient = {
        e["email_to"]: e["headers"]["List-Unsubscribe"]
        for e in captured_emails
        if e["kind"] == "generic"
    }
    assert by_recipient[a.email] != by_recipient[b.email]


# ---------------------------------------------------------------------------
# segment audience
# ---------------------------------------------------------------------------


def _make_segment(db_session: Session, **filter_kwargs) -> Segment:
    seg = crud_segment.create(
        db_session,
        obj_in=SegmentCreate(name="RouteSeg", filters=SegmentFilters(**filter_kwargs)),
    )
    db_session.commit()
    db_session.refresh(seg)
    return seg


async def test_create_segment_audience_requires_segment_id(client, user_factory):
    admin = user_factory(email="cmp-seg-noid@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "SegNoId", "subject": "x", "audience": "segment"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_segment_id_rejected_for_non_segment_audience(
    client, user_factory, db_session
):
    admin = user_factory(email="cmp-seg-wrongaud@example.com", role=UserRole.ADMIN)
    seg = _make_segment(db_session, role="customer")
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "WrongAud", "subject": "x", "audience": "all", "segment_id": seg.id},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_create_segment_audience_unknown_segment_404(client, user_factory):
    admin = user_factory(email="cmp-seg-404@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "GhostSeg", "subject": "x", "audience": "segment", "segment_id": 999999},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_create_segment_audience_ok(client, user_factory, db_session):
    admin = user_factory(email="cmp-seg-ok@example.com", role=UserRole.ADMIN)
    seg = _make_segment(db_session, role="customer")
    resp = await client.post(
        "/api/v1/admin/campaigns/",
        json={"name": "SegOK", "subject": "x", "audience": "segment", "segment_id": seg.id},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["audience"] == "segment"
    assert body["segment_id"] == seg.id


async def test_patch_to_segment_without_segment_id_422(client, user_factory, campaign_factory):
    """Merged-state re-check: flipping audience to segment without a segment_id 422s."""
    admin = user_factory(email="cmp-seg-patch422@example.com", role=UserRole.ADMIN)
    c = campaign_factory(name="FLIPSEG", audience="all")
    resp = await client.patch(
        f"/api/v1/admin/campaigns/{c.id}",
        json={"audience": "segment"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_patch_to_segment_with_segment_id_ok(
    client, user_factory, campaign_factory, db_session
):
    admin = user_factory(email="cmp-seg-patchok@example.com", role=UserRole.ADMIN)
    seg = _make_segment(db_session, role="customer")
    c = campaign_factory(name="FLIPSEG2", audience="all")
    resp = await client.patch(
        f"/api/v1/admin/campaigns/{c.id}",
        json={"audience": "segment", "segment_id": seg.id},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["audience"] == "segment"
    assert body["segment_id"] == seg.id


async def test_send_segment_audience_mails_exactly_members(
    client, user_factory, campaign_factory, captured_emails, db_session
):
    """A segment campaign mails exactly the segment's resolved members."""
    admin = user_factory(email="cmp-seg-send-admin@example.com", role=UserRole.ADMIN)
    member = user_factory(email="cmp-seg-send-member@example.com", role=UserRole.CUSTOMER)
    _opt_in(db_session, admin, member)
    seg = _make_segment(db_session, role="customer")
    c = campaign_factory(
        name="SEGSEND", subject="for segment", audience="segment", segment_id=seg.id
    )

    resp = await client.post(f"/api/v1/admin/campaigns/{c.id}/send", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"

    recipients = _sent_to(captured_emails)
    # role=customer segment → the customer is mailed, the admin is not.
    assert member.email in recipients
    assert admin.email not in recipients


async def test_delete_segment_orphans_campaign_segment_id(
    client, user_factory, campaign_factory, db_session
):
    """Deleting the referenced segment SET-NULLs the campaign's segment_id; the
    campaign then resolves to no recipients."""
    user_factory(email="cmp-seg-orphan-cust@example.com", role=UserRole.CUSTOMER)
    seg = _make_segment(db_session, role="customer")
    c = campaign_factory(name="ORPHAN", subject="x", audience="segment", segment_id=seg.id)
    cid = c.id

    crud_segment.delete(db_session, segment=seg)
    db_session.commit()
    db_session.expire_all()

    orphaned = db_session.get(Campaign, cid)
    assert orphaned is not None
    assert orphaned.segment_id is None
    assert orphaned.audience == "segment"
    from app.crud import campaign as crud_campaign

    assert (
        crud_campaign.resolve_recipient_emails(
            db_session, audience=orphaned.audience, segment_id=orphaned.segment_id
        )
        == []
    )
