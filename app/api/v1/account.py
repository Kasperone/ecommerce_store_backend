"""Self-service account preferences + GDPR API (`/api/v1/me/*`).

These endpoints are strictly owner-scoped: the user is always derived from the
bearer token via ``get_current_user`` — there is no path/body id, so a caller can
only ever read or write their own row.

Kept on a dedicated router (NOT folded into ``/auth/me``) so the hand-rolled FE
``UserResponse`` type stays untouched and these get a clean generated type of their
own (decisions D-A-1 / D-A-2). Auth-session additions live in ``auth.py`` (owned by
another task this wave).

GDPR surfaces (Wave B):
- consent is read/written through ``/preferences`` against the append-only
  ``user_consents`` log (D-B-5);
- ``/data-export`` returns the caller's full personal-data snapshot (Art. 15/20);
- ``/account-deletion-request`` soft-deletes: it stamps the request and
  anonymizes PII immediately while keeping order/invoice rows (D-B-4 / Art. 17).
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.dependencies import client_ip, get_current_user, get_db, user_agent
from app.crud import user as crud_user
from app.crud import user_consent as crud_user_consent
from app.models.user import User
from app.schemas.account import (
    AccountDeletionResponse,
    DataExportResponse,
    PreferencesResponse,
    PreferencesUpdate,
)
from app.services.account_export import build_export

router = APIRouter()


def _preferences_snapshot(db: Session, user: User) -> dict:
    """Build the GET/PATCH preferences payload (currency + locale + consents)."""
    return {
        "preferred_currency": user.preferred_currency,
        "locale": user.locale,
        "consents": crud_user_consent.current_map(db, user_id=user.id),
    }


@router.get("/preferences", response_model=PreferencesResponse)
async def get_preferences(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the caller's own preferences (currency + locale + consents map)."""
    return _preferences_snapshot(db, current_user)


@router.patch("/preferences", response_model=PreferencesResponse)
async def update_preferences(
    payload: PreferencesUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Partially update the caller's preferences.

    Only fields present in the body change; an explicit ``preferred_currency: null``
    clears the preference. Unknown currency/locale codes are rejected with 422 by the
    request schema. Each entry in the optional ``consents`` map appends a new row to
    the append-only consent log (never an UPDATE), capturing source + ip + user_agent.
    """
    changes = payload.model_dump(exclude_unset=True)
    consents = changes.pop("consents", None)

    if changes:
        crud_user.set_preferences(db, user=current_user, changes=changes)

    if consents:
        ip = client_ip(request)
        ua = user_agent(request)
        for consent_type, granted in consents.items():
            crud_user_consent.record(
                db,
                user_id=current_user.id,
                consent_type=consent_type,
                granted=granted,
                source="preferences",
                ip=ip,
                user_agent=ua,
            )

    if changes or consents:
        db.commit()
        db.refresh(current_user)

    return _preferences_snapshot(db, current_user)


@router.post("/data-export", response_model=DataExportResponse)
async def export_my_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the caller's full personal-data export (GDPR Art. 15/20).

    Owner-scoped: profile + addresses + orders (with items) + consent trail. Auth
    secrets (password hash, reset-token hash, TOTP secret) are never included.
    """
    return build_export(db, user=current_user)


@router.post("/account-deletion-request", response_model=AccountDeletionResponse)
async def request_account_deletion(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Request account deletion (GDPR Art. 17) — soft-delete, applied immediately.

    Stamps ``deletion_requested_at`` and anonymizes PII in place (name/phone/email,
    shipping + company fields), deletes the address book, and revokes all refresh
    tokens. Order/invoice rows are KEPT for tax retention. No purge job runs — the
    5-year retention window is documented only (D-B-4 / D-ACCT-1).
    """
    crud_user.anonymize(db, user=current_user)
    db.commit()
    db.refresh(current_user)
    # ``anonymize`` always stamps both columns; ``model_validate`` reads them off
    # the refreshed ORM row (the response schema's required fields are satisfied).
    return AccountDeletionResponse.model_validate(
        {
            "deletion_requested_at": current_user.deletion_requested_at,
            "anonymized_at": current_user.anonymized_at,
        }
    )
