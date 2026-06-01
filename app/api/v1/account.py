"""Self-service account preferences API (`/api/v1/me/*`).

These endpoints are strictly owner-scoped: the user is always derived from the
bearer token via ``get_current_user`` — there is no path/body id, so a caller can
only ever read or write their own row.

Kept on a dedicated router (NOT folded into ``/auth/me``) so the hand-rolled FE
``UserResponse`` type stays untouched and these get a clean generated type of their
own (decisions D-A-1 / D-A-2). Auth-session additions live in ``auth.py`` (owned by
another task this wave).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, get_db
from app.crud import user as crud_user
from app.models.user import User
from app.schemas.account import PreferencesResponse, PreferencesUpdate

router = APIRouter()


@router.get("/preferences", response_model=PreferencesResponse)
async def get_preferences(
    current_user: User = Depends(get_current_user),
):
    """Return the caller's own preferences (currency + locale)."""
    return current_user


@router.patch("/preferences", response_model=PreferencesResponse)
async def update_preferences(
    payload: PreferencesUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Partially update the caller's preferences.

    Only fields present in the body change; an explicit ``preferred_currency: null``
    clears the preference. Unknown currency/locale codes are rejected with 422 by the
    request schema.
    """
    changes = payload.model_dump(exclude_unset=True)
    if changes:
        crud_user.set_preferences(db, user=current_user, changes=changes)
        db.commit()
        db.refresh(current_user)
    return current_user
