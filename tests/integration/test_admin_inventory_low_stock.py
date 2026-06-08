"""Integration tests for the low-stock alert endpoints + the settings field.

Covers:
- ``GET /admin/inventory/low-stock-summary`` — auth gating + grouped counts that
  honour the store-wide ``low_stock_default_threshold``.
- ``POST /admin/inventory/low-stock-report/email`` — auth gating, the resolved
  recipient + product count, the audit row (``action="low_stock_report"``), and
  the email send (mocked via ``captured_emails``, dispatched on a BackgroundTask).
- ``low_stock_default_threshold`` round-trips through ``PATCH /admin/settings``
  (set, re-GET reflects it, clear back to NULL, range validation).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.crud import store_config as crud_store_config
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import UserRole
from app.services import inventory_email

pytestmark = pytest.mark.integration

SUMMARY_URL = "/api/v1/admin/inventory/low-stock-summary"
REPORT_URL = "/api/v1/admin/inventory/low-stock-report/email"
SETTINGS_URL = "/api/v1/admin/settings"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _set_global_default(db: Session, value: int | None) -> None:
    config = crud_store_config.get_singleton(db)
    config.low_stock_default_threshold = value
    db.add(config)
    db.commit()


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_summary_requires_admin(client, user_factory):
    customer = user_factory(email="ls-cust1@example.com", role=UserRole.CUSTOMER)
    assert (await client.get(SUMMARY_URL)).status_code == 401
    assert (await client.get(SUMMARY_URL, headers=_headers(customer))).status_code == 403


async def test_report_requires_admin(client, user_factory):
    customer = user_factory(email="ls-cust2@example.com", role=UserRole.CUSTOMER)
    assert (await client.post(REPORT_URL)).status_code == 401
    assert (await client.post(REPORT_URL, headers=_headers(customer))).status_code == 403


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


async def test_summary_counts_without_global_default(client, user_factory, product_factory):
    admin = user_factory(email="ls-admin1@example.com", role=UserRole.ADMIN)
    product_factory(stock=2, low_stock_threshold=5)  # own threshold → low
    product_factory(stock=50, low_stock_threshold=5)  # in_stock
    product_factory(stock=0, allow_backorder=False)  # out_of_stock
    product_factory(stock=0, allow_backorder=True)  # backorder

    resp = await client.get(SUMMARY_URL, headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json() == {
        "low_stock_count": 1,
        "out_of_stock_count": 1,
        "backorder_count": 1,
    }


async def test_summary_honours_global_default(client, user_factory, product_factory, db_session):
    admin = user_factory(email="ls-admin2@example.com", role=UserRole.ADMIN)
    product_factory(stock=4, low_stock_threshold=None)  # inherits global → low
    product_factory(stock=50, low_stock_threshold=None)  # in_stock

    # Without a global default, nothing is low.
    resp = await client.get(SUMMARY_URL, headers=_headers(admin))
    assert resp.json()["low_stock_count"] == 0

    _set_global_default(db_session, 5)
    resp = await client.get(SUMMARY_URL, headers=_headers(admin))
    assert resp.json()["low_stock_count"] == 1


# ---------------------------------------------------------------------------
# manual report email
# ---------------------------------------------------------------------------


async def test_report_resolves_set_recipients_and_audits(
    client, user_factory, product_factory, db_session, monkeypatch
):
    admin = user_factory(email="ls-admin3@example.com", role=UserRole.ADMIN)
    low1 = product_factory(stock=2, low_stock_threshold=5)
    low2 = product_factory(stock=1, low_stock_threshold=3)
    product_factory(stock=50, low_stock_threshold=5)  # not low

    # Capture the background-task dispatch (a fresh-session send can't see the
    # test's savepoint data, so we assert the route resolved + queued correctly;
    # the composer itself is covered by a unit test).
    dispatched: dict = {}

    def _fake_bg(recipients, product_ids):
        dispatched["recipients"] = recipients
        dispatched["product_ids"] = product_ids

    monkeypatch.setattr(inventory_email, "send_low_stock_report_in_background", _fake_bg)

    resp = await client.post(REPORT_URL, headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["product_count"] == 2
    assert isinstance(body["recipients"], list) and len(body["recipients"]) == 1

    # Audit row written.
    audit = (
        db_session.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "inventory",
            AdminAuditEvent.action == "low_stock_report",
        )
        .one()
    )
    assert audit.payload["low_stock_report"]["product_count"] == 2

    # The background task was queued with the resolved recipient + the two
    # low-stock product ids.
    assert dispatched["recipients"] == body["recipients"]
    assert set(dispatched["product_ids"]) == {low1.id, low2.id}


async def test_report_with_no_low_stock_still_audits(
    client, user_factory, product_factory, db_session, monkeypatch
):
    admin = user_factory(email="ls-admin4@example.com", role=UserRole.ADMIN)
    product_factory(stock=50, low_stock_threshold=5)  # nothing low

    monkeypatch.setattr(
        inventory_email, "send_low_stock_report_in_background", lambda *a, **k: None
    )
    resp = await client.post(REPORT_URL, headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["product_count"] == 0
    # Still audited even with nothing low.
    assert (
        db_session.query(AdminAuditEvent)
        .filter(AdminAuditEvent.action == "low_stock_report")
        .count()
        == 1
    )


def test_resolve_report_recipients_prefers_store_config_email(db_session):
    """The recipient resolver prefers store_config.email over env fallbacks."""
    config = crud_store_config.get_singleton(db_session)
    config.email = "stock@store.example"
    db_session.add(config)
    db_session.flush()
    assert inventory_email.resolve_report_recipients(db_session) == ["stock@store.example"]


def test_send_low_stock_report_composer_emails_each_recipient(
    db_session, product_factory, captured_emails
):
    """The pure composer sends one HTML email per recipient listing the products
    (free-text product names escaped). Covered here since the background entry
    runs in a fresh session that can't see the test savepoint."""
    low1 = product_factory(name="Widget A", stock=2, low_stock_threshold=5)
    low2 = product_factory(name="Widget B", stock=1, low_stock_threshold=3)

    inventory_email.send_low_stock_report(db_session, ["ops@store.example"], [low1, low2])

    sent = [e for e in captured_emails if e.get("kind") == "generic"]
    assert len(sent) == 1
    assert sent[0]["email_to"] == "ops@store.example"
    html = sent[0]["html_content"]
    assert "Widget A" in html and "Widget B" in html


