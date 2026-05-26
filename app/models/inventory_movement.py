import enum
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product


class InventoryMovementReason(str, enum.Enum):
    """Why a stock movement happened.

    Stored as the lowercase value string in a plain ``String`` column (not a PG
    enum type) so adding a reason is a code-only change, no migration. ``RESTOCK``
    is reserved for the future purchase-order / restock workflow.
    """

    ORDER_PLACED = "order_placed"
    ORDER_CANCELLED = "order_cancelled"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    RESTOCK = "restock"


class InventoryMovement(BaseModel):
    """Append-only ledger row for every change to ``Product.stock``.

    Written in the same transaction as the stock mutation via
    ``crud_inventory.apply_delta`` so the movement and the new ``stock`` value
    land (or roll back) together. ``resulting_stock`` snapshots the stock *after*
    this delta so the ledger reconstructs history without replaying every row.

    ``actor_user_id`` / ``order_id`` are SET NULL on parent delete so the ledger
    survives the deletion of the user or order that caused the movement.
    """

    __tablename__ = "inventory_movements"

    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    delta: Mapped[int] = mapped_column(Integer)
    resulting_stock: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(32))
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(String(255))

    product: Mapped["Product"] = relationship("Product", back_populates="movements")

    def __repr__(self) -> str:
        return (
            f"<InventoryMovement product#{self.product_id} {self.delta:+d} "
            f"-> {self.resulting_stock} ({self.reason})>"
        )
