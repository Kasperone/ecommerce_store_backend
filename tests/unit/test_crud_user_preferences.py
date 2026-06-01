"""Unit tests for the preferences columns + ``CRUDUser.set_preferences`` helper.

DB-backed (savepoint session), no HTTP — covers the column defaults and the
partial-update semantics the route relies on.
"""

import pytest

from app.crud import user as crud_user

pytestmark = pytest.mark.integration


def test_new_user_preferences_default_to_null(user_factory):
    """The three new columns are all nullable with no value on a fresh user."""
    user = user_factory(email="cru-pref-default@example.com")
    assert user.preferred_currency is None
    assert user.totp_secret_enc is None
    assert user.totp_enabled_at is None


def test_set_preferences_applies_each_change(db_session, user_factory):
    user = user_factory(email="cru-pref-set@example.com")
    crud_user.set_preferences(
        db_session,
        user=user,
        changes={"preferred_currency": "EUR", "locale": "pl"},
    )
    db_session.commit()
    db_session.refresh(user)
    assert user.preferred_currency == "EUR"
    assert user.locale == "pl"


def test_set_preferences_only_touches_given_fields(db_session, user_factory):
    """A change dict without ``locale`` leaves the stored locale alone."""
    user = user_factory(email="cru-pref-partial@example.com")
    user.locale = "pl"
    db_session.add(user)
    db_session.commit()

    crud_user.set_preferences(db_session, user=user, changes={"preferred_currency": "USD"})
    db_session.commit()
    db_session.refresh(user)
    assert user.preferred_currency == "USD"
    assert user.locale == "pl"  # untouched


def test_set_preferences_can_clear_currency(db_session, user_factory):
    user = user_factory(email="cru-pref-clear@example.com")
    user.preferred_currency = "PLN"
    db_session.add(user)
    db_session.commit()

    crud_user.set_preferences(db_session, user=user, changes={"preferred_currency": None})
    db_session.commit()
    db_session.refresh(user)
    assert user.preferred_currency is None
