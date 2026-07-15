"""Admin product catalog tools — CSV export + import + bulk edit.

Mounted at ``/api/v1/admin/products``. All routes are admin-gated. The export is a
read-only GET (no audit row, no persistence) that streams the catalog as RFC 4180
CSV (UTF-8 with BOM), inheriting the admin product-list filters (category / active
/ search) so the file matches the on-screen filtered view. One row per product;
variants are flagged via ``has_variants`` (not expanded to rows in v1). Free-text
cells pass through the formula-injection guard (``csv_safe``).

The import (``POST /import``) round-trips that same 31-column schema. ``dry_run``
(default true) validates every row and writes NOTHING, returning a per-row report;
``dry_run=false`` applies **all-or-nothing in one transaction** (any row error →
rollback, nothing applied), reusing the product create/update path — slug/SKU
uniqueness, OMNIBUS price-history, tag get-or-create — and ``apply_delta`` for
stock (never ``Product.stock`` directly; variant products reject stock changes).
On success the touched products are re-synced to Meili (incremental). Dry-run and
apply share ONE validator (``_process_rows`` with ``apply`` toggled).

Bulk edit (``PATCH /bulk``) applies a narrow ``set`` — price (per currency),
``is_active``, ``low_stock_threshold`` — to a list of products in one
all-or-nothing transaction. **Stock is never settable** here (it moves only
through the inventory ledger; the ``set`` schema is ``extra="forbid"``). Any
unknown id fails the whole request with 404. Price changes record OMNIBUS
price-history (shared helper); touched products are re-synced to Meili.
"""

from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.v1.products import _reconcile_product_tags
from app.core.capabilities import CATALOG_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.core.money import as_float
from app.crud import category as crud_category
from app.crud import collection as crud_collection
from app.crud import inventory as crud_inventory
from app.crud import price_history as crud_price_history
from app.crud import product as crud_product
from app.models.inventory_movement import InventoryMovementReason
from app.models.price_history import SUPPORTED_CURRENCIES
from app.models.product import Product
from app.models.user import User
from app.schemas.product import (
    ProductBulkUpdate,
    ProductBulkUpdateResult,
    ProductCreate,
    ProductUpdate,
)
from app.services import product_export, product_import, search_index
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Every write route in this router is a catalog mutation.
_catalog_write = Depends(require_capability(CATALOG_WRITE))

SUPPORTED_CURRENCIES_LC = tuple(c.lower() for c in SUPPORTED_CURRENCIES)


def _export_filename(category_id: int | None, is_active: bool | None, search: str | None) -> str:
    """``products`` base, suffixed with active-filter / category / search markers;
    falls back to ``products-export-{today}.csv`` when nothing is filtered."""
    parts = ["products", "export"]
    if is_active is True:
        parts.append("active")
    elif is_active is False:
        parts.append("inactive")
    if category_id is not None:
        parts.append(f"cat{category_id}")
    if search:
        parts.append("search")
    if len(parts) == 2:
        parts.append(date.today().strftime("%Y%m%d"))
    return "-".join(parts) + ".csv"


@router.get(
    "/export",
    responses={200: {"content": {"text/csv": {}}, "description": "Product CSV export"}},
)
async def export_products(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    category_id: int | None = Query(None, description="Filter by category ID"),
    is_active: bool | None = Query(None, description="Filter by active status (omit = all)"),
    search: str | None = Query(None, max_length=100, description="Search in name/description"),
):
    """Download the product catalog as CSV (admin), inheriting the list filters.

    One row per product; ``tags`` / ``collection_slugs`` packed into ``"; "``-joined
    columns; variant products flagged with ``has_variants`` (not expanded — v1
    export only). RFC 4180, UTF-8 with BOM. Generated on demand — no persistence,
    no audit row (read-only GET). This is export-only; import + bulk-edit ship in
    follow-up PRs (CSV-BE2 / CSV-BE3).
    """
    products = crud_product.get_for_export(
        db, category_id=category_id, is_active=is_active, search=search
    )

    rows = [
        product_export.ProductExportRow(
            id=product.id,
            slug=product.slug,
            sku=product.sku,
            name=product.name,
            name_pl=product.name_pl,
            description=product.description,
            description_pl=product.description_pl,
            price_usd=as_float(product.price_usd),
            price_pln=as_float(product.price_pln),
            price_eur=as_float(product.price_eur),
            stock=product.stock,
            low_stock_threshold=product.low_stock_threshold,
            allow_backorder=product.allow_backorder,
            is_active=product.is_active,
            is_featured=product.is_featured,
            has_variants=bool(product.variants),
            category_slug=product.category.slug if product.category else None,
            brand=product.brand,
            gtin=product.gtin,
            mpn=product.mpn,
            condition=product.condition,
            weight_kg=product.weight_kg,
            length_cm=product.length_cm,
            width_cm=product.width_cm,
            height_cm=product.height_cm,
            meta_title=product.meta_title,
            meta_description=product.meta_description,
            meta_title_pl=product.meta_title_pl,
            meta_description_pl=product.meta_description_pl,
            tags=[tag.name for tag in product.tags],
            collection_slugs=[collection.slug for collection in product.collections],
        )
        for product in products
    ]

    csv_bytes = product_export.build_products_csv(rows)
    filename = _export_filename(category_id, is_active, search)
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


