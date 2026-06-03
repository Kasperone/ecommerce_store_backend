"""Integration tests for ``POST /api/v1/auth/register-business`` (Wave D / D-D-5).

The endpoint atomically creates a user (UserRole.CUSTOMER) + a ``pending``
company + an active COMPANY_ADMIN membership, sends a verification email, and is
anti-enumeration (same generic 200 body for a free vs. taken email — a duplicate
sends the "already registered" notice out of band and creates NOTHING).
"""

from __future__ import annotations

import pytest

from app.api.v1.auth import GENERIC_REGISTRATION_MESSAGE
from app.models.company import Company
from app.models.company_user import CompanyMemberStatus, CompanyRole, CompanyUser
from app.models.user import User, UserRole

pytestmark = pytest.mark.integration

REGISTER_BUSINESS_PATH = "/api/v1/auth/register-business"


def _payload(
    email: str = "biz@example.com",
    password: str = "Str0ngPass!",
    company_name: str = "New Biz LLC",
):
    return {
        "email": email,
        "password": password,
        "first_name": "Biz",
        "last_name": "Owner",
        "company_name": company_name,
        "company_tax_id": "PL9999999999",
        "company_address_street": "1 Commerce St",
        "company_address_city": "Warsaw",
        "company_address_postal_code": "00-001",
        "company_address_country": "PL",
        "company_address_state": "Mazowieckie",
    }


async def test_new_business_creates_user_company_membership_atomically(
    client, captured_emails, reset_limiter, db_session
):
    resp = await client.post(REGISTER_BUSINESS_PATH, json=_payload("fresh-biz@example.com"))
    assert resp.status_code == 200
    assert resp.json() == {"message": GENERIC_REGISTRATION_MESSAGE}

    # User: CUSTOMER, unverified, company fields populated.
    user = db_session.query(User).filter(User.email == "fresh-biz@example.com").one()
    assert user.role == UserRole.CUSTOMER
    assert user.is_verified is False
    assert user.company_name == "New Biz LLC"
    # Company address persisted on the user (the invoice PDF reads these columns).
    assert user.company_address_street == "1 Commerce St"
    assert user.company_address_city == "Warsaw"
    assert user.company_address_country == "PL"

    # Company: pending, name + tax_id from the payload, primary contact = the user.
    company = db_session.query(Company).filter(Company.primary_user_id == user.id).one()
    assert company.name == "New Biz LLC"
    assert company.tax_id == "PL9999999999"
    assert company.status == "pending"

    # Membership: active COMPANY_ADMIN linking the user to the company.
    membership = (
        db_session.query(CompanyUser)
        .filter(CompanyUser.company_id == company.id, CompanyUser.user_id == user.id)
        .one()
    )
    assert membership.role == CompanyRole.COMPANY_ADMIN.value
    assert membership.status == CompanyMemberStatus.ACTIVE.value


async def test_new_business_sends_verification_email(client, captured_emails, reset_limiter):
    await client.post(REGISTER_BUSINESS_PATH, json=_payload("verify-biz@example.com"))
    sent = [e for e in captured_emails if e["email_to"] == "verify-biz@example.com"]
    assert [e["kind"] for e in sent] == ["verification"]
    assert sent[0].get("token")


async def test_duplicate_email_is_anti_enumeration_and_creates_nothing(
    client, captured_emails, reset_limiter, user_factory, db_session
):
    user_factory(email="taken-biz@example.com")
    before = db_session.query(Company).count()

    resp = await client.post(REGISTER_BUSINESS_PATH, json=_payload("taken-biz@example.com"))
    assert resp.status_code == 200
    assert resp.json() == {"message": GENERIC_REGISTRATION_MESSAGE}

    # "already registered" out of band, NOT a verification email.
    sent = [e for e in captured_emails if e["email_to"] == "taken-biz@example.com"]
    assert [e["kind"] for e in sent] == ["already_registered"]

    # No company was created for the duplicate.
    assert db_session.query(Company).count() == before


async def test_response_indistinguishable_for_new_vs_duplicate(client, reset_limiter, user_factory):
    user_factory(email="known-biz@example.com")
    new_resp = await client.post(REGISTER_BUSINESS_PATH, json=_payload("brand-new-biz@example.com"))
    dup_resp = await client.post(REGISTER_BUSINESS_PATH, json=_payload("known-biz@example.com"))

    assert new_resp.status_code == dup_resp.status_code == 200
    assert new_resp.json() == dup_resp.json()


@pytest.mark.parametrize(
    "weak_password",
    ["nouppercase1", "NoDigitHere", "Sh0rt!"],
)
async def test_weak_password_rejected_422(client, reset_limiter, weak_password):
    resp = await client.post(
        REGISTER_BUSINESS_PATH,
        json=_payload(email="weak-biz@example.com", password=weak_password),
    )
    assert resp.status_code == 422


async def test_blank_company_name_rejected_422(client, reset_limiter):
    resp = await client.post(
        REGISTER_BUSINESS_PATH, json=_payload(email="noname-biz@example.com", company_name="   ")
    )
    assert resp.status_code == 422


async def test_registered_business_can_login_after_verification(
    client, captured_emails, reset_limiter, db_session
):
    """End-to-end: the created account is a usable B2B login once verified, and
    GET /companies/me resolves its company as COMPANY_ADMIN."""
    await client.post(REGISTER_BUSINESS_PATH, json=_payload("e2e-biz@example.com"))
    user = db_session.query(User).filter(User.email == "e2e-biz@example.com").one()
    # Simulate clicking the verification link.
    user.is_verified = True
    db_session.commit()

    login = await client.post(
        "/api/v1/auth/login",
        data={"username": "e2e-biz@example.com", "password": "Str0ngPass!"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await client.get("/api/v1/companies/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["role"] == "COMPANY_ADMIN"
    assert me.json()["status"] == "pending"


async def test_register_business_rate_limit_blocks_sixth_request(
    client, captured_emails, reset_limiter
):
    """Limit is 5/hour per IP (mirrors /register); the 6th call is 429."""
    for i in range(5):
        r = await client.post(REGISTER_BUSINESS_PATH, json=_payload(f"rl-biz{i}@example.com"))
        assert r.status_code == 200, f"call {i} should pass the limiter"

    sixth = await client.post(REGISTER_BUSINESS_PATH, json=_payload("rl-biz5@example.com"))
    assert sixth.status_code == 429
    assert not any(e["email_to"] == "rl-biz5@example.com" for e in captured_emails)
