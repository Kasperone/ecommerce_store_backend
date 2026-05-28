"""Unit tests for the INVOICE_NUMBER_FORMAT Settings validator (fail-loud at load)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_default_format_is_valid():
    # The shipped default must load without error. (pydantic-settings loads the
    # required fields from .env — pyright can't see that, hence the ignores.)
    settings = Settings()  # pyright: ignore[reportCallIssue]
    assert settings.INVOICE_NUMBER_FORMAT == "{prefix}/{year}/{seq:05d}"


@pytest.mark.parametrize(
    "template",
    [
        "{prefix}/{year}/{seq:05d}",
        "{year}/{seq:04d}",
        "{prefix}-{year}{month:02d}-{seq:06d}",
        "INV{seq}",  # no zero-pad, no year — still a usable template
    ],
)
def test_valid_custom_templates_accepted(template):
    loaded = Settings(INVOICE_NUMBER_FORMAT=template).INVOICE_NUMBER_FORMAT  # pyright: ignore[reportCallIssue]
    assert loaded == template


@pytest.mark.parametrize(
    "template",
    [
        "{prefix}/{year}/{bogus}",  # unknown placeholder
        "{prefix}/{seq:zzz}",  # bad format spec
        "{0}/{seq}",  # positional field not provided
    ],
)
def test_malformed_templates_rejected(template):
    with pytest.raises(ValidationError):
        Settings(INVOICE_NUMBER_FORMAT=template)  # pyright: ignore[reportCallIssue]
