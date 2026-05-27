"""Meilisearch client + configuration gate.

Search is **backend-private**: the browser never talks to Meilisearch
directly. Route handlers receive a client via ``Depends(get_meili_client)``
(BE-C search endpoints), which raises ``HTTPException(503)`` when the engine is
unconfigured — an exact mirror of ``app.core.storage.get_r2_client`` so dev/CI
environments without Meilisearch still boot and serve every other endpoint.

The index-sync layer (``app.services.search_index``) uses the non-raising
``meili_configured()`` + ``build_client()`` pair instead, so a write path can
no-op silently rather than 503 when the engine is absent.

A short request timeout keeps a hung/slow engine from ever blocking a request
longer than a few seconds; sync paths additionally swallow + log on failure.
"""

import meilisearch
from fastapi import HTTPException, status

from app.core.config import settings

# Index names — shared by the sync layer and the query endpoints.
PRODUCTS_INDEX = "products"
CATEGORIES_INDEX = "categories"

# Both must be set for search to be considered configured (mirrors the R2 rule
# that every required field is present). The master key is used from the trusted
# backend only — the engine is never exposed to the browser, so no search-only
# key / tenant token is needed for v1.
_MEILI_REQUIRED_FIELDS = ("MEILISEARCH_URL", "MEILISEARCH_MASTER_KEY")

# Seconds. Bounds the worst case a down/slow engine can add to a request.
_MEILI_TIMEOUT_S = 5


def meili_configured() -> bool:
    """True only when every required Meilisearch setting is non-empty."""
    return all(getattr(settings, name) for name in _MEILI_REQUIRED_FIELDS)


def build_client() -> meilisearch.Client:
    """Construct a client. Caller must ensure ``meili_configured()`` first.

    Construction is pure config — no network call happens until an actual
    operation, so this never blocks on its own.
    """
    return meilisearch.Client(
        settings.MEILISEARCH_URL,
        settings.MEILISEARCH_MASTER_KEY,
        timeout=_MEILI_TIMEOUT_S,
    )


def get_meili_client() -> meilisearch.Client:
    """FastAPI dependency for search endpoints. 503s when unconfigured."""
    if not meili_configured():
        missing = [name for name in _MEILI_REQUIRED_FIELDS if not getattr(settings, name)]
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Search not configured — missing settings: {', '.join(missing)}. "
                "Set these in .env to enable storefront search."
            ),
        )
    return build_client()
