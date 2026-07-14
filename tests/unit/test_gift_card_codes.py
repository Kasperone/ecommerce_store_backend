"""Unit tests for the gift-card code helpers (pure functions, no DB).

D4 contract: 16 chars from an alphabet with no ``0/O/1/I`` via
``secrets.choice``; sha256 hex at rest; presented codes normalize (uppercase,
spaces/dashes stripped) before hashing so every display/typing variant of the
same code resolves to the same hash.
"""

from __future__ import annotations

import hashlib

from app.crud.gift_card import (
    CODE_ALPHABET,
    CODE_LENGTH,
    format_code,
    generate_code,
    hash_code,
    normalize_code,
)


def test_alphabet_excludes_ambiguous_characters():
    for forbidden in "0O1I":
        assert forbidden not in CODE_ALPHABET
    assert len(CODE_ALPHABET) == 32


def test_generate_code_length_and_alphabet():
    for _ in range(50):
        code = generate_code()
        assert len(code) == CODE_LENGTH
        assert all(ch in CODE_ALPHABET for ch in code)


def test_generate_code_is_not_constant():
    # Statistically impossible to collide across 10 draws of a 32^16 space.
    assert len({generate_code() for _ in range(10)}) == 10


def test_format_code_groups_of_four():
    assert format_code("ABCDEFGHJKLMNPQR") == "ABCD-EFGH-JKLM-NPQR"


def test_normalize_strips_separators_and_uppercases():
    assert normalize_code("abcd-efgh jklm-npqr") == "ABCDEFGHJKLMNPQR"
    assert normalize_code("  ABCD-EFGH-JKLM-NPQR  ") == "ABCDEFGHJKLMNPQR"


def test_normalize_round_trips_format():
    code = generate_code()
    assert normalize_code(format_code(code)) == code


def test_hash_is_sha256_of_normalized_form():
    code = "ABCD-EFGH-JKLM-NPQR"
    expected = hashlib.sha256(b"ABCDEFGHJKLMNPQR").hexdigest()
    assert hash_code(code) == expected
    # Every presentation variant hashes identically.
    assert hash_code("abcd efgh jklm npqr") == expected
    assert hash_code("ABCDEFGHJKLMNPQR") == expected
