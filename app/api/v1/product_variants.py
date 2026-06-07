"""Admin product option/variant routes, nested under ``/api/v1/products``.

The matrix PUT (``/{product_id}/options``) is the one structural writer: it
takes the product's full desired option/variant state and the CRUD layer
reconciles it in one transaction (incl. the §0 stock transitions through
``crud_inventory.apply_delta``). The single-variant PATCH/DELETE cover the
cheap edits without resending the whole matrix.

All routes are admin-gated + audited; Omnibus rows are recorded per changed
variant price AFTER the audit write (same flush-ordering rule as products.py),
and the Meilisearch document is re-synced after commit (best-effort).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import price_history as crud_price_history
from app.crud import product as crud_product
from app.crud import product_variant as crud_product_variant
from app.crud.product_variant import VariantMatrixError, VariantSkuConflictError
from app.models.price_history import SUPPORTED_CURRENCIES
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.user import User
from app.schemas.product_variant import (
    ProductOptionResponse,
    ProductOptionsMatrixResponse,
    ProductOptionsMatrixUpdate,
    ProductVariantResponse,
    ProductVariantUpdate,
)
from app.services import search_index
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()


def _get_product_or_404(db: Session, product_id: int) -> Product:
    product = crud_product.get(db, id=product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


def _variant_response(variant: ProductVariant) -> ProductVariantResponse:
    return ProductVariantResponse.model_validate(variant)


def _record_variant_prices(db: Session, *, product: Product, variant: ProductVariant) -> None:
    """OMNIBUS: append a per-variant history row for any changed price.

    ``record_if_changed`` no-ops on an unchanged price and skips NULLs (an
    inheriting variant's trail is the product-grain rows). Must run AFTER the
    audit write — its flush would otherwise clear the ``get_history`` diff the
    audit writer reads (same ordering rule as products.py).
    """
    for currency in SUPPORTED_CURRENCIES:
        crud_price_history.record_if_changed(
            db,
            product=product,
            currency=currency,
            new_price=getattr(variant, f"price_{currency.lower()}"),
            variant_id=variant.id,
        )


@router.get("/{product_id}/variants", response_model=list[ProductVariantResponse])
async def list_product_variants(
    product_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Admin: all variants of a product, including inactive ones."""
    _get_product_or_404(db, product_id)
    variants = crud_product_variant.get_for_product(db, product_id=product_id)
    return [_variant_response(variant) for variant in variants]


@router.put("/{product_id}/options", response_model=ProductOptionsMatrixResponse)
async def update_product_options_matrix(
    product_id: int,
    matrix_in: ProductOptionsMatrixUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Admin: reconcile the product's full option/variant matrix in one transaction.

    Options/values are diffed by name (a rename is delete + create); variants
    match existing rows by ``id`` (new rows by combination). A product gaining
    its first variants has its product-grain stock zeroed out and each new
    variant's ``initial_stock`` seeded — both through the inventory ledger.
    Empty ``options`` + ``variants`` reverts the product to single-SKU.
    """
    product = _get_product_or_404(db, product_id)

    try:
        result = crud_product_variant.apply_matrix(
            db, product=product, matrix=matrix_in, actor_user_id=current_admin.id
        )
    except VariantSkuConflictError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except VariantMatrixError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    audit.record_event(
        action="variants_matrix_update",
        resource_type="product",
        resource_id=product.id,
        payload={
            "variants_matrix": {
                "options": [option.name for option in result.options],
                "created_variant_ids": result.created_ids,
                "updated_variant_ids": result.updated_ids,
                "deleted_variant_ids": result.deleted_ids,
            }
        },
    )
    # OMNIBUS rows AFTER the audit write (flush-ordering rule — see products.py).
    for variant in result.variants:
        _record_variant_prices(db, product=product, variant=variant)
    db.commit()
    db.refresh(product)
    # Best-effort: variant prices/stock feed the search document (min price,
    # has_variants, option_values).
    search_index.sync_product(db, product.id)
    return ProductOptionsMatrixResponse(
        options=[ProductOptionResponse.model_validate(option) for option in product.options],
        variants=[
            _variant_response(variant)
            for variant in crud_product_variant.get_for_product(db, product_id=product.id)
        ],
    )


@router.patch("/{product_id}/variants/{variant_id}", response_model=ProductVariantResponse)
async def update_product_variant(
    product_id: int,
    variant_id: int,
    variant_in: ProductVariantUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Admin: edit one variant (price/SKU/image/active/position).

    Stock is owned by the inventory endpoints; combination changes go through
    the matrix PUT. 400 on a taken SKU, 422 on an image outside the product's
    gallery — both mirroring the product routes.
    """
    product = _get_product_or_404(db, product_id)
    variant = crud_product_variant.get(db, product_id=product_id, variant_id=variant_id)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")

    if variant_in.sku and variant_in.sku != variant.sku:
        conflict = crud_product_variant.get_by_sku(db, sku=variant_in.sku)
        if conflict is not None and conflict.id != variant.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Variant with this SKU already exists",
            )
    if variant_in.image_id is not None and variant_in.image_id not in {
        image.id for image in product.images
    }:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Image is not in this product's gallery",
        )

    variant = crud_product_variant.update_variant(db, variant=variant, obj_in=variant_in)
    audit.record_update(resource_type="product_variant", obj=variant)
    # OMNIBUS rows AFTER the audit write (flush-ordering rule — see products.py).
    _record_variant_prices(db, product=product, variant=variant)
    db.commit()
    db.refresh(variant)
    search_index.sync_product(db, product.id)
    return _variant_response(variant)


@router.delete("/{product_id}/variants/{variant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product_variant(
    product_id: int,
    variant_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Admin: delete one variant.

    409 when it is the last active variant of a variant product — deleting the
    whole variant dimension is an explicit act (the matrix PUT with empty
    arrays), not a side effect of trimming rows. The variant's stock levels
    are zeroed through the inventory ledger first so it reconciles.
    """
    product = _get_product_or_404(db, product_id)
    variant = crud_product_variant.get(db, product_id=product_id, variant_id=variant_id)
    if variant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")

    if variant.is_active and not any(
        other.is_active for other in product.variants if other.id != variant.id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot delete the last active variant; revert the product to "
                "single-SKU via the options matrix instead"
            ),
        )

    # Snapshot before delete — the writer needs ``obj`` readable.
    audit.record_delete(resource_type="product_variant", obj=variant)
    crud_product_variant.delete_variant(
        db, product=product, variant=variant, actor_user_id=current_admin.id
    )
    db.commit()
    search_index.sync_product(db, product.id)
    return None
