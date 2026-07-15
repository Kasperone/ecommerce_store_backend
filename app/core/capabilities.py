"""Staff capability taxonomy.

A small, closed set of coarse-grained *write* permissions layered over the
unchanged ``UserRole.CUSTOMER``/``ADMIN`` enum. A staff account (``role==ADMIN``)
is either an **owner** (``users.is_owner`` — implicitly holds every capability)
or a **scoped staffer** whose ``users.capabilities`` array lists the exact
capabilities they were granted.

Read (GET) admin routes stay open to any staff (``get_current_admin``). Every
write/DELETE admin route is fenced behind ``require_capability(<cap>)`` (see
``app/core/dependencies.py``). The set is deliberately aligned to the audit
log's ``resource_type`` vocabulary so a grant maps cleanly onto what shows up in
the audit trail.
"""

from __future__ import annotations

# --- Capability constants -------------------------------------------------
# Kept as module-level names so routers reference a symbol, not a stringly-typed
# literal (a typo then fails at import, not at request time).
CATALOG_WRITE = (
    "catalog:write"  # products, variants, categories, collections, tags, media, uploads, reviews
)
INVENTORY_WRITE = "inventory:write"  # inventory adjust/restock/transfer, warehouses
ORDERS_WRITE = "orders:write"  # order status, editing (A2), timeline comments (A3), drafts, quotes
ORDERS_REFUND = (
    "orders:refund"  # refunds, corrective invoices, gift cards / store-credit adjustments
)
CUSTOMERS_WRITE = "customers:write"  # customer records, notes, tags, segments, companies
MARKETING_WRITE = "marketing:write"  # coupons, banners, campaigns
CONTENT_WRITE = "content:write"  # CMS pages, blocks
SETTINGS_WRITE = "settings:write"  # store_config, shipping zones/methods/rates, translations
STAFF_WRITE = "staff:write"  # manage other staff + capabilities (owner-only in practice)

# The authoritative closed set. Any assignment is validated against this — an
# unknown capability is rejected at the schema boundary.
CAPABILITIES: frozenset[str] = frozenset(
    {
        CATALOG_WRITE,
        INVENTORY_WRITE,
        ORDERS_WRITE,
        ORDERS_REFUND,
        CUSTOMERS_WRITE,
        MARKETING_WRITE,
        CONTENT_WRITE,
        SETTINGS_WRITE,
        STAFF_WRITE,
    }
)
