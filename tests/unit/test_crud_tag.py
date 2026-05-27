"""Unit tests for ``crud_tag`` (DB-backed, no HTTP)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.crud import tag as crud_tag
from app.crud.tag import normalize_name

pytestmark = pytest.mark.integration


def test_normalize_name():
    assert normalize_name("  VIP  ") == "VIP"
    assert normalize_name("two   words") == "two words"
    assert normalize_name("\tlead\ntrail ") == "lead trail"


def test_get_or_create_creates_then_returns_existing(db_session: Session):
    first = crud_tag.get_or_create(db_session, name="Gold", color="#facc15")
    db_session.commit()
    assert first.id is not None
    assert first.name == "Gold"

    # ci match returns the same row, does not create a second
    again = crud_tag.get_or_create(db_session, name="  gold ")
    assert again.id == first.id


def test_get_by_name_ci(db_session: Session):
    created = crud_tag.create_tag(db_session, name="Wholesale")
    db_session.commit()
    found = crud_tag.get_by_name_ci(db_session, name="WHOLESALE")
    assert found is not None
    assert found.id == created.id
    assert crud_tag.get_by_name_ci(db_session, name="nope") is None


def test_attach_detach_idempotent(db_session: Session, user_factory):
    user = user_factory(email="ad-cust@example.com")
    t = crud_tag.create_tag(db_session, name="seg")
    db_session.commit()

    assert crud_tag.attach(db_session, user=user, tag=t) is True
    assert crud_tag.attach(db_session, user=user, tag=t) is False  # already there
    db_session.commit()
    assert [x.id for x in user.tags] == [t.id]

    assert crud_tag.detach(db_session, user=user, tag=t) is True
    assert crud_tag.detach(db_session, user=user, tag=t) is False  # already gone
    db_session.commit()
    assert user.tags == []


def test_list_with_counts_and_usage(db_session: Session, user_factory):
    a = user_factory(email="lc-a@example.com")
    b = user_factory(email="lc-b@example.com")
    vip = crud_tag.create_tag(db_session, name="VIP")
    cold = crud_tag.create_tag(db_session, name="cold")
    db_session.commit()
    crud_tag.attach(db_session, user=a, tag=vip)
    crud_tag.attach(db_session, user=b, tag=vip)
    db_session.commit()

    rows = crud_tag.list_with_counts(db_session)
    by_name = {t.name: count for t, count in rows}
    assert by_name == {"cold": 0, "VIP": 2}
    # ordered by lower(name): "cold" before "VIP"
    assert [t.name for t, _ in rows] == ["cold", "VIP"]
    assert crud_tag.usage_count(db_session, tag_id=vip.id) == 2
    assert crud_tag.usage_count(db_session, tag_id=cold.id) == 0
