"""Public block-schema registry API.

``GET /blocks/schema`` returns the declarative Shopify-style settings-schema for
every block type (research §3.5) — the data that drives the admin auto-form. It is
public (it leaks no data, only the shape of the section catalog) and static.
"""

from typing import Any

from fastapi import APIRouter

from app.schemas.page import BLOCK_SCHEMAS, BLOCK_TYPES

router = APIRouter()


@router.get("/schema")
async def get_block_schemas() -> dict[str, Any]:
    """Return the settings-schema registry for all block types (drives the admin
    auto-form). Order is the canonical block-type order."""
    return {"blocks": [{"type": t, **BLOCK_SCHEMAS[t]} for t in BLOCK_TYPES]}
