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
