"""VIES validation service (``app/services/vies.py``), with the EU REST API mocked.

respx intercepts the service's outbound ``httpx.Client`` call. The behaviours
that matter are the ones whose failure would mis-tax a sale downstream (U4): a
transient outage must NOT drop or invent a confirmation, and a changed answer
must flip ``vies_valid``.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.company import Company
from app.services import vies
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration

VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Company:
        return CompanyFactory(**kwargs)

    return _make


class TestCheckVat:
    @respx.mock
    def test_valid_number_returns_result(self) -> None:
        respx.post(VIES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "valid": True,
                    "name": "ACME SP Z OO",
                    "address": "PL",
                    "requestIdentifier": "REQ-1",
                },
            )
        )
        result = vies.check_vat("PL5252445724")
        assert result.valid is True
        assert result.name == "ACME SP Z OO"
        assert result.request_id == "REQ-1"

    @respx.mock
    def test_invalid_number_returns_not_valid(self) -> None:
        respx.post(VIES_URL).mock(return_value=httpx.Response(200, json={"valid": False}))
        assert vies.check_vat("PL0000000000").valid is False

    @respx.mock
    def test_ms_unavailable_raises_unavailable(self) -> None:
        respx.post(VIES_URL).mock(
            return_value=httpx.Response(200, json={"valid": False, "userError": "MS_UNAVAILABLE"})
        )
        with pytest.raises(vies.VIESUnavailableError):
            vies.check_vat("PL5252445724")

    @respx.mock
    def test_network_error_raises_unavailable(self) -> None:
        respx.post(VIES_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(vies.VIESUnavailableError):
            vies.check_vat("PL5252445724")

    @respx.mock
    def test_invalid_input_raises_validation(self) -> None:
        respx.post(VIES_URL).mock(
            return_value=httpx.Response(200, json={"valid": False, "userError": "INVALID_INPUT"})
        )
        with pytest.raises(vies.VIESValidationError):
            vies.check_vat("PL5252445724")

    def test_malformed_id_raises_validation_without_calling_vies(self) -> None:
        # No respx route registered — a call would error; the format guard must
        # short-circuit before any HTTP happens.
        with pytest.raises(vies.VIESValidationError):
            vies.check_vat("not-a-vat-id")


class TestValidateCompany:
    def test_no_vat_id_raises(self, db_session: Session, company_factory) -> None:
        company = company_factory(name="No VAT")
        with pytest.raises(vies.VIESValidationError):
            vies.validate_company_vat_id(db_session, company)

    @respx.mock
    def test_valid_stamps_confirmation(self, db_session: Session, company_factory) -> None:
        respx.post(VIES_URL).mock(
            return_value=httpx.Response(
                200, json={"valid": True, "name": "ACME", "requestIdentifier": "REQ-9"}
            )
        )
        company = company_factory(name="Acme", vat_id="PL5252445724")
        vies.validate_company_vat_id(db_session, company)
        assert company.vies_valid is True
        assert company.vies_confirmed_at is not None
        assert company.vies_confirmed_name == "ACME"
        assert company.vies_request_id == "REQ-9"
        assert company.vies_last_checked_at is not None

    @respx.mock
    def test_invalid_clears_confirmation(self, db_session: Session, company_factory) -> None:
        respx.post(VIES_URL).mock(return_value=httpx.Response(200, json={"valid": False}))
        company = company_factory(
            name="Stale",
            vat_id="PL5252445724",
            vies_valid=True,
            vies_confirmed_at=datetime(2026, 1, 1),
            vies_confirmed_name="OLD",
        )
        vies.validate_company_vat_id(db_session, company)
        assert company.vies_valid is False
        assert company.vies_confirmed_at is None
        assert company.vies_confirmed_name is None
        assert company.vies_last_checked_at is not None  # attempt still recorded

    @respx.mock
    def test_outage_records_attempt_but_keeps_confirmation(
        self, db_session: Session, company_factory
    ) -> None:
        respx.post(VIES_URL).mock(
            return_value=httpx.Response(
                200, json={"valid": False, "userError": "SERVICE_UNAVAILABLE"}
            )
        )
        confirmed_at = datetime(2026, 6, 1)
        company = company_factory(
            name="Good", vat_id="PL5252445724", vies_valid=True, vies_confirmed_at=confirmed_at
        )
        with pytest.raises(vies.VIESUnavailableError):
            vies.validate_company_vat_id(db_session, company)
        # Confirmation intact; only the attempt timestamp moved.
        assert company.vies_valid is True
        assert company.vies_confirmed_at == confirmed_at
        assert company.vies_last_checked_at is not None


class TestRecheckStaleConfirmations:
    def test_disabled_is_noop(self, db_session: Session, company_factory, monkeypatch) -> None:
        monkeypatch.setattr(settings, "VIES_ENABLED", False)
        company_factory(name="C", vat_id="PL5252445724", vies_valid=True)
        assert vies.recheck_stale_confirmations(db_session) == 0

    @respx.mock
    def test_rechecks_stale_skips_fresh(
        self, db_session: Session, company_factory, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "VIES_ENABLED", True)
        route = respx.post(VIES_URL).mock(
            return_value=httpx.Response(200, json={"valid": True, "name": "OK"})
        )
        now = datetime.now(UTC).replace(tzinfo=None)
        stale = company_factory(
            name="Stale",
            vat_id="PL5252445724",
            vies_valid=True,
            vies_confirmed_at=now - timedelta(days=200),
        )
        fresh = company_factory(
            name="Fresh",
            vat_id="DE811907980",
            vies_valid=True,
            vies_confirmed_at=now - timedelta(days=1),
        )
        count = vies.recheck_stale_confirmations(db_session, older_than_days=90)
        assert count == 1
        assert route.call_count == 1  # only the stale one hit VIES
        db_session.refresh(stale)
        db_session.refresh(fresh)
        assert stale.vies_last_checked_at is not None
