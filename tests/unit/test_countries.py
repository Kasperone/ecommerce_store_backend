"""ISO-2 country handling at the API boundary.

The point of these tests is the *rejection*: ``shipping_country`` is passed
straight into the country-keyed shipping-rate resolver and will key destination
VAT, so a free-text country name must never get past the schema. Before this
was tightened, ``'Poland'`` was accepted, stored, and then silently matched no
shipping zone.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.countries import is_iso2, normalize_country_code
from app.schemas.draft_order import DraftOrderUpdate
from app.schemas.order import OrderCreate


def _order_payload(country: Any) -> dict[str, Any]:
    return {
        "shipping_address": "ul. Testowa 1",
        "shipping_city": "Warszawa",
        "shipping_postal_code": "00-001",
        "shipping_country": country,
        "shipping_method_id": 1,
        "items": [{"product_id": 1, "quantity": 1}],
    }


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("pl", "PL"), (" PL ", "PL"), ("Pl", "PL"), ("PL", "PL")],
    )
    def test_normalizes_case_and_whitespace(self, raw: str, expected: str) -> None:
        assert normalize_country_code(raw) == expected

    def test_is_iso2(self) -> None:
        assert is_iso2("PL")
        assert not is_iso2("pl")  # normalize first; this checks the stored form
        assert not is_iso2("POL")
        assert not is_iso2("Poland")
        assert not is_iso2(None)


class TestOrderCreateCountry:
    @pytest.mark.parametrize("raw", ["pl", " pl ", "PL"])
    def test_accepts_and_normalizes_a_code(self, raw: str) -> None:
        assert OrderCreate(**_order_payload(raw)).shipping_country == "PL"

    @pytest.mark.parametrize(
        "raw",
        ["Poland", "Polska", "USA", "POL", "P", "", "  ", "P1", "12"],
    )
    def test_rejects_anything_that_is_not_a_code(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            OrderCreate(**_order_payload(raw))

    def test_country_optional_at_schema_level(self) -> None:
        # Gift cards v2: ``shipping_country`` is optional at the schema level so an
        # all-gift-card order can omit it. A missing country is accepted here
        # (→ None); ``create_with_items`` enforces it for carts with a physical
        # line (``ShippingRequiredError`` → 422), covered in the order CRUD tests.
        payload = _order_payload("PL")
        del payload["shipping_country"]
        assert OrderCreate(**payload).shipping_country is None


class TestDraftOrderCountry:
    """Drafts convert straight into orders, so they take the same constraint —
    but the address is optional while drafting, so NULL stays legal."""

    def test_optional_but_validated(self) -> None:
        assert DraftOrderUpdate().shipping_country is None
        assert DraftOrderUpdate(shipping_country="de").shipping_country == "DE"
        with pytest.raises(ValidationError):
            DraftOrderUpdate(shipping_country="Germany")
