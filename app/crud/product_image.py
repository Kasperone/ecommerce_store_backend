"""CRUD operations for ProductImage.

Used by PR1's upload endpoints. Mutating helpers do not commit — the route
handler owns the transaction boundary so any audit row staged by the writer
lands (or rolls back) with the data change.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.product_image import ProductImage


class _NoopSchema(BaseModel):
    """Placeholder schema — image rows are constructed by the upload service,
    not by request bodies. ``CRUDBase`` only needs the type parameters."""


class CRUDProductImage(CRUDBase[ProductImage, _NoopSchema, _NoopSchema]):
    """CRUD operations for ProductImage."""

    def list_for_product(self, db: Session, *, product_id: int) -> list[ProductImage]:
        return (
            db.query(ProductImage)
            .filter(ProductImage.product_id == product_id)
            .order_by(ProductImage.sort_order, ProductImage.id)
            .all()
        )

    def set_primary(self, db: Session, *, product_id: int, image_id: int) -> ProductImage | None:
        """Promote one image to primary, demoting every sibling.

        Clears ``is_primary`` on every other row for the same product before
        setting it on the target. Done in one transaction to avoid tripping
        the ``uq_product_images_primary_per_product`` partial unique index.
        Does not commit — caller owns the transaction.
        """
        target = (
            db.query(ProductImage)
            .filter(ProductImage.product_id == product_id, ProductImage.id == image_id)
            .first()
        )
        if target is None:
            return None

        # Demote siblings first so the unique index never sees two primaries.
        db.query(ProductImage).filter(
            ProductImage.product_id == product_id,
            ProductImage.id != image_id,
            ProductImage.is_primary.is_(True),
        ).update({ProductImage.is_primary: False}, synchronize_session=False)

        target.is_primary = True
        db.flush()
        return target


product_image = CRUDProductImage(ProductImage)
