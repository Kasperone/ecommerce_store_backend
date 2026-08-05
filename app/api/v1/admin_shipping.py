"""Admin shipping API — zones, methods, and rates CRUD.

All routes admin-gated. Mutations audited with resource_type ``shipping_zone``,
``shipping_method``, or ``shipping_rate``. The public storefront quote
endpoint lives in ``app/api/v1/shipping.py``.

Conflict handling: zone country-set updates surface
``ShippingZoneCountryConflictError`` (one country can't be in two zones) as a
400 with the conflicting zone's name; rate creates that collide with the
``UNIQUE(zone_id, method_id, currency)`` constraint surface as a 400 with the
existing-combo message.
"""

from __future__ import annotations

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.capabilities import SETTINGS_WRITE, SHIPPING_READ
from app.core.dependencies import get_current_admin, get_db, require_capability, require_read
from app.core.money import as_float
from app.crud import shipping_method as crud_shipping_method
from app.crud import shipping_rate as crud_shipping_rate
from app.crud import shipping_zone as crud_shipping_zone
from app.crud.shipping import ShippingZoneCountryConflictError
from app.models.shipping import ShippingRate, ShippingZone
from app.models.user import User
from app.schemas.shipping import (
    ShippingMethodCreate,
    ShippingMethodListResponse,
    ShippingMethodResponse,
    ShippingMethodUpdate,
    ShippingRateCreate,
    ShippingRateListItem,
    ShippingRateListResponse,
    ShippingRateResponse,
    ShippingRateTier,
    ShippingRateUpdate,
    ShippingRateWeightTier,
    ShippingZoneCreate,
    ShippingZoneDetail,
    ShippingZoneListItem,
    ShippingZoneListResponse,
    ShippingZoneUpdate,
    _classify_pricing,
)
from app.services.audit import AuditWriter, get_audit_writer
from app.services.translation_admin import purge_translation_meta

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(SETTINGS_WRITE))
_read = Depends(require_read(SHIPPING_READ))


def _zone_detail(zone: ShippingZone) -> ShippingZoneDetail:
    """Build the ``ShippingZoneDetail`` payload with sorted country codes."""
    return ShippingZoneDetail(
        id=zone.id,
        created_at=zone.created_at,
        updated_at=zone.updated_at,
        name=zone.name,
        description=zone.description,
        is_active=zone.is_active,
        sort_order=zone.sort_order,
        country_codes=crud_shipping_zone.country_codes_for(zone),
        dim_divisor=zone.dim_divisor,
    )


def _rate_list_item(rate: ShippingRate) -> ShippingRateListItem:
    """Build the matrix-view list row (embeds zone+method names + a pricing tag)."""
    tier_models = [ShippingRateTier(**t) for t in rate.tiers] if rate.tiers is not None else None
    weight_tier_models = (
        [ShippingRateWeightTier(**t) for t in rate.weight_tiers]
        if rate.weight_tiers is not None
        else None
    )
    return ShippingRateListItem(
        id=rate.id,
        created_at=rate.created_at,
        updated_at=rate.updated_at,
        zone_id=rate.zone_id,
        zone_name=rate.zone.name,
        method_id=rate.method_id,
        method_name=rate.method.name,
        currency=rate.currency,
        amount=float(rate.amount),
        free_above_subtotal=as_float(rate.free_above_subtotal),
        tiers=tier_models,
        weight_tiers=weight_tier_models,
        pricing_kind=_classify_pricing(
            rate.amount, rate.free_above_subtotal, tier_models, weight_tier_models
        ),
    )


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


