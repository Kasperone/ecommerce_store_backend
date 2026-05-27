"""CRUD for admin customer notes.

Mutating methods stage on the session and flush (so ``id`` is readable for the
audit row) but never commit — the route owns the transaction boundary, same
contract as the rest of the CRUD layer.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.crud.base import CRUDBase
from app.models.customer_note import CustomerNote
from app.schemas.customer_note import CustomerNoteCreate, CustomerNoteUpdate


class CRUDCustomerNote(CRUDBase[CustomerNote, CustomerNoteCreate, CustomerNoteUpdate]):
    def get_for_customer(self, db: Session, *, user_id: int) -> list[CustomerNote]:
        """All notes about a customer, newest first, with the author eager-loaded.

        ``author`` is selectinload-ed so ``author_name`` doesn't trigger an N+1
        per note during response serialization.
        """
        return (
            db.query(CustomerNote)
            .options(selectinload(CustomerNote.author))
            .filter(CustomerNote.user_id == user_id)
            .order_by(CustomerNote.created_at.desc(), CustomerNote.id.desc())
            .all()
        )

    def create_for_customer(
        self, db: Session, *, user_id: int, author_id: int | None, body: str
    ) -> CustomerNote:
        """Stage a new note. Flushes for the id; caller commits."""
        note = CustomerNote(user_id=user_id, author_id=author_id, body=body)
        db.add(note)
        db.flush()
        db.refresh(note)
        return note

    def note_counts_for(self, db: Session, *, user_ids: list[int]) -> dict[int, int]:
        """Map of ``{user_id: note_count}`` for the given customers in one query.

        Avoids an N+1 on the customer list. Users with no notes are absent from
        the map (the caller defaults to 0).
        """
        if not user_ids:
            return {}
        rows = (
            db.query(CustomerNote.user_id, func.count(CustomerNote.id))
            .filter(CustomerNote.user_id.in_(user_ids))
            .group_by(CustomerNote.user_id)
            .all()
        )
        return {user_id: count for user_id, count in rows}


customer_note = CRUDCustomerNote(CustomerNote)
