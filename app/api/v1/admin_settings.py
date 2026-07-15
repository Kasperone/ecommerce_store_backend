"""Admin store-settings API — view + edit the single store-identity + VAT row.

Both routes are admin-gated and audited (resource_type ``store_config``). The
config is a single row (``id=1``, seeded by the migration), so there is no
create/list/delete — just GET (the current values) and PATCH (a partial update).

``invoice_vat_rate`` is validated 0-100 by the request schema (422 on a bad
value), matching the DB CHECK constraint.

The invoice renderer and the transactional order emails consume this row at
render time via ``StoreIdentity.from_store_config`` (each field used when
non-blank, else falling back to the ``app/core/config.py`` env default), so edits
made here flow through to invoices/emails without a redeploy.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.capabilities import SETTINGS_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.crud import store_config as crud_store_config
from app.models.user import User
from app.schemas.store_config import StoreConfigResponse, StoreConfigUpdate
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(SETTINGS_WRITE))


@router.get("", response_model=StoreConfigResponse)
async def get_settings(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get the current store settings (the single ``id=1`` config row)."""
    return crud_store_config.get_singleton(db)


@router.patch("", dependencies=[_require], response_model=StoreConfigResponse)
async def update_settings(
    payload: StoreConfigUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update store settings (partial). Only the fields sent are changed;
    ``invoice_vat_rate`` must be 0-100 (422 otherwise). Writes an audit row."""
    config = crud_store_config.get_singleton(db)
    crud_store_config.update(db, config=config, obj_in=payload)
    audit.record_update(resource_type="store_config", obj=config)
    db.commit()
    db.refresh(config)
    return config
