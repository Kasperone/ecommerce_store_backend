"""Integration tests for the B2B auto-pay card-vaulting routes (SEC-020).

``POST /companies/me/auto-charge/confirm`` reads back a SetupIntent the browser
confirmed and stores its PaymentMethod on the company. The customer check is
**strict**: the intent's customer must equal the company's ``stripe_customer_id``.
A company with no customer id (i.e. one that never went through
``/auto-charge/setup-intent``) is refused rather than adopted, so a client can't
hand us a PaymentMethod vaulted under someone else's Stripe Customer.

Stripe is faked at the SDK boundary (``stripe.SetupIntent.retrieve/create``) with
the settings monkeypatched non-empty, matching ``test_payments_stripe.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import stripe
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration

CONFIRM_PATH = "/api/v1/companies/me/auto-charge/confirm"
SETUP_INTENT_PATH = "/api/v1/companies/me/auto-charge/setup-intent"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


@pytest.fixture
def configured_stripe(monkeypatch):
    """Non-empty Stripe settings so ``get_stripe_service`` doesn't 503."""
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test_x")


@pytest.fixture
def fake_setup_intent(monkeypatch):
    """Stub ``SetupIntent.retrieve``; returns a setter for the intent it yields."""

    state: dict = {"status": "succeeded", "payment_method": "pm_new", "customer": "cus_company"}

    def _retrieve(_setup_intent_id, **_kwargs):
        return SimpleNamespace(**state)

    monkeypatch.setattr(stripe.SetupIntent, "retrieve", _retrieve)
    return state


async def test_confirm_stores_payment_method_on_matching_customer(
    client, db_session, user_factory, company_factory, configured_stripe, fake_setup_intent
):
    """Happy path: intent customer == company customer → the card is stored."""
    admin = user_factory(email="ac-confirm-ok@example.com")
    company = company_factory(
        name="ConfirmOkCo",
        primary_user_id=admin.id,
        net_terms_enabled=True,
        stripe_customer_id="cus_company",
    )

    resp = await client.post(
        CONFIRM_PATH, json={"setup_intent_id": "seti_1"}, headers=_headers(admin)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_charge_card_set"] is True
    db_session.refresh(company)
    assert company.auto_charge_payment_method_id == "pm_new"
    assert company.stripe_customer_id == "cus_company"


async def test_confirm_rejects_foreign_customer(
    client, db_session, user_factory, company_factory, configured_stripe, fake_setup_intent
):
    """An intent vaulted under a different Stripe Customer → 400, nothing stored."""
    admin = user_factory(email="ac-confirm-foreign@example.com")
    company = company_factory(
        name="ConfirmForeignCo",
        primary_user_id=admin.id,
        net_terms_enabled=True,
        stripe_customer_id="cus_company",
    )
    fake_setup_intent["customer"] = "cus_someone_else"

    resp = await client.post(
        CONFIRM_PATH, json={"setup_intent_id": "seti_2"}, headers=_headers(admin)
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "SetupIntent customer mismatch"
    db_session.refresh(company)
    assert company.auto_charge_payment_method_id is None
    assert company.stripe_customer_id == "cus_company"


async def test_confirm_rejects_company_without_stripe_customer(
    client, db_session, user_factory, company_factory, configured_stripe, fake_setup_intent
):
    """SEC-020: a company that never ran ``/setup-intent`` has no customer id.

    The old check only fired when *both* sides were set, so this call adopted the
    attacker-supplied customer + PaymentMethod wholesale. It must 400 instead.
    """
    admin = user_factory(email="ac-confirm-nocus@example.com")
    company = company_factory(
        name="ConfirmNoCusCo",
        primary_user_id=admin.id,
        net_terms_enabled=True,
        stripe_customer_id=None,
    )
    fake_setup_intent["customer"] = "cus_attacker"

    resp = await client.post(
        CONFIRM_PATH, json={"setup_intent_id": "seti_3"}, headers=_headers(admin)
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "SetupIntent customer mismatch"
    db_session.refresh(company)
    assert company.auto_charge_payment_method_id is None
    assert company.stripe_customer_id is None


async def test_confirm_rejects_intent_without_customer(
    client, db_session, user_factory, company_factory, configured_stripe, fake_setup_intent
):
    """A customerless SetupIntent can't match, even when the company has an id."""
    admin = user_factory(email="ac-confirm-nointentcus@example.com")
    company = company_factory(
        name="ConfirmNoIntentCusCo",
        primary_user_id=admin.id,
        net_terms_enabled=True,
        stripe_customer_id="cus_company",
    )
    fake_setup_intent["customer"] = None

    resp = await client.post(
        CONFIRM_PATH, json={"setup_intent_id": "seti_4"}, headers=_headers(admin)
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "SetupIntent customer mismatch"
    db_session.refresh(company)
    assert company.auto_charge_payment_method_id is None


async def test_confirm_rejects_unfinished_intent(
    client, db_session, user_factory, company_factory, configured_stripe, fake_setup_intent
):
    """Status gate still fires ahead of the customer check."""
    admin = user_factory(email="ac-confirm-unfinished@example.com")
    company = company_factory(
        name="ConfirmUnfinishedCo",
        primary_user_id=admin.id,
        net_terms_enabled=True,
        stripe_customer_id="cus_company",
    )
    fake_setup_intent["status"] = "requires_action"

    resp = await client.post(
        CONFIRM_PATH, json={"setup_intent_id": "seti_5"}, headers=_headers(admin)
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "SetupIntent not completed"
    db_session.refresh(company)
    assert company.auto_charge_payment_method_id is None


async def test_setup_intent_then_confirm_round_trip(
    client,
    db_session,
    user_factory,
    company_factory,
    configured_stripe,
    fake_setup_intent,
    monkeypatch,
):
    """The real flow: ``/setup-intent`` ensures the customer, ``/confirm`` matches it.

    This is the positive control for the strict check — the endpoint pair that
    actually creates the customer id must still succeed end to end.
    """
    admin = user_factory(email="ac-roundtrip@example.com")
    company = company_factory(
        name="RoundTripCo",
        primary_user_id=admin.id,
        net_terms_enabled=True,
        stripe_customer_id=None,
    )
    monkeypatch.setattr(stripe.Customer, "create", lambda **_k: SimpleNamespace(id="cus_roundtrip"))
    monkeypatch.setattr(
        stripe.SetupIntent,
        "create",
        lambda **_k: SimpleNamespace(client_secret="seti_rt_secret", id="seti_rt"),
    )

    start = await client.post(SETUP_INTENT_PATH, headers=_headers(admin))
    assert start.status_code == 200, start.text
    assert start.json()["client_secret"] == "seti_rt_secret"
    db_session.refresh(company)
    assert company.stripe_customer_id == "cus_roundtrip"

    fake_setup_intent["customer"] = "cus_roundtrip"
    confirm = await client.post(
        CONFIRM_PATH, json={"setup_intent_id": "seti_rt"}, headers=_headers(admin)
    )

    assert confirm.status_code == 200, confirm.text
    db_session.refresh(company)
    assert company.auto_charge_payment_method_id == "pm_new"
