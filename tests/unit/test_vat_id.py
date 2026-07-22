"""EU VAT-ID format handling (``app/core/vat_id.py``).

Format-only: these pin normalisation, prefix rules (EL/XI aren't ISO-2), and the
pydantic type's accept/reject boundary. Whether a number is *real* is VIES's job,
not tested here.
"""

import pytest
from pydantic import BaseModel, ValidationError

from app.core.vat_id import (
    EuVatId,
    is_vat_id,
    normalize_vat_id,
    split_vat_id,
)


class TestNormalize:
    def test_strips_spaces_and_uppercases(self) -> None:
        assert normalize_vat_id(" pl 525 244 57 24 ") == "PL5252445724"

    def test_already_canonical_is_unchanged(self) -> None:
        assert normalize_vat_id("DE811907980") == "DE811907980"


class TestIsVatId:
    @pytest.mark.parametrize("value", ["PL5252445724", "DE811907980", "EL123456789", "XI123456789"])
    def test_accepts_well_formed_with_known_prefix(self, value: str) -> None:
        assert is_vat_id(value) is True

    def test_rejects_greece_as_gr_not_el(self) -> None:
        # ISO-2 for Greece is GR, but the VAT prefix is EL — GR is not a VAT prefix.
        assert is_vat_id("GR123456789") is False

    @pytest.mark.parametrize("value", ["Poland", "PL", "1234567890", "ZZ123456", None, ""])
    def test_rejects_malformed_or_unknown_prefix(self, value: str | None) -> None:
        assert is_vat_id(value) is False


class TestSplit:
    def test_splits_prefix_and_number(self) -> None:
        assert split_vat_id("PL5252445724") == ("PL", "5252445724")


class _Model(BaseModel):
    v: EuVatId


class TestEuVatIdType:
    def test_coerces_and_accepts(self) -> None:
        assert _Model(v=" pl 525 244 57 24 ").v == "PL5252445724"

    def test_rejects_country_name(self) -> None:
        with pytest.raises(ValidationError):
            _Model(v="Poland")

    def test_rejects_unknown_prefix(self) -> None:
        with pytest.raises(ValidationError):
            _Model(v="ZZ123456789")
