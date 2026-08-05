"""Staff capability taxonomy.

A small, closed set of coarse-grained permissions layered over the unchanged
``UserRole.CUSTOMER``/``ADMIN`` enum. A staff account (``role==ADMIN``) is either
an **owner** (``users.is_owner`` — implicitly holds every capability) or a
**scoped staffer** whose ``users.capabilities`` array lists the exact
capabilities they were granted.

The taxonomy has two halves:

- **write capabilities** (``<domain>:write``, plus ``orders:refund``) fence every
  write/DELETE admin route behind ``require_capability(<cap>)``
- **read capabilities** (``<domain>:read``) fence admin *reads* — added by
  SEC-021, which found that any staffer could read the full customer database,
  every order and the audit log regardless of their grants

**Write implies read.** Holding ``catalog:write`` authorises ``catalog:read``
without the grant being stored, so no existing grant set breaks and no backfill
migration is needed. The implication map (``IMPLIED_READS``) also carries the
three genuine *cross-module* reads found by the Phase 0 call-graph inventory —
an order staffer must read companies and shipping to compose a draft order, a
catalog staffer must read tax rates to set a product's VAT. A read gate keyed
naively to the single matching write capability would break those workflows.

Two read domains are **standalone** (``STANDALONE_READS``): no write capability
implies ``reports:read`` or ``audit:read``, because neither surface has writes.
They must be granted explicitly, which is the intended tightening — before
SEC-021 the dashboard, reports and audit log carried no capability at all.

The set is deliberately aligned to the audit log's ``resource_type`` vocabulary
so a grant maps cleanly onto what shows up in the audit trail.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.models.user import User, UserRole

# --- Write capabilities ---------------------------------------------------
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

# --- Read capabilities ----------------------------------------------------
# Finer-grained than the write set on purpose: `companies`, `shipping` and `tax`
# are cross-module read targets, and folding them into `customers:read` /
# `settings:read` would over-grant the two most sensitive surfaces (the customer
# database and store configuration) to every order staffer.
CATALOG_READ = "catalog:read"  # products, variants, categories, collections, tags, reviews, media
INVENTORY_READ = "inventory:read"  # stock levels, warehouses, low-stock alerts
ORDERS_READ = "orders:read"  # orders, draft orders, returns, quotes, invoices
CUSTOMERS_READ = "customers:read"  # customer records, notes, tags, segments — PII
COMPANIES_READ = "companies:read"  # B2B company directory, members, price lists, net terms
MARKETING_READ = "marketing:read"  # coupons, banners, campaigns
CONTENT_READ = "content:read"  # CMS pages, blocks
SETTINGS_READ = "settings:read"  # store_config, translations
SHIPPING_READ = "shipping:read"  # shipping zones, methods, rates
TAX_READ = "tax:read"  # VAT rates, tax rules
STAFF_READ = "staff:read"  # the staff roster and who holds what
REPORTS_READ = "reports:read"  # admin dashboard + reports (revenue, top products)
AUDIT_READ = "audit:read"  # the admin audit log

WRITE_CAPABILITIES: frozenset[str] = frozenset(
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

READ_CAPABILITIES: frozenset[str] = frozenset(
    {
        CATALOG_READ,
        INVENTORY_READ,
        ORDERS_READ,
        CUSTOMERS_READ,
        COMPANIES_READ,
        MARKETING_READ,
        CONTENT_READ,
        SETTINGS_READ,
        SHIPPING_READ,
        TAX_READ,
        STAFF_READ,
        REPORTS_READ,
        AUDIT_READ,
    }
)

# The authoritative closed set. Any assignment is validated against this — an
# unknown capability is rejected at the schema boundary.
CAPABILITIES: frozenset[str] = WRITE_CAPABILITIES | READ_CAPABILITIES

# --- Write → read implication --------------------------------------------
# Every write capability grants its own domain's read plus, where the Phase 0
# frontend call-graph inventory found a real cross-module dependency, the reads
# that dependency needs. Amend this map (not the call sites) when annotating a
# route surfaces a fourth cross-module read.
#
# Reads never imply anything, so a single expansion pass is a fixpoint — an
# invariant the tests enforce.
IMPLIED_READS: Mapping[str, frozenset[str]] = {
    # /admin/products/{new,[id]/edit} render the per-product VAT-rate picker.
    CATALOG_WRITE: frozenset({CATALOG_READ, TAX_READ}),
    INVENTORY_WRITE: frozenset({INVENTORY_READ}),
    # /admin/quotes{,/new} pick the company a quote is for;
    # /admin/draft-orders/[id] needs shipping methods to price the order.
    ORDERS_WRITE: frozenset({ORDERS_READ, COMPANIES_READ, SHIPPING_READ}),
    ORDERS_REFUND: frozenset({ORDERS_READ}),
    CUSTOMERS_WRITE: frozenset({CUSTOMERS_READ, COMPANIES_READ}),
    MARKETING_WRITE: frozenset({MARKETING_READ}),
    CONTENT_WRITE: frozenset({CONTENT_READ}),
    SETTINGS_WRITE: frozenset({SETTINGS_READ, SHIPPING_READ, TAX_READ}),
    STAFF_WRITE: frozenset({STAFF_READ}),
}

# Reads no write capability grants. Granting one of these is a deliberate act:
# the surfaces have no writes, so there is no write capability to imply them.
STANDALONE_READS: frozenset[str] = frozenset({REPORTS_READ, AUDIT_READ})


def effective_capabilities(granted: Iterable[str] | None) -> frozenset[str]:
    """Expand a stored grant set into everything it actually authorises.

    Adds the read capabilities implied by each held write capability. Owners
    bypass this entirely (they hold every capability by definition) — see
    ``app/core/dependencies.py::require_read``.
    """
    caps = set(granted or ())
    for cap in tuple(caps):
        caps |= IMPLIED_READS.get(cap, frozenset())
    return frozenset(caps)


def staff_can_read(current_user: User | None, cap: str) -> bool:
    """Whether ``current_user`` may read the domain guarded by ``cap``.

    The non-raising half of read scoping, for **dual-purpose and public
    surfaces** where admin privilege is a *scope widening* rather than a
    separate endpoint: ``crud_order.get_scoped`` widens from "your own orders"
    to "all orders", and ``GET /products/`` lets staff see inactive products. A
    staffer without the read capability must **degrade to customer scope**, not
    403 — a 403 would stop a staffer seeing their *own* orders and would break
    the storefront for any logged-in staff member.

    This lives beside the taxonomy rather than in ``dependencies.py`` so the
    CRUD layer can enforce the policy at the choke point where the widening
    actually happens. That is deliberate: it is the same coupling the role check
    it replaces already had, and putting the fence in the widening branch means
    a future caller cannot forget to pass a flag.

    Anonymous callers and customers are ``False`` (they never take the widened
    branch); an owner is always ``True``.
    """
    if current_user is None or current_user.role != UserRole.ADMIN:
        return False
    if current_user.is_owner:
        return True
    return cap in effective_capabilities(current_user.capabilities)
