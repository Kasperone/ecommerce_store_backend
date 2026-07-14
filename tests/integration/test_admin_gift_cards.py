"""Integration tests for the admin gift-cards API (/api/v1/admin/gift-cards).

Covers authz (401/403), issue (201 with the plaintext code — the ONLY response
carrying it — hash-at-rest, audit row, issue email iff recipient_email with
EN/PL localization, 404 unknown user_id), list pagination + filters
(status/currency/q by last4 or recipient email), detail (ledger newest-first),
PATCH management fields (+audit), and the signed adjust (ledger row + 400
``gift_card_balance_negative`` machine code + audit event).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.crud.gift_card import hash_code
from app.models.admin_audit_event import AdminAuditEvent
from app.models.gift_card import GiftCard
from app.models.user import UserRole

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin/gift-cards/"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _admin(user_factory, email="gc-admin@example.com"):
    return user_factory(email=email, role=UserRole.ADMIN)


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "gift_card", AdminAuditEvent.action == action)
        .count()
    )


async def _issue(client, admin, **overrides) -> dict:
    body = {"initial_value": 100.0, "currency": "PLN"}
    body.update(overrides)
    resp = await client.post(BASE, json=body, headers=_headers(admin))
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="gc-cust1@example.com")
    assert (await client.get(BASE)).status_code == 401
    assert (await client.get(BASE, headers=_headers(customer))).status_code == 403


async def test_issue_requires_admin(client, user_factory):
    customer = user_factory(email="gc-cust2@example.com")
    body = {"initial_value": 50.0, "currency": "USD"}
    assert (await client.post(BASE, json=body)).status_code == 401
    assert (await client.post(BASE, json=body, headers=_headers(customer))).status_code == 403


async def test_adjust_requires_admin(client, user_factory, db_session):
    customer = user_factory(email="gc-cust3@example.com")
    admin = _admin(user_factory, "gc-admin3@example.com")
    issued = await _issue(client, admin)
    url = f"{BASE}{issued['id']}/adjust"
    assert (await client.post(url, json={"amount": 5})).status_code == 401
    assert (
        await client.post(url, json={"amount": 5}, headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# issue
# ---------------------------------------------------------------------------


async def test_issue_returns_plaintext_code_once_and_stores_hash(client, user_factory, db_session):
    admin = _admin(user_factory)
    before = _audit_count(db_session, "create")
    body = await _issue(client, admin, initial_value=150.5, currency="USD", note="vip")
    # Dash-formatted 16-char code, present ONLY here.
    code = body["code"]
    assert len(code) == 19 and code.count("-") == 3
    assert body["balance"] == 150.5
    assert body["initial_value"] == 150.5
    assert body["currency"] == "USD"
    assert body["status"] == "active"
    assert body["last4"] == code.replace("-", "")[-4:]
    assert "code_hash" not in body

    row = db_session.get(GiftCard, body["id"])
    assert row is not None
    assert row.code_hash == hash_code(code)
    assert code.replace("-", "") not in row.code_hash
    assert _audit_count(db_session, "create") == before + 1

    # The detail/list shapes never leak the code.
    detail = (await client.get(f"{BASE}{body['id']}", headers=_headers(admin))).json()
    assert "code" not in detail and "code_hash" not in detail


async def test_issue_with_recipient_sends_email_with_code(client, user_factory, captured_emails):
    admin = _admin(user_factory)
    body = await _issue(client, admin, recipient_email="lucky@example.com")
    sent = [e for e in captured_emails if e["email_to"] == "lucky@example.com"]
    assert len(sent) == 1
    assert body["code"] in sent[0]["html_content"]
    assert "gift card" in sent[0]["subject"].lower()


async def test_issue_locale_pl_localizes_email(client, user_factory, captured_emails):
    admin = _admin(user_factory)
    await _issue(client, admin, recipient_email="pl@example.com", locale="pl")
    sent = [e for e in captured_emails if e["email_to"] == "pl@example.com"]
    assert len(sent) == 1
    assert "kartę podarunkową" in sent[0]["subject"]
    assert "karty podarunkowej" in sent[0]["html_content"]


async def test_issue_without_recipient_sends_no_email(client, user_factory, captured_emails):
    admin = _admin(user_factory)
    await _issue(client, admin)
    assert captured_emails == []


async def test_issue_unknown_user_id_404(client, user_factory):
    admin = _admin(user_factory)
    resp = await client.post(
        BASE,
        json={"initial_value": 10.0, "currency": "PLN", "user_id": 999999},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"initial_value": 0, "currency": "PLN"},
        {"initial_value": -5, "currency": "PLN"},
        {"initial_value": 10, "currency": "GBP"},
        {"initial_value": 10, "currency": "PLN", "locale": "de"},
        {"initial_value": 10, "currency": "PLN", "recipient_email": "not-an-email"},
    ],
)
async def test_issue_validation_422(client, user_factory, body):
    admin = _admin(user_factory)
    resp = await client.post(BASE, json=body, headers=_headers(admin))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# list + filters
# ---------------------------------------------------------------------------


async def test_list_pagination_and_filters(client, user_factory):
    admin = _admin(user_factory)
    a = await _issue(client, admin, currency="PLN", recipient_email="filter-a@example.com")
    b = await _issue(client, admin, currency="USD")
    # Disable b via PATCH.
    resp = await client.patch(
        f"{BASE}{b['id']}", json={"status": "disabled"}, headers=_headers(admin)
    )
    assert resp.status_code == 200

    listing = (await client.get(BASE, headers=_headers(admin))).json()
    assert listing["total"] >= 2
    assert {"items", "total", "page", "page_size", "pages"} <= set(listing)

    only_disabled = (
        await client.get(BASE, params={"status": "disabled"}, headers=_headers(admin))
    ).json()
    assert {c["id"] for c in only_disabled["items"]} == {b["id"]}

    only_pln = (await client.get(BASE, params={"currency": "PLN"}, headers=_headers(admin))).json()
    ids = {c["id"] for c in only_pln["items"]}
    assert a["id"] in ids and b["id"] not in ids

    by_email = (await client.get(BASE, params={"q": "filter-a@"}, headers=_headers(admin))).json()
    assert {c["id"] for c in by_email["items"]} == {a["id"]}

    by_last4 = (await client.get(BASE, params={"q": a["last4"]}, headers=_headers(admin))).json()
    assert a["id"] in {c["id"] for c in by_last4["items"]}

    paged = (
        await client.get(BASE, params={"page": 1, "page_size": 1}, headers=_headers(admin))
    ).json()
    assert len(paged["items"]) == 1
    assert paged["pages"] >= 2


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


async def test_detail_entries_newest_first(client, user_factory):
    admin = _admin(user_factory)
    issued = await _issue(client, admin, initial_value=100.0)
    await client.post(
        f"{BASE}{issued['id']}/adjust", json={"amount": 25.0}, headers=_headers(admin)
    )
    detail = (await client.get(f"{BASE}{issued['id']}", headers=_headers(admin))).json()
    kinds = [e["kind"] for e in detail["entries"]]
    assert kinds == ["adjust", "issue"]  # newest first
    assert detail["entries"][0]["balance_after"] == 125.0


async def test_detail_404(client, user_factory):
    admin = _admin(user_factory)
    assert (await client.get(f"{BASE}999999", headers=_headers(admin))).status_code == 404


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------


async def test_patch_updates_management_fields_and_audits(client, user_factory, db_session):
    admin = _admin(user_factory)
    issued = await _issue(client, admin)
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        f"{BASE}{issued['id']}",
        json={"status": "disabled", "expires_at": "2027-01-01T00:00:00", "note": "frozen"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "disabled"
    assert body["expires_at"] == "2027-01-01T00:00:00"
    assert body["note"] == "frozen"
    assert _audit_count(db_session, "update") == before + 1


async def test_patch_explicit_null_clears_expiry(client, user_factory):
    admin = _admin(user_factory)
    issued = await _issue(client, admin, expires_at="2027-01-01T00:00:00")
    resp = await client.patch(
        f"{BASE}{issued['id']}", json={"expires_at": None}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["expires_at"] is None


async def test_patch_rejects_unknown_status(client, user_factory):
    admin = _admin(user_factory)
    issued = await _issue(client, admin)
    resp = await client.patch(
        f"{BASE}{issued['id']}", json={"status": "archived"}, headers=_headers(admin)
    )
    assert resp.status_code == 422


async def test_patch_404(client, user_factory):
    admin = _admin(user_factory)
    resp = await client.patch(f"{BASE}999999", json={"note": "x"}, headers=_headers(admin))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# adjust
# ---------------------------------------------------------------------------


async def test_adjust_positive_and_negative(client, user_factory, db_session):
    admin = _admin(user_factory)
    issued = await _issue(client, admin, initial_value=100.0)
    url = f"{BASE}{issued['id']}/adjust"
    before = _audit_count(db_session, "update")

    up_resp = await client.post(
        url, json={"amount": 50.0, "note": "bonus"}, headers=_headers(admin)
    )
    assert up_resp.json()["balance"] == 150.0

    down = (await client.post(url, json={"amount": -30.0}, headers=_headers(admin))).json()
    assert down["balance"] == 120.0
    kinds = [e["kind"] for e in down["entries"]]
    assert kinds == ["adjust", "adjust", "issue"]
    assert _audit_count(db_session, "update") == before + 2


async def test_adjust_below_zero_400_machine_code(client, user_factory):
    admin = _admin(user_factory)
    issued = await _issue(client, admin, initial_value=20.0)
    resp = await client.post(
        f"{BASE}{issued['id']}/adjust", json={"amount": -20.01}, headers=_headers(admin)
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "gift_card_balance_negative"


async def test_adjust_zero_rejected(client, user_factory):
    admin = _admin(user_factory)
    issued = await _issue(client, admin)
    resp = await client.post(
        f"{BASE}{issued['id']}/adjust", json={"amount": 0}, headers=_headers(admin)
    )
    assert resp.status_code == 422


async def test_adjust_404(client, user_factory):
    admin = _admin(user_factory)
    resp = await client.post(f"{BASE}999999/adjust", json={"amount": 5}, headers=_headers(admin))
    assert resp.status_code == 404
