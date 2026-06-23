"""Product option/variant CRUD — the matrix reconcile + single-variant ops.

``apply_matrix`` is the one writer for a product's option/variant *structure*:
it diffs the desired state (the matrix PUT body) against the DB in one
transaction — options/values matched by name (a rename is delete + create),
variants matched by ``id`` (new rows by ``option_signature``) — and runs the
§0 stock-transition rules through ``crud_inventory.apply_delta`` so the ledger
stays append-only and ``product.stock`` lands at the new sum:

* product gains its first variants → every product-grain level is zeroed out
  (``manual_adjustment`` / "migrated to variants"), then each new variant's
  ``initial_stock`` is seeded (``restock`` / "variant initial stock");
* a variant is removed → its levels are zeroed first so the ledger reconciles.

Existing variants' stock is NOT writable here — the inventory endpoints own it
(mirroring the ProductUpdate "stock not updatable" rule).

No commits — the route owns the transaction boundary (same contract as
``crud_inventory.apply_delta`` and ``AuditWriter``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.money import to_money
from app.crud.inventory import inventory as crud_inventory
from app.models.inventory_movement import InventoryMovementReason
from app.models.product import Product
from app.models.product_variant import ProductOption, ProductOptionValue, ProductVariant
from app.models.warehouse import StockLevel
from app.schemas.product_variant import ProductOptionsMatrixUpdate, ProductVariantUpdate

MAX_OPTIONS = 3
MAX_VARIANTS = 100


class VariantMatrixError(Exception):
    """A structurally invalid matrix body (cross-entity rule) — maps to 422."""


class VariantSkuConflictError(Exception):
    """A variant SKU is already taken — maps to 400 (same as products)."""


@dataclass
class MatrixResult:
    """What the reconcile did — drives the route's audit payload + Omnibus loop."""

    options: list[ProductOption] = field(default_factory=list)
    variants: list[ProductVariant] = field(default_factory=list)
    created_ids: list[int] = field(default_factory=list)
    updated_ids: list[int] = field(default_factory=list)
    deleted_ids: list[int] = field(default_factory=list)


def _signature(value_ids: list[int]) -> str:
    """Sorted option_value ids joined with "-" — the DB-unique combination key."""
    return "-".join(str(value_id) for value_id in sorted(value_ids))


def _titles(values: list[ProductOptionValue]) -> tuple[str, str | None]:
    """Denormalized variant titles ("Red / XL") from values in option order.

    ``title_pl`` is set only when at least one value carries a PL translation
    (each missing one falls back to EN), else NULL — mirrors the storefront's
    EN-fallback convention.
    """
    ordered = sorted(values, key=lambda value: value.option.position)
    title = " / ".join(value.value for value in ordered)
    if any(value.value_pl for value in ordered):
        title_pl = " / ".join(value.value_pl or value.value for value in ordered)
    else:
        title_pl = None
    return title, title_pl


