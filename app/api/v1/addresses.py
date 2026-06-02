"""User address-book API (`/api/v1/addresses`).

Owner-scoped: the user is always derived from the bearer token via
``get_current_user`` and every lookup goes through ``crud_address.get_owned``
(``id`` + ``user_id``), so a caller can only ever read or mutate their own rows.
A cross-user or unknown id returns **404** (never 403) — the response is identical
whether the row belongs to someone else or doesn't exist, so it can't be used to
probe for other users' address ids.

Default-flag mutual-exclusion and first-address-auto-default are enforced
transactionally in ``crud_address`` (the route owns the commit).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, get_db
from app.crud import address as crud_address
from app.models.user import User
from app.schemas.address import AddressCreate, AddressResponse, AddressUpdate

router = APIRouter()


@router.get("", response_model=list[AddressResponse])
async def list_addresses(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the caller's own addresses (default-shipping first, then newest)."""
    return crud_address.get_multi_for_user(db, user_id=current_user.id)


@router.post("", response_model=AddressResponse, status_code=status.HTTP_201_CREATED)
async def create_address(
    payload: AddressCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create an address for the caller.

    The first address a user creates auto-becomes both defaults; otherwise the
    posted ``is_default_*`` flags apply (clearing the matching flag on the
    caller's other rows).
    """
    obj = crud_address.create(db, user_id=current_user.id, obj_in=payload)
    db.commit()
    db.refresh(obj)
    return obj


@router.put("/{address_id}", response_model=AddressResponse)
async def update_address(
    address_id: int,
    payload: AddressUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update one of the caller's addresses. Unknown/cross-user id → 404."""
    obj = crud_address.get_owned(db, id=address_id, user_id=current_user.id)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")
    obj = crud_address.update(db, address=obj, obj_in=payload)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(
    address_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete one of the caller's addresses. Unknown/cross-user id → 404."""
    obj = crud_address.get_owned(db, id=address_id, user_id=current_user.id)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Address not found")
    crud_address.delete(db, address=obj)
    db.commit()
