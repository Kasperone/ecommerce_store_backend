"""Unit tests for ``crud_customer_note`` (DB-backed, no HTTP)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.crud import customer_note as crud_customer_note
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _note(db: Session, *, user_id: int, author_id: int | None, body: str):
    note = crud_customer_note.create_for_customer(
        db, user_id=user_id, author_id=author_id, body=body
    )
    db.commit()
    return note


def test_create_for_customer_sets_fields(db_session: Session, user_factory):
    admin = user_factory(email="u-admin@example.com", role=UserRole.ADMIN)
    cust = user_factory(email="u-cust@example.com")
    note = _note(db_session, user_id=cust.id, author_id=admin.id, body="hi")
    assert note.id is not None
    assert note.user_id == cust.id
    assert note.author_id == admin.id
    assert note.body == "hi"


def test_create_for_customer_allows_null_author(db_session: Session, user_factory):
    cust = user_factory(email="u-cust2@example.com")
    note = _note(db_session, user_id=cust.id, author_id=None, body="system note")
    assert note.author_id is None
    assert note.author_name == "Removed admin"


def test_get_for_customer_newest_first_and_scoped(db_session: Session, user_factory):
    admin = user_factory(email="u-admin3@example.com", role=UserRole.ADMIN)
    a = user_factory(email="u-a@example.com")
    b = user_factory(email="u-b@example.com")
    for text in ("a1", "a2", "a3"):
        _note(db_session, user_id=a.id, author_id=admin.id, body=text)
    _note(db_session, user_id=b.id, author_id=admin.id, body="b1")

    a_notes = crud_customer_note.get_for_customer(db_session, user_id=a.id)
    assert [n.body for n in a_notes] == ["a3", "a2", "a1"]
    # author eager-loaded → author_name resolves without a fresh query
    assert a_notes[0].author_name  # non-empty
    b_notes = crud_customer_note.get_for_customer(db_session, user_id=b.id)
    assert [n.body for n in b_notes] == ["b1"]


def test_note_counts_for(db_session: Session, user_factory):
    admin = user_factory(email="u-admin4@example.com", role=UserRole.ADMIN)
    a = user_factory(email="u-cnt-a@example.com")
    b = user_factory(email="u-cnt-b@example.com")
    c = user_factory(email="u-cnt-c@example.com")  # no notes
    _note(db_session, user_id=a.id, author_id=admin.id, body="x")
    _note(db_session, user_id=a.id, author_id=admin.id, body="y")
    _note(db_session, user_id=b.id, author_id=admin.id, body="z")

    counts = crud_customer_note.note_counts_for(db_session, user_ids=[a.id, b.id, c.id])
    assert counts == {a.id: 2, b.id: 1}  # c absent (caller defaults to 0)
    assert crud_customer_note.note_counts_for(db_session, user_ids=[]) == {}