class CRUDProductVariant:
    """Option/variant structure ops (no commit; caller owns the txn)."""

    def get(self, db: Session, *, product_id: int, variant_id: int) -> ProductVariant | None:
        """One variant scoped to its product (admin routes 404 on a mismatch)."""
        return (
            db.query(ProductVariant)
            .filter(ProductVariant.id == variant_id, ProductVariant.product_id == product_id)
            .first()
        )

    def get_for_product(self, db: Session, *, product_id: int) -> list[ProductVariant]:
        """All variants of a product (including inactive), by position."""
        return (
            db.query(ProductVariant)
            .filter(ProductVariant.product_id == product_id)
            .order_by(ProductVariant.position.asc(), ProductVariant.id.asc())
            .all()
        )

    def get_by_sku(self, db: Session, *, sku: str) -> ProductVariant | None:
        """Variant by its (globally unique) SKU."""
        return db.query(ProductVariant).filter(ProductVariant.sku == sku).first()

    # ------------------------------------------------------------------
    # The matrix reconcile
    # ------------------------------------------------------------------

    def apply_matrix(
        self,
        db: Session,
        *,
        product: Product,
        matrix: ProductOptionsMatrixUpdate,
        actor_user_id: int | None = None,
    ) -> MatrixResult:
        """Reconcile the product's options + variants to the desired state.

        See the module docstring for the diffing + stock-transition rules.
        Raises ``VariantMatrixError`` (→422) on cross-entity problems the
        Pydantic schema can't see, ``VariantSkuConflictError`` (→400) on a
        taken SKU. Caller commits.
        """
        # Defense-in-depth re-check of the schema caps (the plan's "CRUD also
        # rejects a 4th option with a clean 422").
        if len(matrix.options) > MAX_OPTIONS:
            raise VariantMatrixError(f"A product may define at most {MAX_OPTIONS} options")
        if len(matrix.variants) > MAX_VARIANTS:
            raise VariantMatrixError(f"A product may define at most {MAX_VARIANTS} variants")

        had_variants = bool(product.variants)

        options = self._reconcile_options(db, product=product, matrix=matrix)
        resolved = self._resolve_variant_values(matrix=matrix, options=options)
        result = self._reconcile_variants(
            db,
            product=product,
            matrix=matrix,
            resolved_values=resolved,
            had_variants=had_variants,
            actor_user_id=actor_user_id,
        )
        result.options = options
        return result

    def _reconcile_options(
        self, db: Session, *, product: Product, matrix: ProductOptionsMatrixUpdate
    ) -> list[ProductOption]:
        """Diff options + values by name; a rename is delete + create.

        Deletes are flushed before creates so a freed (product, name) pair can
        be reused in the same PUT; position swaps among kept options ride on
        the DEFERRABLE position constraint.
        """
        existing_by_name = {option.name: option for option in product.options}
        desired_names = {option.name for option in matrix.options}

        removed = [option for name, option in existing_by_name.items() if name not in desired_names]
        for option in removed:
            db.delete(option)
        if removed:
            db.flush()
            # Drop the deleted rows from the in-memory collection — a later
            # ``db.add(product)`` (inside ``apply_delta``) cascades save-update
            # over ``product.options`` and would trip on a deleted instance.
            db.expire(product, ["options"])

        reconciled: list[ProductOption] = []
        for option_in in matrix.options:
            option = existing_by_name.get(option_in.name)
            if option is None:
                option = ProductOption(
                    product_id=product.id,
                    name=option_in.name,
                    name_pl=option_in.name_pl,
                    position=option_in.position,
                )
                db.add(option)
                for value_in in option_in.values:
                    option.values.append(
                        ProductOptionValue(
                            value=value_in.value,
                            value_pl=value_in.value_pl,
                            position=value_in.position,
                        )
                    )
            else:
                option.name_pl = option_in.name_pl
                option.position = option_in.position
                values_by_name = {value.value: value for value in option.values}
                desired_values = {value_in.value for value_in in option_in.values}
                for value in list(option.values):
                    if value.value not in desired_values:
                        option.values.remove(value)  # delete-orphan cascade
                db.flush()
                for value_in in option_in.values:
                    value = values_by_name.get(value_in.value)
                    if value is None:
                        option.values.append(
                            ProductOptionValue(
                                value=value_in.value,
                                value_pl=value_in.value_pl,
                                position=value_in.position,
                            )
                        )
                    else:
                        value.value_pl = value_in.value_pl
                        value.position = value_in.position
            reconciled.append(option)
        # Flush so freshly created options/values carry ids (signatures below).
        db.flush()
        return reconciled

    def _resolve_variant_values(
        self, *, matrix: ProductOptionsMatrixUpdate, options: list[ProductOption]
    ) -> list[list[ProductOptionValue]]:
        """Resolve each variant input's value strings to ORM rows.

        ``option_values[i]`` pairs with ``options[i]`` (the body's option
        order — the schema already guarantees the lengths match). Unknown
        values and duplicate combinations are matrix errors.
        """
        values_by_option = [{value.value: value for value in option.values} for option in options]
        resolved: list[list[ProductOptionValue]] = []
        seen_signatures: set[str] = set()
        for variant_in in matrix.variants:
            values: list[ProductOptionValue] = []
            for index, value_name in enumerate(variant_in.option_values):
                value = values_by_option[index].get(value_name)
                if value is None:
                    raise VariantMatrixError(
                        f"Unknown value {value_name!r} for option {options[index].name!r}"
                    )
                values.append(value)
            signature = _signature([value.id for value in values])
            if signature in seen_signatures:
                raise VariantMatrixError(
                    "Duplicate variant combination: " + " / ".join(variant_in.option_values)
                )
            seen_signatures.add(signature)
            resolved.append(values)
        return resolved

    def _reconcile_variants(
        self,
        db: Session,
        *,
        product: Product,
        matrix: ProductOptionsMatrixUpdate,
        resolved_values: list[list[ProductOptionValue]],
        had_variants: bool,
        actor_user_id: int | None,
    ) -> MatrixResult:
        existing_by_id = {variant.id: variant for variant in product.variants}
        existing_by_signature = {variant.option_signature: variant for variant in product.variants}

        # SKU sanity — duplicates within the body, then collisions in the DB
        # (excluding the rows being updated). The DB unique constraint is the
        # backstop; this gives the clean 400.
        skus = [variant_in.sku for variant_in in matrix.variants if variant_in.sku]
        if len(skus) != len(set(skus)):
            raise VariantMatrixError("Duplicate SKU within the variant set")

        product_image_ids = {image.id for image in product.images}

        matched_ids: set[int] = set()
        kept: list[tuple[int, ProductVariant | None]] = []  # (input index, existing | None)
        for index, variant_in in enumerate(matrix.variants):
            existing: ProductVariant | None = None
            if variant_in.id is not None:
                existing = existing_by_id.get(variant_in.id)
                if existing is None:
                    raise VariantMatrixError(
                        f"Variant {variant_in.id} does not belong to this product"
                    )
            else:
                signature = _signature([value.id for value in resolved_values[index]])
                existing = existing_by_signature.get(signature)
            if existing is not None:
                if existing.id in matched_ids:
                    raise VariantMatrixError(
                        f"Variant {existing.id} appears more than once in the variant set"
                    )
                matched_ids.add(existing.id)
            if variant_in.image_id is not None and variant_in.image_id not in product_image_ids:
                raise VariantMatrixError(
                    f"Image {variant_in.image_id} is not in this product's gallery"
                )
            kept.append((index, existing))

        result = MatrixResult()

        # Deletions first: zero each removed variant's levels through the
        # chokepoint so the ledger reconciles, then delete the row.
        for variant in product.variants:
            if variant.id not in matched_ids:
                self._zero_variant_levels(
                    db,
                    product=product,
                    variant=variant,
                    note="variant removed",
                    actor_user_id=actor_user_id,
                )
                result.deleted_ids.append(variant.id)
                db.delete(variant)
        if result.deleted_ids:
            db.flush()
            # Drop the deleted rows from the in-memory collection — a later
            # ``db.add(product)`` (inside ``apply_delta``) cascades save-update
            # over ``product.variants`` and would trip on a deleted instance.
            db.expire(product, ["variants"])

        # §0 transition: the product gains its first variants → zero out every
        # product-grain level (synthesised default included) via apply_delta.
        if not had_variants and matrix.variants:
            for warehouse, quantity in crud_inventory.levels_for_product(db, product=product):
                if quantity != 0:
                    crud_inventory.apply_delta(
                        db,
                        product=product,
                        delta=-quantity,
                        reason=InventoryMovementReason.MANUAL_ADJUSTMENT,
                        warehouse_id=warehouse.id,
                        actor_user_id=actor_user_id,
                        note="migrated to variants",
                        allow_negative=True,
                    )

        # Upserts.
        for index, existing in kept:
            variant_in = matrix.variants[index]
            values = resolved_values[index]
            signature = _signature([value.id for value in values])
            title, title_pl = _titles(values)

            if variant_in.sku:
                conflict = self.get_by_sku(db, sku=variant_in.sku)
                if conflict is not None and (existing is None or conflict.id != existing.id):
                    raise VariantSkuConflictError(
                        f"Variant SKU {variant_in.sku!r} is already in use"
                    )

            if existing is None:
                variant = ProductVariant(
                    product_id=product.id,
                    title=title,
                    title_pl=title_pl,
                    sku=variant_in.sku,
                    price_usd=to_money(variant_in.price_usd),
                    price_pln=to_money(variant_in.price_pln),
                    price_eur=to_money(variant_in.price_eur),
                    stock=0,
                    image_id=variant_in.image_id,
                    position=variant_in.position,
                    is_active=variant_in.is_active,
                    option_signature=signature,
                )
                variant.option_values = list(values)
                db.add(variant)
                db.flush()
                if variant_in.initial_stock:
                    crud_inventory.apply_delta(
                        db,
                        product=product,
                        delta=variant_in.initial_stock,
                        reason=InventoryMovementReason.RESTOCK,
                        variant=variant,
                        actor_user_id=actor_user_id,
                        note="variant initial stock",
                    )
                result.created_ids.append(variant.id)
                result.variants.append(variant)
            else:
                # Reload the join collection from the DB — value deletions above
                # CASCADE join rows at the DB level, and a stale in-memory
                # collection would make the replacement diff issue deletes for
                # rows that are already gone.
                db.expire(existing, ["option_values"])
                existing.title = title
                existing.title_pl = title_pl
                existing.sku = variant_in.sku
                existing.price_usd = to_money(variant_in.price_usd)
                existing.price_pln = to_money(variant_in.price_pln)
                existing.price_eur = to_money(variant_in.price_eur)
                existing.image_id = variant_in.image_id
                existing.position = variant_in.position
                existing.is_active = variant_in.is_active
                existing.option_signature = signature
                existing.option_values = list(values)
                # initial_stock is intentionally ignored for existing variants —
                # the inventory endpoints own post-create stock.
                result.updated_ids.append(existing.id)
                result.variants.append(existing)

        db.flush()
        return result

    # ------------------------------------------------------------------
    # Single-variant ops
    # ------------------------------------------------------------------

    def update_variant(
        self, db: Session, *, variant: ProductVariant, obj_in: ProductVariantUpdate
    ) -> ProductVariant:
        """Apply a single-variant PATCH (price/sku/image/active/position).

        No flush — the audit writer reads ``get_history`` between this and the
        route's commit (same contract as ``CRUDBase.update``). SKU/image
        validation lives in the route (it has the product loaded).
        """
        update_data = obj_in.model_dump(exclude_unset=True)
        for field_name, value in update_data.items():
            setattr(variant, field_name, value)
        db.add(variant)
        return variant

    def delete_variant(
        self,
        db: Session,
        *,
        product: Product,
        variant: ProductVariant,
        actor_user_id: int | None = None,
    ) -> None:
        """Zero a variant's levels through the chokepoint, then delete the row.

        The ledger rows survive (movements' ``variant_id`` is SET NULL on
        delete). Caller commits.
        """
        self._zero_variant_levels(
            db,
            product=product,
            variant=variant,
            note="variant deleted",
            actor_user_id=actor_user_id,
        )
        db.delete(variant)

    def _zero_variant_levels(
        self,
        db: Session,
        *,
        product: Product,
        variant: ProductVariant,
        note: str,
        actor_user_id: int | None,
    ) -> None:
        """Bring every materialised level of ``variant`` to 0 via ``apply_delta``."""
        # Unload the join collection: when the variant's option values were just
        # deleted (option removal earlier in the same reconcile), the
        # ``db.add(variant)`` inside ``apply_delta`` would cascade save-update
        # over the stale deleted instances. The DB-level FK CASCADE owns the
        # join-row cleanup.
        db.expire(variant, ["option_values"])
        levels = (
            db.query(StockLevel)
            .filter(
                StockLevel.product_id == product.id,
                StockLevel.variant_id == variant.id,
                StockLevel.quantity != 0,
            )
            .all()
        )
        for level in levels:
            crud_inventory.apply_delta(
                db,
                product=product,
                delta=-level.quantity,
                reason=InventoryMovementReason.MANUAL_ADJUSTMENT,
                warehouse_id=level.warehouse_id,
                variant=variant,
                actor_user_id=actor_user_id,
                note=note,
                allow_negative=True,
            )


product_variant = CRUDProductVariant()
