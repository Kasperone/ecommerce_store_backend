"""Store brand-name resolution for transactional emails + feeds.

The customer-facing brand (sender display name, email subjects/body copy, the
Google feed channel title) comes from the admin-editable ``store_config``
singleton, so rebranding a store is an admin-panel edit — not an env change +
redeploy. The env values (``EMAIL_FROM_NAME`` / ``APP_NAME``) remain the
fallback when ``store_name`` is blank or the lookup fails.

Deliberately uncached: email sends are infrequent, and a fresh read keeps the
admin edit → next email path immediate (and the tests trivial). The resolver
opens its **own** short-lived session so no caller signature grows a ``db``
param — several call sites (pure renderers, background tasks) have no session
at hand. Fail-open by design: an email must never fail because of a brand
lookup, so any exception logs a warning and returns the env fallback.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.database import SessionLocal
from app.crud.store_config import store_config as crud_store_config

logger = logging.getLogger(__name__)


def get_brand_name(fallback: str | None = None) -> str:
    """The store's display name: ``store_config.store_name`` or the env fallback.

    ``fallback`` defaults to ``settings.EMAIL_FROM_NAME`` (the historical
    source for email branding); the Google feed passes ``settings.APP_NAME``
    to preserve its own historical fallback. Never raises.
    """
    env_name = fallback if fallback is not None else settings.EMAIL_FROM_NAME
    try:
        db = SessionLocal()
        try:
            store_name = (crud_store_config.get_singleton(db).store_name or "").strip()
        finally:
            db.close()
    except Exception:
        logger.warning("Brand lookup failed; falling back to env value", exc_info=True)
        return env_name
    return store_name or env_name
