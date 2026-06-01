"""Unit tests for the session-listing CRUD helpers on ``crud_refresh_token``.

Covers ``list_active_for_user`` (active-only, newest-first, per-user) and the
by-id ``get`` helper. DB-backed, no HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_refresh_token
from app.crud import refresh_token as crud_refresh_token

pytestmark = pytest.mark.integration


def _make_row(db: Session, *, user_id: int, expires_in_days: int = 30):
    token, jti, exp = create_refresh_token(subject=user_id)
    # Override the expiry so we can exercise the expired path deterministically.
    exp = datetime.now(UTC) + timedelta(days=expires_in_days)
    return crud_refresh_token.create(
        db,
        user_id=user_id,
        jti=jti,
        token=token,
        expires_at=exp,
        ip="1.2.3.4",
        user_agent="pytest",
    )


def test_get_by_id_returns_row(db_session: Session, user_factory):
    user = user_factory(email="getbyid@example.com")
    row = _make_row(db_session, user_id=user.id)
    found = crud_refresh_token.get(db_session, id=row.id)
    assert found is not None
    assert found.id == row.id


def test_get_by_id_unknown_returns_none(db_session: Session):
    assert crud_refresh_token.get(db_session, id=999999) is None


def test_list_active_only_returns_unrevoked_unexpired(db_session: Session, user_factory):
    user = user_factory(email="active@example.com")
    live = _make_row(db_session, user_id=user.id)
    revoked = _make_row(db_session, user_id=user.id)
    expired = _make_row(db_session, user_id=user.id, expires_in_days=-1)

    crud_refresh_token.revoke(db_session, row=revoked)

    rows = crud_refresh_token.list_active_for_user(db_session, user_id=user.id)
    ids = {r.id for r in rows}
    assert live.id in ids
    assert revoked.id not in ids
    assert expired.id not in ids


def test_list_active_is_user_scoped(db_session: Session, user_factory):
    a = user_factory(email="scope-a@example.com")
    b = user_factory(email="scope-b@example.com")
    a_row = _make_row(db_session, user_id=a.id)
    _make_row(db_session, user_id=b.id)

    rows = crud_refresh_token.list_active_for_user(db_session, user_id=a.id)
    assert [r.id for r in rows] == [a_row.id]


def test_list_active_newest_first(db_session: Session, user_factory):
    user = user_factory(email="order@example.com")
    older = _make_row(db_session, user_id=user.id)
    newer = _make_row(db_session, user_id=user.id)
    # Force a deterministic created_at ordering (created_at can collide at the
    # same wall-clock instant; the helper also tie-breaks on id desc).
    older.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    newer.created_at = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(older)
    db_session.add(newer)
    db_session.commit()

    rows = crud_refresh_token.list_active_for_user(db_session, user_id=user.id)
    assert [r.id for r in rows] == [newer.id, older.id]
