"""Integration tests for the public banners API (/api/v1/banners).

The customer-facing read side of storefront banners: unauthenticated, returns
only banners that are **active** AND currently within their optional display
window (``[starts_at, ends_at]``, null bounds = open-ended), ordered by
``sort_order`` then ``id``. Supports an optional ``placement`` filter. An empty
result is a valid 200 ``[]`` (never a 404). The slim ``BannerPublic`` shape omits
audit/window/``is_active`` noise.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.banner import Banner
from tests.factories.banner import BannerFactory

pytestmark = pytest.mark.integration

NOW = datetime.utcnow()
PAST = NOW - timedelta(days=1)
FUTURE = NOW + timedelta(days=1)


@pytest.fixture
def banner_factory(db_session: Session):
    """Callable creating a Banner row via the factory (session bound per-test)."""
    BannerFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    BannerFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Banner:
        return BannerFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# no auth required
# ---------------------------------------------------------------------------


async def test_public_no_auth_required(client, banner_factory):
    """No token → 200 (this is a public endpoint, unlike /api/v1/admin/banners)."""
    banner_factory(title="PUBLIC", is_active=True)
    response = await client.get("/api/v1/banners/")
    assert response.status_code == 200


async def test_empty_returns_200_not_404(client):
    """No matching banners → empty list with 200, never a 404."""
    response = await client.get("/api/v1/banners/")
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# active + window filtering
# ---------------------------------------------------------------------------


async def test_inactive_banners_excluded(client, banner_factory):
    banner_factory(title="ON", is_active=True)
    banner_factory(title="OFF", is_active=False)
    response = await client.get("/api/v1/banners/")
    titles = [b["title"] for b in response.json()]
    assert titles == ["ON"]


async def test_window_open_ended_included(client, banner_factory):
    """Null starts_at/ends_at = open-ended on that side → always in-window."""
    banner_factory(title="OPEN", starts_at=None, ends_at=None, is_active=True)
    response = await client.get("/api/v1/banners/")
    assert [b["title"] for b in response.json()] == ["OPEN"]


async def test_window_currently_active_included(client, banner_factory):
    banner_factory(title="LIVE", starts_at=PAST, ends_at=FUTURE, is_active=True)
    response = await client.get("/api/v1/banners/")
    assert [b["title"] for b in response.json()] == ["LIVE"]


async def test_window_not_yet_started_excluded(client, banner_factory):
    banner_factory(title="SCHEDULED", starts_at=FUTURE, ends_at=None, is_active=True)
    response = await client.get("/api/v1/banners/")
    assert response.json() == []


async def test_window_already_ended_excluded(client, banner_factory):
    banner_factory(title="EXPIRED", starts_at=None, ends_at=PAST, is_active=True)
    response = await client.get("/api/v1/banners/")
    assert response.json() == []


async def test_lower_bound_only_in_window(client, banner_factory):
    """starts_at in the past, no end → in-window."""
    banner_factory(title="STARTED", starts_at=PAST, ends_at=None, is_active=True)
    response = await client.get("/api/v1/banners/")
    assert [b["title"] for b in response.json()] == ["STARTED"]


async def test_upper_bound_only_in_window(client, banner_factory):
    """no start, ends_at in the future → in-window."""
    banner_factory(title="UNTIL", starts_at=None, ends_at=FUTURE, is_active=True)
    response = await client.get("/api/v1/banners/")
    assert [b["title"] for b in response.json()] == ["UNTIL"]


# ---------------------------------------------------------------------------
# placement filter
# ---------------------------------------------------------------------------


async def test_placement_filter(client, banner_factory):
    banner_factory(title="HERO", placement="home_hero", is_active=True)
    banner_factory(title="STRIP", placement="promo_strip", is_active=True)

    hero = await client.get("/api/v1/banners/", params={"placement": "home_hero"})
    assert [b["title"] for b in hero.json()] == ["HERO"]

    strip = await client.get("/api/v1/banners/", params={"placement": "promo_strip"})
    assert [b["title"] for b in strip.json()] == ["STRIP"]


async def test_placement_omitted_returns_all(client, banner_factory):
    banner_factory(title="HERO", placement="home_hero", sort_order=1, is_active=True)
    banner_factory(title="STRIP", placement="promo_strip", sort_order=2, is_active=True)
    response = await client.get("/api/v1/banners/")
    assert {b["title"] for b in response.json()} == {"HERO", "STRIP"}


async def test_placement_invalid_value_422(client):
    """Unknown placement is rejected by the literal type (mirrors admin route)."""
    response = await client.get("/api/v1/banners/", params={"placement": "footer"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


async def test_ordered_by_sort_order_then_id(client, banner_factory):
    # Insert out of sort_order; same sort_order resolves by id asc.
    banner_factory(title="THIRD", sort_order=5, is_active=True)
    first = banner_factory(title="FIRST", sort_order=1, is_active=True)
    second = banner_factory(title="SECOND", sort_order=1, is_active=True)
    assert first.id < second.id  # sanity: id tie-break order

    response = await client.get("/api/v1/banners/")
    assert [b["title"] for b in response.json()] == ["FIRST", "SECOND", "THIRD"]


# ---------------------------------------------------------------------------
# slim response shape
# ---------------------------------------------------------------------------


async def test_response_shape_is_slim(client, banner_factory):
    banner_factory(
        title="SHAPE",
        subtitle="sub",
        image_url="https://cdn.example.com/x.jpg",
        link_url="https://example.com/sale",
        placement="home_hero",
        sort_order=3,
        is_active=True,
    )
    response = await client.get("/api/v1/banners/")
    body = response.json()
    assert len(body) == 1
    item = body[0]
    assert set(item.keys()) == {
        "id",
        "title",
        "subtitle",
        "image_url",
        "link_url",
        "placement",
        "sort_order",
    }
    assert item["title"] == "SHAPE"
    assert item["subtitle"] == "sub"
    assert item["image_url"] == "https://cdn.example.com/x.jpg"
    assert item["link_url"] == "https://example.com/sale"
    assert item["placement"] == "home_hero"
    assert item["sort_order"] == 3
    # audit/window/is_active noise must NOT leak into the public shape
    for leaked in ("is_active", "starts_at", "ends_at", "created_at", "updated_at"):
        assert leaked not in item
