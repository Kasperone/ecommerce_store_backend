"""Unit tests for ``CRUDCoupon`` (DB-backed, no HTTP).

Covers create + flush-assigned id, code normalisation/lookup, the ``is_active`` +
``search`` filters, pagination ordering (newest first), partial update, and
delete. Validation (value range, currency-by-type, date window) is enforced by
the Pydantic schema, so it's exercised in the schema/endpoint tests rather than
here — the CRUD layer trusts a validated ``CouponCreate``/``CouponUpdate``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.crud import coupon as crud_coupon
from app.models.coupon import Coupon
from app.schemas.coupon import CouponCreate, CouponUpdate
from tests.factories.coupon import CouponFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def coupon_factory(db_session: Session):
    """Callable creating a Coupon row via the factory (session bound per-test)."""
    CouponFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    CouponFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Coupon:
        return CouponFactory(**kwargs)

    return _make


def test_create_percent_assigns_id_and_persists(db_session: Session):
    obj_in = CouponCreate(code="welcome10", discount_type="percent", value=10)
    created = crud_coupon.create(db_session, obj_in=obj_in)
    assert created.id is not None
    # Schema uppercases the code on the way in.
    assert created.code == "WELCOME10"
    assert created.discount_type == "percent"
    assert created.currency is None
    assert created.used_count == 0
    assert created.is_active is True


def test_create_fixed_keeps_currency(db_session: Session):
    obj_in = CouponCreate(code="flat5", discount_type="fixed", value=5, currency="EUR")
    created = crud_coupon.create(db_session, obj_in=obj_in)
    assert created.discount_type == "fixed"
    assert created.currency == "EUR"
    assert created.value == 5


def test_get_by_code_is_case_insensitive(db_session: Session, coupon_factory):
    coupon_factory(code="SUMMER")
    assert crud_coupon.get_by_code(db_session, code="summer") is not None
    assert crud_coupon.get_by_code(db_session, code="  Summer  ") is not None
    assert crud_coupon.get_by_code(db_session, code="winter") is None


def test_get_multi_filters_is_active(db_session: Session, coupon_factory):
    coupon_factory(code="ACT1", is_active=True)
    coupon_factory(code="INACT1", is_active=False)
    active = crud_coupon.get_multi(db_session, is_active=True)
    assert all(c.is_active for c in active)
    inactive = crud_coupon.get_multi(db_session, is_active=False)
    assert all(not c.is_active for c in inactive)
    assert {"ACT1"}.issubset({c.code for c in active})
    assert {"INACT1"}.issubset({c.code for c in inactive})


def test_get_multi_search_by_code_substring(db_session: Session, coupon_factory):
    coupon_factory(code="BLACKFRIDAY")
    coupon_factory(code="CYBERMONDAY")
    coupon_factory(code="NEWYEAR")
    hits = crud_coupon.get_multi(db_session, search="friday")
    codes = {c.code for c in hits}
    assert "BLACKFRIDAY" in codes
    assert "CYBERMONDAY" not in codes


def test_count_respects_filters(db_session: Session, coupon_factory):
    coupon_factory(code="CNT-A", is_active=True)
    coupon_factory(code="CNT-B", is_active=True)
    coupon_factory(code="CNT-C", is_active=False)
    assert crud_coupon.count(db_session) >= 3
    assert crud_coupon.count(db_session, search="CNT-") == 3
    assert crud_coupon.count(db_session, search="CNT-", is_active=True) == 2


def test_get_multi_pagination_newest_first(db_session: Session, coupon_factory):
    first = coupon_factory(code="PAGE-1")
    second = coupon_factory(code="PAGE-2")
    page = crud_coupon.get_multi(db_session, search="PAGE-", skip=0, limit=1)
    assert len(page) == 1
    # Newest (highest id) first.
    assert page[0].id == max(first.id, second.id)


def test_update_applies_only_sent_fields(db_session: Session, coupon_factory):
    c = coupon_factory(code="UPD1", value=10, description="old")
    crud_coupon.update(db_session, coupon=c, obj_in=CouponUpdate(value=25))
    db_session.commit()
    db_session.refresh(c)
    assert c.value == 25
    assert c.description == "old"  # untouched


def test_delete_removes_row(db_session: Session, coupon_factory):
    c = coupon_factory(code="DEL1")
    cid = c.id
    crud_coupon.delete(db_session, coupon=c)
    db_session.commit()
    assert db_session.get(Coupon, cid) is None


# ---------------------------------------------------------------------------
# Redemption: compute_discount + validate_for_redemption + redeem
# ---------------------------------------------------------------------------

from datetime import UTC, datetime, timedelta  # noqa: E402

from app.crud.coupon import CouponRedemptionError, compute_discount  # noqa: E402

_NOW = datetime.now(UTC).replace(tzinfo=None)


def test_compute_discount_percent():
    c = CouponFactory.build(discount_type="percent", value=10, currency=None)
    assert compute_discount(c, 200.0) == 20.0


def test_compute_discount_fixed_capped_at_subtotal():
    c = CouponFactory.build(discount_type="fixed", value=50, currency="USD")
    assert compute_discount(c, 30.0) == 30.0  # never below zero
    assert compute_discount(c, 80.0) == 50.0


def test_validate_percent_returns_coupon_and_discount(db_session, coupon_factory):
    coupon_factory(code="PCT20", discount_type="percent", value=20, currency=None)
    found, discount = crud_coupon.validate_for_redemption(
        db_session, code="pct20", subtotal=100.0, currency="USD"
    )
    assert found.code == "PCT20"
    assert discount == 20.0


def test_validate_unknown_code_raises_not_found(db_session):
    with pytest.raises(CouponRedemptionError) as exc:
        crud_coupon.validate_for_redemption(db_session, code="NOPE", subtotal=100.0, currency="USD")
    assert exc.value.code == "not_found"


def test_validate_inactive_raises(db_session, coupon_factory):
    coupon_factory(code="OFF", is_active=False)
    with pytest.raises(CouponRedemptionError) as exc:
        crud_coupon.validate_for_redemption(db_session, code="OFF", subtotal=100.0, currency="USD")
    assert exc.value.code == "inactive"


def test_validate_not_started_raises(db_session, coupon_factory):
    future = _NOW + timedelta(days=1)
    coupon_factory(code="SOON", starts_at=future)
    with pytest.raises(CouponRedemptionError) as exc:
        crud_coupon.validate_for_redemption(db_session, code="SOON", subtotal=100.0, currency="USD")
    assert exc.value.code == "not_started"


def test_validate_expired_raises(db_session, coupon_factory):
    past = _NOW - timedelta(days=1)
    coupon_factory(code="GONE", ends_at=past)
    with pytest.raises(CouponRedemptionError) as exc:
        crud_coupon.validate_for_redemption(db_session, code="GONE", subtotal=100.0, currency="USD")
    assert exc.value.code == "expired"


def test_validate_usage_exhausted_raises(db_session, coupon_factory):
    coupon_factory(code="MAXED", max_uses=2, used_count=2)
    with pytest.raises(CouponRedemptionError) as exc:
        crud_coupon.validate_for_redemption(
            db_session, code="MAXED", subtotal=100.0, currency="USD"
        )
    assert exc.value.code == "usage_exhausted"


def test_validate_fixed_currency_mismatch_raises(db_session, coupon_factory):
    coupon_factory(code="USDONLY", discount_type="fixed", value=10, currency="USD")
    with pytest.raises(CouponRedemptionError) as exc:
        crud_coupon.validate_for_redemption(
            db_session, code="USDONLY", subtotal=100.0, currency="PLN"
        )
    assert exc.value.code == "currency_mismatch"


def test_validate_min_order_not_met_raises(db_session, coupon_factory):
    coupon_factory(code="BIG", min_order_amount=100.0)
    with pytest.raises(CouponRedemptionError) as exc:
        crud_coupon.validate_for_redemption(db_session, code="BIG", subtotal=50.0, currency="USD")
    assert exc.value.code == "min_order_not_met"


def test_redeem_increments_used_count(db_session, coupon_factory):
    c = coupon_factory(code="USE1", max_uses=5, used_count=0)
    crud_coupon.redeem(db_session, coupon=c)
    db_session.commit()
    db_session.refresh(c)
    assert c.used_count == 1


def test_redeem_at_cap_raises_usage_exhausted(db_session, coupon_factory):
    c = coupon_factory(code="USE2", max_uses=1, used_count=1)
    with pytest.raises(CouponRedemptionError) as exc:
        crud_coupon.redeem(db_session, coupon=c)
    assert exc.value.code == "usage_exhausted"


# ---------------------------------------------------------------------------
# Automatic discounts + free_shipping: effective_saving / get_active_automatic
# / pick_best
# ---------------------------------------------------------------------------

from app.crud.coupon import effective_saving  # noqa: E402


def test_compute_discount_free_shipping_is_zero_goods_discount():
    c = CouponFactory.build(discount_type="free_shipping", value=0, currency=None)
    assert compute_discount(c, 200.0) == 0.0


def test_effective_saving_percent_and_fixed_use_goods_discount():
    pct = CouponFactory.build(discount_type="percent", value=10, currency=None)
    assert effective_saving(pct, 200.0, 15.0) == 20.0
    fixed = CouponFactory.build(discount_type="fixed", value=50, currency="USD")
    assert effective_saving(fixed, 30.0, 15.0) == 30.0  # capped at subtotal


def test_effective_saving_free_shipping_is_the_shipping_amount():
    c = CouponFactory.build(discount_type="free_shipping", value=0, currency=None)
    assert effective_saving(c, 200.0, 12.5) == 12.5
    # Free-above already zeroed shipping → the coupon saves nothing.
    assert effective_saving(c, 200.0, 0.0) == 0.0


def test_get_active_automatic_filters(db_session, coupon_factory):
    auto = coupon_factory(code="AUTO-ON", is_automatic=True)
    coupon_factory(code="AUTO-OFF", is_automatic=True, is_active=False)
    coupon_factory(code="MANUAL-1", is_automatic=False)
    coupon_factory(code="AUTO-FUTURE", is_automatic=True, starts_at=_NOW + timedelta(days=1))
    coupon_factory(code="AUTO-PAST", is_automatic=True, ends_at=_NOW - timedelta(days=1))
    coupon_factory(code="AUTO-MAXED", is_automatic=True, max_uses=1, used_count=1)
    codes = {c.code for c in crud_coupon.get_active_automatic(db_session, now=_NOW)}
    assert auto.code in codes
    assert codes.isdisjoint({"AUTO-OFF", "MANUAL-1", "AUTO-FUTURE", "AUTO-PAST", "AUTO-MAXED"})


def test_pick_best_nothing_applies_returns_none(db_session):
    assert (
        crud_coupon.pick_best(db_session, code=None, subtotal=100.0, shipping=10.0, currency="USD")
        is None
    )


def test_pick_best_automatic_only(db_session, coupon_factory):
    auto = coupon_factory(code="AUTO10", is_automatic=True, discount_type="percent", value=10)
    picked = crud_coupon.pick_best(
        db_session, code=None, subtotal=100.0, shipping=10.0, currency="USD"
    )
    assert picked is not None
    assert picked.coupon.id == auto.id
    assert picked.saving == 10.0
    assert picked.is_manual is False
    assert picked.free_shipping is False


def test_pick_best_manual_only(db_session, coupon_factory):
    coupon_factory(code="HAND10", discount_type="percent", value=10)
    picked = crud_coupon.pick_best(
        db_session, code="hand10", subtotal=100.0, shipping=10.0, currency="USD"
    )
    assert picked is not None
    assert picked.coupon.code == "HAND10"
    assert picked.is_manual is True


def test_pick_best_bigger_automatic_beats_manual(db_session, coupon_factory):
    coupon_factory(code="HAND5", discount_type="percent", value=5)
    auto = coupon_factory(code="AUTO20", is_automatic=True, discount_type="percent", value=20)
    picked = crud_coupon.pick_best(
        db_session, code="HAND5", subtotal=100.0, shipping=0.0, currency="USD"
    )
    assert picked is not None
    assert picked.coupon.id == auto.id
    assert picked.saving == 20.0
    assert picked.is_manual is False


def test_pick_best_manual_beats_smaller_automatic(db_session, coupon_factory):
    coupon_factory(code="HAND25", discount_type="percent", value=25)
    coupon_factory(code="AUTO10B", is_automatic=True, discount_type="percent", value=10)
    picked = crud_coupon.pick_best(
        db_session, code="HAND25", subtotal=100.0, shipping=0.0, currency="USD"
    )
    assert picked is not None
    assert picked.coupon.code == "HAND25"
    assert picked.is_manual is True


def test_pick_best_tie_prefers_manual(db_session, coupon_factory):
    coupon_factory(code="HANDTIE", discount_type="percent", value=10)
    coupon_factory(code="AUTOTIE", is_automatic=True, discount_type="percent", value=10)
    picked = crud_coupon.pick_best(
        db_session, code="HANDTIE", subtotal=100.0, shipping=0.0, currency="USD"
    )
    assert picked is not None
    assert picked.coupon.code == "HANDTIE"


def test_pick_best_tie_between_automatics_prefers_lowest_id(db_session, coupon_factory):
    first = coupon_factory(code="AUTOTIE1", is_automatic=True, discount_type="percent", value=10)
    coupon_factory(code="AUTOTIE2", is_automatic=True, discount_type="percent", value=10)
    picked = crud_coupon.pick_best(
        db_session, code=None, subtotal=100.0, shipping=0.0, currency="USD"
    )
    assert picked is not None
    assert picked.coupon.id == first.id


def test_pick_best_free_shipping_vs_goods_discount(db_session, coupon_factory):
    coupon_factory(code="AUTOSHIP", is_automatic=True, discount_type="free_shipping", value=0)
    coupon_factory(code="AUTOPCT5", is_automatic=True, discount_type="percent", value=5)
    # Shipping 20 > 5% of 100 → free_shipping wins.
    picked = crud_coupon.pick_best(
        db_session, code=None, subtotal=100.0, shipping=20.0, currency="USD"
    )
    assert picked is not None
    assert picked.coupon.code == "AUTOSHIP"
    assert picked.free_shipping is True
    assert picked.saving == 20.0


def test_pick_best_free_shipping_skipped_when_shipping_already_zero(db_session, coupon_factory):
    # Free-above already zeroed shipping → the free_shipping automatic saves 0
    # and the goods-discount candidate wins instead.
    coupon_factory(code="AUTOSHIP0", is_automatic=True, discount_type="free_shipping", value=0)
    pct = coupon_factory(code="AUTOPCT5B", is_automatic=True, discount_type="percent", value=5)
    picked = crud_coupon.pick_best(
        db_session, code=None, subtotal=100.0, shipping=0.0, currency="USD"
    )
    assert picked is not None
    assert picked.coupon.id == pct.id
    assert picked.free_shipping is False


def test_pick_best_invalid_manual_code_raises_despite_automatics(db_session, coupon_factory):
    # A typed code that fails validation 400s — it never silently falls back to
    # an eligible automatic.
    coupon_factory(code="AUTOOK", is_automatic=True, discount_type="percent", value=10)
    with pytest.raises(CouponRedemptionError) as exc:
        crud_coupon.pick_best(db_session, code="NOPE", subtotal=100.0, shipping=0.0, currency="USD")
    assert exc.value.code == "not_found"


def test_pick_best_automatic_ineligibility_is_swallowed(db_session, coupon_factory):
    # Automatics failing per-cart checks (min-order, currency) are skipped, not
    # raised — they are opportunistic.
    coupon_factory(code="AUTOMIN", is_automatic=True, min_order_amount=500.0)
    coupon_factory(
        code="AUTOEUR", is_automatic=True, discount_type="fixed", value=5, currency="EUR"
    )
    assert (
        crud_coupon.pick_best(db_session, code=None, subtotal=100.0, shipping=0.0, currency="USD")
        is None
    )


def test_pick_best_exclude_coupon_ids_skips_automatic(db_session, coupon_factory):
    big = coupon_factory(code="AUTOBIG", is_automatic=True, discount_type="percent", value=20)
    small = coupon_factory(code="AUTOSMALL", is_automatic=True, discount_type="percent", value=10)
    picked = crud_coupon.pick_best(
        db_session,
        code=None,
        subtotal=100.0,
        shipping=0.0,
        currency="USD",
        exclude_coupon_ids={big.id},
    )
    assert picked is not None
    assert picked.coupon.id == small.id


def test_pick_best_typed_automatic_code_is_single_manual_candidate(db_session, coupon_factory):
    # Typing an automatic coupon's own code must not duplicate it; it competes
    # once, flagged manual (so a redeem failure re-raises).
    auto = coupon_factory(code="AUTOTYPED", is_automatic=True, discount_type="percent", value=10)
    picked = crud_coupon.pick_best(
        db_session, code="AUTOTYPED", subtotal=100.0, shipping=0.0, currency="USD"
    )
    assert picked is not None
    assert picked.coupon.id == auto.id
    assert picked.is_manual is True