def _resolve_target(db: Session, parsed: product_import.ParsedRow) -> Product | None:
    """Resolve the upsert target for a row: ``id`` → ``slug`` → ``sku`` (first hit)."""
    if "id" in parsed.values:
        return crud_product.get(db, id=parsed.values["id"])
    slug = parsed.values.get("slug")
    if slug:
        existing = crud_product.get_by_slug(db, slug=str(slug))
        if existing:
            return existing
    sku = parsed.values.get("sku")
    if sku:
        return crud_product.get_by_sku(db, sku=str(sku))
    return None


def _model_fields(parsed: product_import.ParsedRow) -> dict[str, object]:
    """Pull the product column values present on the row (excludes id/stock/refs)."""
    skip = {"id", "stock", "category_slug", "has_variants"}
    return {k: v for k, v in parsed.values.items() if k not in skip}


def _process_rows(
    db: Session,
    parsed_rows: list[product_import.ParsedRow],
    *,
    apply: bool,
    audit: AuditWriter,
    actor_id: int,
) -> tuple[list[product_import.ReportRow], list[int]]:
    """The single validator+applier shared by dry-run and apply.

    Validates (and, when ``apply``, mutates) every row. Returns the per-row
    report plus the list of touched product ids (for Meili re-sync on apply).
    Stages writes only — the route owns the transaction boundary (commit on a
    clean apply, rollback on any error or on dry-run).
    """
    report: list[product_import.ReportRow] = []
    touched: list[int] = []

    for row in parsed_rows:
        errors = list(row.errors)
        action: product_import.Action = "error"
        product_id: int | None = None

        # Resolve the upsert target up front (id/slug/sku). An ``id`` that points
        # at nothing is a hard row error.
        target: Product | None = None
        if "id" in row.values:
            target = crud_product.get(db, id=row.values["id"])
            if target is None:
                errors.append(f"No product with id {row.values['id']}")
        else:
            target = _resolve_target(db, row)
        if target is not None:
            product_id = target.id

        # Resolve category_slug → id (unknown slug is a row error).
        category_id: int | None = None
        if "category_slug" in row.values:
            cat = crud_category.get_by_slug(db, slug=str(row.values["category_slug"]))
            if cat is None:
                errors.append(f"Unknown category_slug {row.values['category_slug']!r}")
            else:
                category_id = cat.id

        # Resolve collection_slugs → ids (any unknown slug is a row error).
        collection_ids: list[int] | None = None
        if row.collection_slugs is not None:
            collection_ids = []
            for slug in row.collection_slugs:
                col = crud_collection.get_by_slug(db, slug=slug)
                if col is None:
                    errors.append(f"Unknown collection_slug {slug!r}")
                else:
                    collection_ids.append(col.id)

        # Variant products: reject any stock change (variant stock lives at
        # variant grain). ``bool(product.variants)`` is the inventory-grain check.
        is_variant_product = target is not None and bool(target.variants)
        wants_stock_change = "stock" in row.values
        if is_variant_product and wants_stock_change:
            errors.append(
                "Cannot set stock on a variant product via CSV — stock lives at variant grain"
            )

        # New product (no target): requires name + at least one price.
        is_create = target is None
        if is_create:
            if not row.values.get("name"):
                errors.append("New product requires a name")
            if not any(row.values.get(f"price_{c}") is not None for c in SUPPORTED_CURRENCIES_LC):
                errors.append("New product requires at least one price (USD, PLN, or EUR)")
            if not row.values.get("slug"):
                errors.append("New product requires a slug")

        if errors:
            report.append(
                product_import.ReportRow(
                    row_number=row.row_number,
                    action="error",
                    product_id=product_id,
                    errors=errors,
                )
            )
            continue

        action = "create" if is_create else "update"

        if not apply:
            report.append(
                product_import.ReportRow(
                    row_number=row.row_number, action=action, product_id=product_id, errors=[]
                )
            )
            continue

        # ---- apply path -----------------------------------------------------
        try:
            if is_create or target is None:
                target = _apply_create(db, row, category_id, audit)
            else:
                _apply_update(db, target, row, category_id, audit)
            product_id = target.id

            if "tags" in row.present and row.tags is not None:
                _reconcile_product_tags(db, target, row.tags)
                audit.record_event(
                    action="update",
                    resource_type="product_tag_link",
                    resource_id=target.id,
                    payload={"tags": [t.name for t in target.tags]},
                )

            if collection_ids is not None:
                crud_collection.set_product_collections(
                    db, product_id=target.id, collection_ids=collection_ids
                )

            if wants_stock_change and not is_variant_product:
                _apply_stock(db, target, int(row.values["stock"]), actor_id)  # type: ignore[arg-type]
        except ValueError as exc:
            # Route-layer uniqueness (slug/SKU) + apply_delta guards raise
            # ValueError — surface as a row error, not a 500. All-or-nothing means
            # the route rolls the whole batch back when any row errors.
            report.append(
                product_import.ReportRow(
                    row_number=row.row_number,
                    action="error",
                    product_id=product_id,
                    errors=[str(exc)],
                )
            )
            continue

        touched.append(target.id)
        report.append(
            product_import.ReportRow(
                row_number=row.row_number, action=action, product_id=product_id, errors=[]
            )
        )

    return report, touched


