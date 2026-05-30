"""Pydantic schemas for the OMNIBUS price-history endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.schemas.base import BaseSchema


class LowestPriceResponse(BaseSchema):
    """``GET /products/{id}/lowest-30d?currency=PLN`` response.

    ``lowest_price`` + ``since`` are nullable to express "no history exists for
    this (product, currency) pair" — the storefront then renders no comparison
    line. ``currency`` is always echoed back so the caller can read the payload
    without re-quoting the query param.
    """

    currency: Literal["USD", "PLN", "EUR"]
    lowest_price: float | None
    since: datetime | None
