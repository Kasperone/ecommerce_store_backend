"""Address-book CRUD — owner-scoped, with transactional default flags.

No commit anywhere (the route owns the transaction). Two invariants are enforced
here, in the same unit of work as the mutation, so they can never partially apply:

- **Default mutual-exclusion:** setting ``is_default_shipping=True`` on one row
  clears that flag on the user's *other* rows (same for billing). The clear and
  the set flush together, so a user always has at most one default per kind.
- **First-address auto-default:** the very first address a user creates becomes
  both the default-shipping and default-billing automatically, regardless of the
  flags the caller sent — a user with addresses always has a usable default.

Ownership is checked via ``get_owned`` (``id`` + ``user_id`` together) so a
cross-user or unknown id is indistinguishable: the route turns ``None`` into a
404 (never a 403), which doesn't leak whether the id exists.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.address import Address
from app.schemas.address import AddressCreate, AddressUpdate


class CRUDAddress:
    """Address rows (no commit; caller owns the txn)."""

    def get_owned(self, db: Session, *, id: int, user_id: int) -> Address | None:
        """Return the address only if it belongs to ``user_id`` (else ``None``)."""
        return db.execute(
            select(Address).where(Address.id == id, Address.user_id == user_id)
        ).scalar_one_or_none()

    def get_multi_for_user(self, db: Session, *, user_id: int) -> list[Address]:
        """The caller's addresses — default-shipping first, then newest."""
        return list(
            db.execute(
                select(Address)
                .where(Address.user_id == user_id)
                .order_by(
                    Address.is_default_shipping.desc(),
                    Address.created_at.desc(),
                    Address.id.desc(),
                )
            )
            .scalars()
            .all()
        )

    def _has_any(self, db: Session, *, user_id: int) -> bool:
        return (
            db.execute(select(Address.id).where(Address.user_id == user_id).limit(1)).first()
            is not None
        )

    def _clear_defaults(
        self,
        db: Session,
        *,
        user_id: int,
        shipping: bool,
        billing: bool,
        exclude_id: int | None = None,
    ) -> None:
        """Clear the requested default flag(s) on the user's other rows.

        ``exclude_id`` protects the row being promoted (so we don't clear the flag
        we're about to set on it). A no-op when neither flag is requested.
        """
        if not shipping and not billing:
            return
        values: dict[str, bool] = {}
        if shipping:
            values["is_default_shipping"] = False
        if billing:
            values["is_default_billing"] = False
        stmt = update(Address).where(Address.user_id == user_id)
        if exclude_id is not None:
            stmt = stmt.where(Address.id != exclude_id)
        db.execute(stmt.values(**values))

    def create(self, db: Session, *, user_id: int, obj_in: AddressCreate) -> Address:
        """Stage a new address for ``user_id`` (id populated via flush).

        The first address a user creates is forced to both defaults. Otherwise the
        caller's requested flags apply, clearing the matching flag on sibling rows
        first so exactly one default per kind survives.
        """
        is_first = not self._has_any(db, user_id=user_id)
        want_shipping = True if is_first else obj_in.is_default_shipping
        want_billing = True if is_first else obj_in.is_default_billing

        # Clear siblings' flags before inserting the new default (no exclude — the
        # new row has no id yet and isn't in the table).
        self._clear_defaults(db, user_id=user_id, shipping=want_shipping, billing=want_billing)

        address = Address(
            user_id=user_id,
            label=obj_in.label,
            recipient_name=obj_in.recipient_name,
            street=obj_in.street,
            city=obj_in.city,
            postal_code=obj_in.postal_code,
            country=obj_in.country,
            state=obj_in.state,
            phone=obj_in.phone,
            is_default_shipping=want_shipping,
            is_default_billing=want_billing,
        )
        db.add(address)
        db.flush()
        db.refresh(address)
        return address

    def update(self, db: Session, *, address: Address, obj_in: AddressUpdate) -> Address:
        """Apply a partial update. Promoting a default clears siblings first.

        Only fields explicitly sent change. Setting ``is_default_shipping`` /
        ``is_default_billing`` to ``True`` clears that flag on the user's other
        rows (mutual exclusion); setting it ``False`` just clears it on this row.
        """
        data = obj_in.model_dump(exclude_unset=True)

        promote_shipping = data.get("is_default_shipping") is True
        promote_billing = data.get("is_default_billing") is True
        if promote_shipping or promote_billing:
            self._clear_defaults(
                db,
                user_id=address.user_id,
                shipping=promote_shipping,
                billing=promote_billing,
                exclude_id=address.id,
            )

        for field, value in data.items():
            setattr(address, field, value)
        db.add(address)
        db.flush()
        db.refresh(address)
        return address

    def delete(self, db: Session, *, address: Address) -> None:
        db.delete(address)
        db.flush()


address = CRUDAddress()