def _record_price_history(db: Session, product: Product) -> None:
    for currency in SUPPORTED_CURRENCIES:
        crud_price_history.record_if_changed(
            db,
            product=product,
            currency=currency,
            new_price=getattr(product, f"price_{currency.lower()}"),
        )


def _apply_create(
    db: Session,
    row: product_import.ParsedRow,
    category_id: int | None,
    audit: AuditWriter,
) -> Product:
    """Create a product reusing the route-layer uniqueness checks + OMNIBUS history.

    Raises ``ValueError`` on a slug/SKU collision (the importer surfaces it as a
    row error). Mirrors ``products.create_product`` minus the per-route commit.
    """
    fields = _model_fields(row)
    if category_id is not None:
        fields["category_id"] = category_id
    create_in = ProductCreate.model_validate(fields)

    if crud_product.get_by_slug(db, slug=create_in.slug):
        raise ValueError(f"slug {create_in.slug!r} already exists")
    if create_in.sku and crud_product.get_by_sku(db, sku=create_in.sku):
        raise ValueError(f"sku {create_in.sku!r} already exists")

    product = crud_product.create(db, obj_in=create_in)
    audit.record_create(resource_type="product", obj=product)
    _record_price_history(db, product)
    return product


def _apply_update(
    db: Session,
    product: Product,
    row: product_import.ParsedRow,
    category_id: int | None,
    audit: AuditWriter,
) -> None:
    """Update a product reusing the route-layer uniqueness checks + OMNIBUS history."""
    fields = _model_fields(row)
    if category_id is not None:
        fields["category_id"] = category_id
    update_in = ProductUpdate.model_validate(fields)

    if (
        update_in.slug
        and update_in.slug != product.slug
        and crud_product.get_by_slug(db, slug=update_in.slug)
    ):
        raise ValueError(f"slug {update_in.slug!r} already exists")
    if (
        update_in.sku
        and update_in.sku != product.sku
        and crud_product.get_by_sku(db, sku=update_in.sku)
    ):
        raise ValueError(f"sku {update_in.sku!r} already exists")

    crud_product.update(db, db_obj=product, obj_in=update_in)
    audit.record_update(resource_type="product", obj=product)
    _record_price_history(db, product)


def _apply_stock(db: Session, product: Product, target_stock: int, actor_id: int) -> None:
    """Move a variantless product's stock TO ``target_stock`` via ``apply_delta``.

    The CSV column is an absolute stock figure; we compute the delta and write it
    through the inventory ledger (never ``Product.stock`` directly).
    """
    delta = target_stock - product.stock
    if delta == 0:
        return
    crud_inventory.apply_delta(
        db,
        product=product,
        delta=delta,
        reason=InventoryMovementReason.MANUAL_ADJUSTMENT,
        actor_user_id=actor_id,
        note="CSV import",
        allow_negative=target_stock < 0,
    )


