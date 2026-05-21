"""Unit tests for product currency optionality + ``get_price()`` fallback.

Covers the Pydantic-level at-least-one rule and the deterministic
``PLN → USD → EUR`` fallback on the ORM model. Neither needs a DB — schema
validation is pure, and ``get_price()`` is a method on a fresh instance.
"""

import pytest
from pydantic import ValidationError

from app.models.product import Product
from app.schemas.product import ProductCreate, ProductUpdate

# ---------------------------------------------------------------------------
# ProductCreate Pydantic validator: at least one currency required
# ---------------------------------------------------------------------------


def _base_payload(**overrides) -> dict:
    payload: dict = {
        "name": "Test Product",
        "slug": "test-product",
        "price_usd": None,
        "price_pln": None,
        "price_eur": None,
    }
    payload.update(overrides)
    return payload


def test_pln_only_create_succeeds():
    product = ProductCreate(**_base_payload(price_pln=42.5))
    assert product.price_pln == 42.5
    assert product.price_usd is None
    assert product.price_eur is None


def test_usd_only_create_succeeds():
    product = ProductCreate(**_base_payload(price_usd=9.99))
    assert product.price_usd == 9.99
    assert product.price_pln is None
    assert product.price_eur is None


def test_eur_only_create_succeeds():
    product = ProductCreate(**_base_payload(price_eur=12.0))
    assert product.price_eur == 12.0


def test_all_three_currencies_set_succeeds():
    product = ProductCreate(**_base_payload(price_usd=10, price_pln=40, price_eur=9))
    assert product.price_usd == 10
    assert product.price_pln == 40
    assert product.price_eur == 9


def test_all_null_create_fails_with_at_least_one_error():
    with pytest.raises(ValidationError) as exc_info:
        ProductCreate(**_base_payload())
    assert "At least one price is required (USD, PLN, or EUR)" in str(exc_info.value)


def test_negative_or_zero_price_still_rejected():
    with pytest.raises(ValidationError):
        ProductCreate(**_base_payload(price_usd=0))
    with pytest.raises(ValidationError):
        ProductCreate(**_base_payload(price_pln=-1))


# ---------------------------------------------------------------------------
# ProductUpdate Pydantic validator: only blocks "clear all three at once"
# ---------------------------------------------------------------------------


def test_update_empty_patch_passes():
    # A bare `{}` patch must succeed — no fields touched, no rule to enforce.
    ProductUpdate.model_validate({})


def test_update_partial_patch_without_prices_passes():
    # Updating only `name` leaves existing currencies alone.
    ProductUpdate.model_validate({"name": "Renamed"})


def test_update_single_currency_patch_passes():
    # A patch that sets one currency and omits the others (existing values
    # stay) must succeed.
    ProductUpdate.model_validate({"price_pln": 42.5})


def test_update_clear_one_keep_others_passes():
    # Explicitly null on one currency, set another — the row still has at
    # least one price after the patch.
    ProductUpdate.model_validate({"price_usd": None, "price_pln": 42.5})


def test_update_clear_all_three_fails():
    with pytest.raises(ValidationError) as exc_info:
        ProductUpdate.model_validate({"price_usd": None, "price_pln": None, "price_eur": None})
    assert "At least one price is required (USD, PLN, or EUR)" in str(exc_info.value)


def test_update_set_all_three_passes():
    ProductUpdate.model_validate({"price_usd": 10, "price_pln": 40, "price_eur": 9})


# ---------------------------------------------------------------------------
# Product.get_price(): asked currency wins, then PLN → USD → EUR fallback
# ---------------------------------------------------------------------------


def test_get_price_returns_asked_currency_when_set():
    product = Product(name="x", slug="x", price_usd=20, price_pln=80, price_eur=18)
    assert product.get_price("USD") == 20
    assert product.get_price("PLN") == 80
    assert product.get_price("EUR") == 18


def test_get_price_falls_back_to_pln_first():
    product = Product(name="x", slug="x", price_pln=42, price_usd=None, price_eur=None)
    assert product.get_price("USD") == 42
    assert product.get_price("EUR") == 42


def test_get_price_falls_back_to_usd_when_pln_missing():
    product = Product(name="x", slug="x", price_pln=None, price_usd=15, price_eur=None)
    assert product.get_price("PLN") == 15
    assert product.get_price("EUR") == 15


def test_get_price_falls_back_to_eur_when_pln_and_usd_missing():
    product = Product(name="x", slug="x", price_pln=None, price_usd=None, price_eur=9)
    assert product.get_price("USD") == 9
    assert product.get_price("PLN") == 9


def test_get_price_returns_zero_when_all_null():
    # Defensive — the CHECK constraint prevents this row from being persisted.
    # The model emits a warning log so an operator notices if it ever fires.
    product = Product(name="x", slug="x", price_pln=None, price_usd=None, price_eur=None)
    assert product.get_price("USD") == 0.0


def test_get_price_currency_arg_is_case_insensitive():
    product = Product(name="x", slug="x", price_usd=10, price_pln=40, price_eur=9)
    assert product.get_price("usd") == 10
    assert product.get_price("pln") == 40