def test_send_low_stock_report_empty_set_sends_nothing_to_action(db_session, captured_emails):
    inventory_email.send_low_stock_report(db_session, ["ops@store.example"], [])
    sent = [e for e in captured_emails if e.get("kind") == "generic"]
    assert len(sent) == 1
    assert "Nothing to action" in sent[0]["html_content"]


# ---------------------------------------------------------------------------
# settings field round-trip
# ---------------------------------------------------------------------------


async def test_settings_low_stock_default_round_trips(client, user_factory):
    admin = user_factory(email="ls-admin5@example.com", role=UserRole.ADMIN)
    headers = _headers(admin)

    # Seeded default is NULL.
    resp = await client.get(SETTINGS_URL, headers=headers)
    assert resp.json()["low_stock_default_threshold"] is None

    # Set it.
    resp = await client.patch(
        SETTINGS_URL, json={"low_stock_default_threshold": 7}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["low_stock_default_threshold"] == 7
    assert (await client.get(SETTINGS_URL, headers=headers)).json()[
        "low_stock_default_threshold"
    ] == 7

    # Clear it back to NULL via an explicit null.
    resp = await client.patch(
        SETTINGS_URL, json={"low_stock_default_threshold": None}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["low_stock_default_threshold"] is None


async def test_settings_low_stock_default_rejects_negative(client, user_factory):
    admin = user_factory(email="ls-admin6@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        SETTINGS_URL, json={"low_stock_default_threshold": -1}, headers=_headers(admin)
    )
    assert resp.status_code == 422