@router.post("/import", dependencies=[_catalog_write])
async def import_products(
    file: UploadFile = File(..., description="Product CSV (the export schema)"),
    dry_run: bool = Query(True, description="Validate only (no writes) when true"),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Import products from a CSV (admin). The round-trip of the export schema.

    ``dry_run=true`` (default) validates every row and **writes nothing**,
    returning the per-row report so the admin can preview. ``dry_run=false``
    applies the batch **all-or-nothing in one transaction**: any row error rolls
    the whole batch back and returns the error report with nothing applied.

    Upsert key per row: ``id`` → else ``slug`` → else ``sku``. A new product (no
    id, new slug) is created (requires name + slug + ≥1 price). ``tags`` /
    ``collection_slugs`` are split from their packed columns and REPLACE the
    product's tags / collection membership. Stock changes go through the
    inventory ledger (``apply_delta``); **variant products reject stock changes**
    (variant stock lives at variant grain). Dry-run and apply share one
    validator. On a successful apply the touched products are re-synced to Meili
    (incremental). Audited (``resource_type="product"``, action ``import``).
    """
    raw = await file.read()
    parsed = product_import.parse_csv(raw)
    if parsed.header_errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="; ".join(parsed.header_errors),
        )

    report_rows, touched = _process_rows(
        db, parsed.rows, apply=not dry_run, audit=audit, actor_id=current_admin.id
    )
    summary = product_import.build_summary(report_rows)

    if dry_run:
        # No writes happened, but the validator may have flushed lookups — be
        # explicit and roll back so the request leaves the DB untouched.
        db.rollback()
        return {
            "dry_run": True,
            "applied": False,
            "summary": summary,
            "rows": [r.as_dict() for r in report_rows],
        }

    # Apply path: all-or-nothing. Any row error → roll the whole batch back.
    if summary["errors"]:
        db.rollback()
        return {
            "dry_run": False,
            "applied": False,
            "summary": summary,
            "rows": [r.as_dict() for r in report_rows],
        }

    audit.record_event(
        action="import",
        resource_type="product",
        resource_id=None,
        payload={"creates": summary["creates"], "updates": summary["updates"]},
    )
    db.commit()
    # Incremental Meili re-sync of the touched products (no settings change → no
    # full reindex). Best-effort — never fails the import.
    search_index.sync_products_bulk(db, touched)

    return {
        "dry_run": False,
        "applied": True,
        "summary": summary,
        "rows": [r.as_dict() for r in report_rows],
    }


# --------------------------------------------------------------------------- #
# Bulk edit
# --------------------------------------------------------------------------- #


@router.patch("/bulk", response_model=ProductBulkUpdateResult, dependencies=[_catalog_write])
async def bulk_update_products(
    payload: ProductBulkUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Apply a narrow set of fields to many products at once (admin).

    Body: ``{ product_ids: [int], set: { price_usd?, price_pln?, price_eur?,
    is_active?, low_stock_threshold? } }``. The ``set`` is applied to **every**
    listed product in ONE transaction (all-or-nothing).

    **Stock is not a settable field** — it is the maintained sum of per-warehouse
    levels and may only move through the inventory ledger (``apply_delta``); the
    ``set`` schema is ``extra="forbid"`` so a ``stock`` key is rejected (422).

    Unknown-id handling is all-or-nothing: if any ``product_ids`` entry does not
    resolve to a product, the whole request fails with **404** (no partial writes)
    and the response lists the missing ids. On a price change, an OMNIBUS
    price-history row is recorded per changed product (reusing the shared helper).
    After commit, the touched products are re-synced to Meili (incremental — no
    settings change, no full reindex). Audited (``resource_type="product"``,
    action ``bulk_update``).
    """
    # ``model_fields_set`` on the validated ``set`` model tells us exactly which
    # fields the admin chose to write (distinguishing "omitted" from "set to its
    # default value" — e.g. ``is_active=False`` vs absent).
    set_fields = payload.set.model_fields_set
    update_values = {field: getattr(payload.set, field) for field in set_fields}
    touches_price = any(f.startswith("price_") for f in set_fields)

    # Resolve every id up front. Any miss → all-or-nothing 404, nothing written.
    products: list[Product] = []
    missing: list[int] = []
    for pid in payload.product_ids:
        product = crud_product.get(db, id=pid)
        if product is None:
            missing.append(pid)
        else:
            products.append(product)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No product(s) with id(s): {', '.join(str(m) for m in missing)}",
        )

    touched: list[int] = []
    for product in products:
        for field, value in update_values.items():
            setattr(product, field, value)
        db.add(product)
        audit.record_update(resource_type="product", obj=product)
        # OMNIBUS: append a price-history row for any currency whose price changed.
        # ``record_if_changed`` no-ops when the new price equals the most recent
        # row, so calling it for all three currencies is safe. Must run AFTER
        # ``audit.record_update`` — its flush would otherwise clear the diff the
        # audit writer reads (banked ordering from products PUT).
        if touches_price:
            _record_price_history(db, product)
        touched.append(product.id)

    db.commit()
    # Incremental Meili re-sync of the touched products (no settings change → no
    # full reindex). Best-effort — never fails the bulk edit.
    search_index.sync_products_bulk(db, touched)

    return ProductBulkUpdateResult(updated=len(touched), product_ids=touched)
