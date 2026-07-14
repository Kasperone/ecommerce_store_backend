"""Integration tests for the public gift-card check (/api/v1/gift-cards/check).

Covers the ships-dark toggle (409 ``gift_cards_disabled`` while
``store_config.gift_cards_enabled`` is false — the default), the machine-code
error contract (404 ``gift_card_not_found``; 409 ``gift_card_disabled`` /
``gift_card_expired`` / ``gift_card_depleted`` / ``gift_card_currency_mismatch``),
code normalization (case/dashes/spaces), the slim 200 shape, and the 10/min/IP
rate limit. The whole module resets the limiter — every request here shares the
constant "testclient" IP bucket.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.crud import gift_card as crud_gift_card
from app.crud import store_config as crud_store_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rate_limit,
    pytest.mark.usefixtures("reset_limiter"),
]

CHECK_URL = "/api/v1/gift-cards/check"


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def gift_cards_enabled(db_session):
    """Flip the ships-dark toggle on for this test (savepoint-rolled-back)."""
    config = crud_store_config.get_singleton(db_session)
    config.gift_cards_enabled = True
    db_session.commit()
    return config


def _issue(
    db,
    *,
    initial_value: Decimal = Decimal("100.00"),
    currency: str = "PLN",
    expires_at: datetime | None = None,
):
    card, code = crud_gift_card.issue(
        db, initial_value=initial_value, currency=currency, expires_at=expires_at
    )
    db.commit()
    return card, code


async def test_toggle_off_409_gift_cards_disabled(client, db_session):
    # Default store_config ships with the toggle off (D7).
    resp = await client.post(CHECK_URL, json={"code": "AAAA-BBBB-CCCC-DDDD", "currency": "PLN"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "gift_cards_disabled"


async def test_unknown_code_404_machine_code(client, db_session, gift_cards_enabled):
    resp = await client.post(CHECK_URL, json={"code": "AAAA-BBBB-CCCC-DDDD", "currency": "PLN"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "gift_card_not_found"


async def test_valid_card_returns_slim_shape(client, db_session, gift_cards_enabled):
    card, code = _issue(db_session, initial_value=Decimal("75.00"))
    resp = await client.post(CHECK_URL, json={"code": code, "currency": "PLN"})
    assert resp.status_code == 200
    assert resp.json() == {"last4": card.last4, "balance": 75.0, "currency": "PLN"}


async def test_check_normalizes_presented_code(client, db_session, gift_cards_enabled):
    _, code = _issue(db_session)
    sloppy = code.lower().replace("-", " ")
    resp = await client.post(CHECK_URL, json={"code": f"  {sloppy}  ", "currency": "PLN"})
    assert resp.status_code == 200


async def test_disabled_card_409(client, db_session, gift_cards_enabled):
    card, code = _issue(db_session)
    card.status = "disabled"
    db_session.commit()
    resp = await client.post(CHECK_URL, json={"code": code, "currency": "PLN"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "gift_card_disabled"


async def test_expired_card_409(client, db_session, gift_cards_enabled):
    _, code = _issue(db_session, expires_at=_utcnow_naive() - timedelta(days=1))
    resp = await client.post(CHECK_URL, json={"code": code, "currency": "PLN"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "gift_card_expired"


async def test_depleted_card_409(client, db_session, gift_cards_enabled):
    card, code = _issue(db_session, initial_value=Decimal("10.00"))
    crud_gift_card.adjust(db_session, gift_card_id=card.id, amount=Decimal("-10.00"))
    db_session.commit()
    resp = await client.post(CHECK_URL, json={"code": code, "currency": "PLN"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "gift_card_depleted"


async def test_currency_mismatch_409(client, db_session, gift_cards_enabled):
    _, code = _issue(db_session, currency="USD")
    resp = await client.post(CHECK_URL, json={"code": code, "currency": "PLN"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "gift_card_currency_mismatch"


@pytest.mark.parametrize(
    "body",
    [
        {"code": "", "currency": "PLN"},
        {"code": "AAAA", "currency": "GBP"},
        {"currency": "PLN"},
        {"code": "AAAA"},
    ],
)
async def test_check_validation_422(client, body):
    assert (await client.post(CHECK_URL, json=body)).status_code == 422


async def test_check_rate_limited_10_per_minute(client, db_session, gift_cards_enabled):
    _, code = _issue(db_session)
    for _ in range(10):
        ok = await client.post(CHECK_URL, json={"code": code, "currency": "PLN"})
        assert ok.status_code == 200
    throttled = await client.post(CHECK_URL, json={"code": code, "currency": "PLN"})
    assert throttled.status_code == 429
