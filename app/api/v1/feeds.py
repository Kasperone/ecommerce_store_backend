"""Public Google Shopping product-feed endpoints (Pillar E).

Unauthenticated, like the public catalog and sitemap sources: Merchant Center
fetches the URL on a daily schedule. One feed per market via path + query:

    GET /api/v1/feeds/google/{country}.xml?lang={lang}&currency={cur}

Examples (the markets we register in Merchant Center):
    * PL  market: /api/v1/feeds/google/PL.xml?lang=pl&currency=PLN
    * EUR market: /api/v1/feeds/google/DE.xml?lang=en&currency=EUR
    * US  market: /api/v1/feeds/google/US.xml?lang=en&currency=USD

The endpoint is generic over ``{country}`` + the query params, so any
(country, language, currency) combination works. Only ``is_active`` products
with a non-null price in the requested currency are emitted.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.orm import Session

from app.core.dependencies import get_db
from app.crud import product as crud_product
from app.services.google_feed import build_feed

router = APIRouter()


@router.get(
    "/google/{country}.xml",
    response_class=Response,
    responses={200: {"content": {"application/xml": {}}}},
)
async def google_shopping_feed(
    country: str = Path(
        ...,
        min_length=2,
        max_length=2,
        description="ISO-3166 alpha-2 target country (e.g. PL, DE, US).",
    ),
    lang: Literal["en", "pl"] = Query("en", description="Feed content language."),
    currency: Literal["USD", "PLN", "EUR"] = Query(
        "USD", description="Feed currency; must match the target country."
    ),
    db: Session = Depends(get_db),
) -> Response:
    """Google Shopping RSS 2.0 feed (``g:`` namespace) for one market.

    Public, no auth. Emits one ``<item>`` per variantless product and one per
    active variant (sharing ``g:item_group_id``), skipping anything without a
    price in ``currency``.
    """
    products = crud_product.get_feed_entries(db)
    xml_bytes = build_feed(
        products,
        country=country.upper(),
        lang=lang,
        currency=currency.upper(),
    )
    return Response(content=xml_bytes, media_type="application/xml")