@router.get("/zones", response_model=ShippingZoneListResponse)
async def list_zones(
    db: Session = Depends(get_db),
    current_admin: User = _read,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """List zones with country + rate counts (admin)."""
    skip = (page - 1) * page_size
    zones = crud_shipping_zone.get_multi_admin(db, skip=skip, limit=page_size)
    total = crud_shipping_zone.count_admin(db)
    country_counts = crud_shipping_rate.aggregate_country_counts(db)
    rate_counts = crud_shipping_rate.aggregate_rate_counts(db)
    items = [
        ShippingZoneListItem(
            id=z.id,
            created_at=z.created_at,
            updated_at=z.updated_at,
            name=z.name,
            description=z.description,
            is_active=z.is_active,
            sort_order=z.sort_order,
            country_count=country_counts.get(z.id, 0),
            rate_count=rate_counts.get(z.id, 0),
            dim_divisor=z.dim_divisor,
        )
        for z in zones
    ]
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/zones/{zone_id}", response_model=ShippingZoneDetail)
async def get_zone(
    zone_id: int,
    db: Session = Depends(get_db),
    current_admin: User = _read,
):
    """Get one zone with its country list (admin)."""
    zone = crud_shipping_zone.get_with_countries(db, id=zone_id)
    if zone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipping zone not found")
    return _zone_detail(zone)


@router.post(
    "/zones",
    dependencies=[_require],
    response_model=ShippingZoneDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_zone(
    payload: ShippingZoneCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a zone with its initial country assignment (admin)."""
    try:
        zone = crud_shipping_zone.create_with_countries(db, obj_in=payload)
    except ShippingZoneCountryConflictError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    audit.record_create(resource_type="shipping_zone", obj=zone)
    db.commit()
    # Re-fetch the freshly-committed row with its countries eager-loaded so
    # the response carries the country codes the admin just assigned.
    fresh = crud_shipping_zone.get_with_countries(db, id=zone.id)
    # We just committed it; if missing, something is very wrong — fail loud.
    if fresh is None:  # pragma: no cover -- defensive guard against torn writes
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Zone disappeared after commit",
        )
    return _zone_detail(fresh)


@router.put("/zones/{zone_id}", dependencies=[_require], response_model=ShippingZoneDetail)
async def update_zone(
    zone_id: int,
    payload: ShippingZoneUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a zone (admin). Reassigns countries if ``country_codes`` is set."""
    zone = crud_shipping_zone.get_with_countries(db, id=zone_id)
    if zone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipping zone not found")
    try:
        crud_shipping_zone.apply_update(db, zone=zone, obj_in=payload)
    except ShippingZoneCountryConflictError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    audit.record_update(resource_type="shipping_zone", obj=zone)
    db.commit()
    fresh = crud_shipping_zone.get_with_countries(db, id=zone.id)
    if fresh is None:  # pragma: no cover -- defensive guard against torn writes
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Zone disappeared after commit",
        )
    return _zone_detail(fresh)


@router.delete("/zones/{zone_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_zone(
    zone_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a zone (admin). Cascades country assignments + rates."""
    zone = crud_shipping_zone.get(db, id=zone_id)
    if zone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipping zone not found")
    audit.record_delete(resource_type="shipping_zone", obj=zone)
    db.delete(zone)
    db.commit()


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------


@router.get("/methods", response_model=ShippingMethodListResponse)
async def list_methods(
    db: Session = Depends(get_db),
    current_admin: User = _read,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """List shipping methods (admin)."""
    skip = (page - 1) * page_size
    methods = crud_shipping_method.get_multi_admin(db, skip=skip, limit=page_size)
    total = crud_shipping_method.count_admin(db)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": methods,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/methods/{method_id}", response_model=ShippingMethodResponse)
async def get_method(
    method_id: int,
    db: Session = Depends(get_db),
    current_admin: User = _read,
):
    method = crud_shipping_method.get(db, id=method_id)
    if method is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Shipping method not found"
        )
    return method


@router.post(
    "/methods",
    dependencies=[_require],
    response_model=ShippingMethodResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_method(
    payload: ShippingMethodCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a shipping method (admin). 400 on duplicate name/code."""
    try:
        method = crud_shipping_method.create(db, obj_in=payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A method with that name or code already exists",
        ) from exc
    audit.record_create(resource_type="shipping_method", obj=method)
    db.commit()
    db.refresh(method)
    return method


@router.put("/methods/{method_id}", dependencies=[_require], response_model=ShippingMethodResponse)
async def update_method(
    method_id: int,
    payload: ShippingMethodUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    method = crud_shipping_method.get(db, id=method_id)
    if method is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Shipping method not found"
        )
    try:
        crud_shipping_method.update(db, db_obj=method, obj_in=payload)
        audit.record_update(resource_type="shipping_method", obj=method)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A method with that name or code already exists",
        ) from exc
    db.refresh(method)
    return method


@router.delete(
    "/methods/{method_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_method(
    method_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a method (admin). CASCADEs its rates; orders snapshotted the
    method name + the FK is SET NULL so history survives."""
    method = crud_shipping_method.get(db, id=method_id)
    if method is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Shipping method not found"
        )
    audit.record_delete(resource_type="shipping_method", obj=method)
    db.delete(method)
    # The translation sidecar is polymorphic, so it carries no FK and nothing
    # at the database level cleans up after it.
    purge_translation_meta(db, resource_type="shipping_method", resource_ids=[method_id])
    db.commit()


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


@router.get("/rates", response_model=ShippingRateListResponse)
async def list_rates(
    db: Session = Depends(get_db),
    current_admin: User = _read,
    zone_id: int | None = Query(None, description="Filter by zone"),
    method_id: int | None = Query(None, description="Filter by method"),
    currency: str | None = Query(None, pattern="^(USD|PLN|EUR)$", description="Filter by currency"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """List rates with filters (admin matrix view)."""
    skip = (page - 1) * page_size
    rates = crud_shipping_rate.get_multi_admin(
        db, zone_id=zone_id, method_id=method_id, currency=currency, skip=skip, limit=page_size
    )
    total = crud_shipping_rate.count_admin(
        db, zone_id=zone_id, method_id=method_id, currency=currency
    )
    items = [_rate_list_item(r) for r in rates]
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post(
    "/rates",
    dependencies=[_require],
    response_model=ShippingRateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_rate(
    payload: ShippingRateCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a rate (admin). 400 on duplicate (zone, method, currency) combo."""
    # Validate FK targets exist so we can return a clean 404 instead of an FK
    # IntegrityError; same UX as customers/inventory.
    if crud_shipping_zone.get(db, id=payload.zone_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipping zone not found")
    if crud_shipping_method.get(db, id=payload.method_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Shipping method not found"
        )
    try:
        rate = crud_shipping_rate.create(db, obj_in=payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"A rate already exists for zone_id={payload.zone_id} "
                f"method_id={payload.method_id} currency={payload.currency}"
            ),
        ) from exc
    audit.record_create(resource_type="shipping_rate", obj=rate)
    db.commit()
    db.refresh(rate)
    return rate


@router.put("/rates/{rate_id}", dependencies=[_require], response_model=ShippingRateResponse)
async def update_rate(
    rate_id: int,
    payload: ShippingRateUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    rate = crud_shipping_rate.get(db, id=rate_id)
    if rate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipping rate not found")
    try:
        crud_shipping_rate.apply_update(db, rate=rate, obj_in=payload)
        audit.record_update(resource_type="shipping_rate", obj=rate)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That zone+method+currency combo is already taken by another rate",
        ) from exc
    db.refresh(rate)
    return rate


@router.delete("/rates/{rate_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_rate(
    rate_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    rate = crud_shipping_rate.get(db, id=rate_id)
    if rate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipping rate not found")
    audit.record_delete(resource_type="shipping_rate", obj=rate)
    db.delete(rate)
    db.commit()
