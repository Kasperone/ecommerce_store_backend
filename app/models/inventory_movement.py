import enum
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.warehouse import Warehouse


class InventoryMovementReason(str, enum.Enum):
    """Why a stock movement happened.

    Stored as the lowercase value string in a plain ``String`` column (not a PG
    enum type) so adding a reason is a code-only change, no migration. A transfer
    between warehouses writes a paired ``TRANSFER_OUT`` (source) + ``TRANSFER_IN``
    (destination). ``RESTOCK`` records incoming stock (purchase-order receipts).
    """

    ORDER_PLACED = "order_placed"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REFUNDED = "order_refunded"
    # Admin edited a pending order's line items: old lines are released and the
    # new line set re-acquired, both under this reason (net delta per SKU).
    ORDER_EDITED = "order_edited"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    RESTOCK = "restock"
    TRANSFER_OUT = "transfer_out"
    TRANSFER_IN = "transfer_in"


class InventoryMovement(BaseModel):
    """Append-only ledger row for every change to ``Product.stock``.

    Written in the same transaction as the stock mutation via
    ``crud_inventory.apply_delta`` so the movement and the new stock land (or roll
    back) together. ``warehouse_id`` scopes the movement to one location;
    ``resulting_stock`` snapshots that warehouse's level *after* this delta so the
    ledger reconstructs per-warehouse history without replaying every row.

    ``actor_user_id`` / ``order_id`` / ``warehouse_id`` are SET NULL on parent
    delete so the ledger survives the deletion of the user, order, or warehouse
    that caused the movement.
    """

    __tablename__ = "inventory_movements"

    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    warehouse_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("warehouses.id", ondelete="SET NULL"), index=True
    )
    # SET NULL — the ledger survives variant deletion, same posture as the
    # other parent FKs above/below.
    variant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("product_variants.id", ondelete="SET NULL"), index=True
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
    warehouse: Mapped["Warehouse | None"] = relationship("Warehouse")

    def __repr__(self) -> str:
        return (
            f"<InventoryMovement product#{self.product_id} {self.delta:+d} "
            f"-> {self.resulting_stock} ({self.reason})>"
        )
