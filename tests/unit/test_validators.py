"""Unit tests for app/core/validators.py (pure functions, no DB)."""

from __future__ import annotations

from app.core.validators import is_valid_nip, normalize_nip


class TestIsValidNip:
    def test_accepts_valid_nip(self):
        # Real, checksum-valid Polish NIPs.
        assert is_valid_nip("5260001246")
        assert is_valid_nip("1234563218")

    def test_accepts_formatted_nip(self):
        # Dashes / spaces / PL prefix are ignored.
        assert is_valid_nip("526-000-12-46")
        assert is_valid_nip("PL 1234563218")

    def test_rejects_bad_checksum(self):
        assert not is_valid_nip("5260001247")  # last digit flipped
        assert not is_valid_nip("1234567890")

    def test_rejects_wrong_length(self):
        assert not is_valid_nip("123")
        assert not is_valid_nip("12345632180")  # 11 digits

    def test_rejects_non_digit(self):
        assert not is_valid_nip("ABCDEFGHIJ")
        assert not is_valid_nip("")


class TestNormalizeNip:
    def test_strips_non_digits(self):
        assert normalize_nip("526-000-12-46") == "5260001246"
        assert normalize_nip("PL 1234563218") == "1234563218"
