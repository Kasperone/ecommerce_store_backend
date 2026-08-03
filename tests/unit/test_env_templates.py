"""The env templates must document every setting an operator has to know about.

This is a *class* fix, not an instance fix. Three separate PRs have now shipped
to add a setting somebody forgot to put in a template — BE #170
(``SSR_SHARED_SECRET``), FE #261 (four SEO keys + the Google tags), and the one
that added this file (``STORE_VAT_ID``, ``TAX_HOME_COUNTRY``, the ``VIES_*``
block, ``NET_TERMS_DUNNING_*``, the KOR numbering pair, the JWT claim trio).
Every one was found by a human reading two files side by side, which is exactly
the review step that keeps getting skipped.

``Settings`` is the source of truth; the template is a derived artefact, so a
test can hold them in agreement. A new field fails this test until it is either
documented or explicitly declared not-operator-facing in ``_NOT_OPERATOR_FACING``
below — the explicit declaration being the point, since it forces the decision
to be made and recorded rather than defaulted by forgetting.

A key may be **commented out** in the template (``# FOO=bar``): that reads as
"this has a sane default, here is what it is and when you'd override it", which
is documentation. Absence is what fails.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PROD_TEMPLATE = _BACKEND_ROOT / ".env.production.example"
_DEV_TEMPLATE = _BACKEND_ROOT / ".env.example"

# Settings an operator never sets, with the reason. Anything listed here is a
# deliberate omission; anything NOT listed must appear in the template.
_NOT_OPERATOR_FACING: dict[str, str] = {
    "APP_NAME": "cosmetic; only ever changed in code",
    "APP_VERSION": "bumped in code at release, not per environment",
    "E2E_DISABLE_RATE_LIMIT": (
        "double-gated on ENVIRONMENT=development — documenting it in a PRODUCTION "
        "template would advertise a switch that cannot work there and invite someone "
        "to try"
    ),
}

# Dev-only settings: meaningful in .env.example but not in production.
_DEV_ONLY: set[str] = {"E2E_DISABLE_RATE_LIMIT"}


def _documented_keys(path: Path) -> set[str]:
    """Every env key the template mentions, commented-out ones included."""
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", text, re.MULTILINE))


def _settings_fields() -> set[str]:
    return set(Settings.model_fields)


def test_every_setting_is_documented_in_the_production_template():
    missing = _settings_fields() - _documented_keys(_PROD_TEMPLATE) - set(_NOT_OPERATOR_FACING)
    assert not missing, (
        "These Settings fields are missing from .env.production.example:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nAdd each one with a line saying what it does and what breaks if it is "
        "wrong, or add it to _NOT_OPERATOR_FACING in this file with the reason."
    )


def test_every_setting_is_documented_in_the_dev_template():
    missing = (
        _settings_fields()
        - _documented_keys(_DEV_TEMPLATE)
        - (set(_NOT_OPERATOR_FACING) - _DEV_ONLY)
    )
    assert not missing, (
        "These Settings fields are missing from .env.example:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nA developer setting up the project reads this file to learn what exists."
    )


def test_the_exemption_list_has_no_stale_entries():
    """An exemption for a field that no longer exists hides a real gap: if the
    name is later reused for something operator-facing, the stale entry silently
    exempts it."""
    stale = set(_NOT_OPERATOR_FACING) - _settings_fields()
    assert not stale, f"_NOT_OPERATOR_FACING names settings that no longer exist: {sorted(stale)}"


def test_production_template_does_not_ship_a_usable_secret():
    """The template is committed. Anything that looks like a real secret in it is
    a real secret that leaked — the placeholders must stay obviously fake."""
    text = _PROD_TEMPLATE.read_text(encoding="utf-8")
    for key in ("SECRET_KEY", "JWT_SECRET_KEY", "SSR_SHARED_SECRET"):
        for match in re.finditer(rf"^{key}=(.*)$", text, re.MULTILINE):
            value = match.group(1).strip()
            assert value == "" or "CHANGE" in value.upper(), (
                f"{key} in .env.production.example looks like a real value ({value!r}). "
                "Templates must carry an obviously-fake placeholder."
            )
