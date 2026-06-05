"""Public store-config API — the customer-facing read side of store settings.

Unauthenticated, like the public banners/catalog reads: no ``get_current_*``
dependency. Returns a slim ``StoreConfigPublic`` projection of the single
store-config row (the footer legally needs the company-identification + social
fields) — it deliberately excludes ``invoice_vat_rate`` and the internal
``tax_id``/audit noise.

The admin CRUD lives in ``admin_settings.py`` (gated + audited); this exposes the
read-only public shape. There is always exactly one row (``get_singleton``
get-or-creates it), so this never 404s.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import get_db
from app.crud import store_config as crud_store_config
from app.schemas.store_config import StoreConfigPublic

router = APIRouter()


@router.get("", response_model=StoreConfigPublic)
async def get_public_store_config(db: Session = Depends(get_db)):
    """Get the public store-config projection for the storefront (no auth)."""
    return crud_store_config.get_singleton(db)
