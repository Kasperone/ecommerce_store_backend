"""Unit tests for ``CRUDBanner`` (DB-backed, no HTTP).

Covers create + flush-assigned id, the ``is_active`` + ``placement`` filters,
list ordering (sort_order asc, then newest first), partial update, and delete.
Cross-field validation (date window) is enforced by the Pydantic schema, so it's
exercised in the schema/endpoint tests rather than here — the CRUD layer trusts a
validated ``BannerCreate`` / ``BannerUpdate``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.crud import banner as crud_banner
from app.models.banner import Banner
from app.schemas.banner import BannerCreate, BannerUpdate
from tests.factories.banner import BannerFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def banner_factory(db_session: Session):
    """Callable creating a Banner row via the factory (session bound per-test)."""
    BannerFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    BannerFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Banner:
        return BannerFactory(**kwargs)

    return _make


def test_create_assigns_id_and_persists(db_session: Session):
    obj_in = BannerCreate(
        title="Summer Sale",
        image_url="https://cdn.example.com/summer.jpg",
        placement="home_hero",
    )
    created = crud_banner.create(db_session, obj_in=obj_in)
    assert created.id is not None
    assert created.title == "Summer Sale"
    assert created.placement == "home_hero"
    assert created.subtitle is None
    assert created.link_url is None
    assert created.sort_order == 0
    assert created.is_active is True


def test_create_keeps_optional_fields(db_session: Session):
    obj_in = BannerCreate(
        title="Promo",
        subtitle="Up to 50% off",
        image_url="https://cdn.example.com/promo.jpg",
        link_url="https://shop.example.com/sale",
        placement="promo_strip",
        sort_order=5,
    )
    created = crud_banner.create(db_session, obj_in=obj_in)
    assert created.subtitle == "Up to 50% off"
    assert created.link_url == "https://shop.example.com/sale"
    assert created.placement == "promo_strip"
    assert created.sort_order == 5


def test_get_multi_filters_is_active(db_session: Session, banner_factory):
    banner_factory(title="ACT", is_active=True)
    banner_factory(title="INACT", is_active=False)
    active = crud_banner.get_multi(db_session, is_active=True)
    assert all(b.is_active for b in active)
    inactive = crud_banner.get_multi(db_session, is_active=False)
    assert all(not b.is_active for b in inactive)


def test_get_multi_filters_placement(db_session: Session, banner_factory):
    banner_factory(title="HERO", placement="home_hero")
    banner_factory(title="STRIP", placement="promo_strip")
    heroes = crud_banner.get_multi(db_session, placement="home_hero")
    assert all(b.placement == "home_hero" for b in heroes)
    assert "HERO" in {b.title for b in heroes}
    assert "STRIP" not in {b.title for b in heroes}


def test_count_respects_filters(db_session: Session, banner_factory):
    banner_factory(title="C-A", placement="home_hero", is_active=True)
    banner_factory(title="C-B", placement="home_hero", is_active=True)
    banner_factory(title="C-C", placement="promo_strip", is_active=False)
    assert crud_banner.count(db_session) >= 3
    assert crud_banner.count(db_session, placement="home_hero") >= 2
    assert crud_banner.count(db_session, placement="home_hero", is_active=True) >= 2
    assert crud_banner.count(db_session, placement="promo_strip", is_active=True) == 0


def test_get_multi_orders_by_sort_order_then_newest(db_session: Session, banner_factory):
    # Distinct placement so this test's rows are isolated from other rows.
    banner_factory(title="FIRST", placement="promo_strip", sort_order=10)
    banner_factory(title="SECOND", placement="promo_strip", sort_order=1)
    third = banner_factory(title="THIRD", placement="promo_strip", sort_order=1)
    ordered = crud_banner.get_multi(db_session, placement="promo_strip")
    titles = [b.title for b in ordered]
    # sort_order=1 rows come before sort_order=10; within sort_order=1 the newer
    # (higher id) row comes first.
    assert titles.index("THIRD") < titles.index("FIRST")
    assert titles.index("SECOND") < titles.index("FIRST")
    assert titles.index("THIRD") < titles.index("SECOND")
    assert ordered[0].id == third.id


def test_get_multi_pagination(db_session: Session, banner_factory):
    banner_factory(title="P1", placement="promo_strip", sort_order=1)
    banner_factory(title="P2", placement="promo_strip", sort_order=2)
    page = crud_banner.get_multi(db_session, placement="promo_strip", skip=0, limit=1)
    assert len(page) == 1
    assert page[0].title == "P1"  # lowest sort_order first


def test_update_applies_only_sent_fields(db_session: Session, banner_factory):
    b = banner_factory(title="UPD", subtitle="old-sub", sort_order=0)
    crud_banner.update(db_session, banner=b, obj_in=BannerUpdate(sort_order=9))
    db_session.commit()
    db_session.refresh(b)
    assert b.sort_order == 9
    assert b.subtitle == "old-sub"  # untouched


def test_delete_removes_row(db_session: Session, banner_factory):
    b = banner_factory(title="DEL")
    bid = b.id
    crud_banner.delete(db_session, banner=b)
    db_session.commit()
    assert db_session.get(Banner, bid) is None
