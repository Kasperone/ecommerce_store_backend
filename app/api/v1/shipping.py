"""Public shipping quote endpoint.

Customers fetch the available shipping methods + resolved prices for their
``(country, currency, subtotal)`` combo from the storefront checkout selector.
No auth — same posture as ``GET /api/v1/products/``.

Empty ``methods=[]`` is the canonical signal that we don't ship to the asked
country (the FE renders "we don't ship there" rather than 404 — a 404 would
be ambiguous because the route itself exists).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.dependencies import get_db
from app.crud import shipping_rate as crud_shipping_rate
from app.schemas.shipping import (
    ShippingQuoteMethod,
    ShippingQuoteResponse,
    _classify_pricing,
)

router = APIRouter()


SupportedCurrency = Literal["USD", "PLN", "EUR"]


@router.get("/quote", response_model=ShippingQuoteResponse)
async def quote_shipping(
    # Constraints declared via Query() so FastAPI/Pydantic enforces them on the
    # query param (an Annotated[str, StringConstraints(...)] alias is honored
    # only on body/response models, not Query params — caught when /quote was
    # accepting "poland" and only blowing up at response-model validation).
    country: str = Query(
        ...,
        pattern=r"^[A-Z]{2}$",
        min_length=2,
        max_length=2,
        description="ISO-2 country code (uppercase)",
    ),
    currency: SupportedCurrency = Query(..., description="USD, PLN, or EUR"),
    subtotal: float = Query(..., ge=0, description="Cart subtotal in the asked currency"),
    weight_kg: float = Query(
        0,
        ge=0,
        description=(
            "Cart total weight in kg. When non-zero, weight-tiered rates resolve "
            "against this value (free-above → weight-tiers → subtotal-tiers → "
            "flat). Default 0 keeps storefronts that don't yet thread cart weight "
            "working — they get subtotal-only resolution."
        ),
    ),
    db: Session = Depends(get_db),
):
    """Return the available shipping methods + resolved prices for a checkout.

    Each method's ``amount`` reflects the *resolved* shipping price for the
    provided subtotal and (optional) cart weight — free-above triggered,
    weight-tier band picked, subtotal-tier band picked, or the flat fallback.
    The storefront renders it directly without re-running the resolver.
    """
    pairs = crud_shipping_rate.resolve_for_country(db, country_code=country, currency=currency)
    methods = [
        ShippingQuoteMethod(
            id=method.id,
            name=method.name,
            code=method.code,
            description=method.description,
            sort_order=method.sort_order,
            amount=round(
                crud_shipping_rate.apply_shipping_rate(rate, subtotal, weight_kg=weight_kg),
                2,
            ),
            currency=currency,
            is_free=(rate.free_above_subtotal is not None and subtotal >= rate.free_above_subtotal),
            pricing_kind=_classify_pricing(
                rate.amount, rate.free_above_subtotal, rate.tiers, rate.weight_tiers
            ),
            free_above_subtotal=rate.free_above_subtotal,
        )
        for method, rate in pairs
    ]
    return ShippingQuoteResponse(
        country_code=country,
        currency=currency,
        subtotal=subtotal,
        methods=methods,
    )
