"""Admin **read** scoping (SEC-021 phase 2) — both mechanisms.

Phase 1 shipped the taxonomy and the gates; this is the annotation, and these
are the tests that keep it true.

Read scoping needs two mechanisms, and a test suite that only checks the first
certifies the wrong thing:

1. **`admin_*` routers** — the whole route is admin-only, so the gate is a
   route dependency and a miss is a 403.
2. **Dual-purpose / public routes** — admin privilege is a *scope widening*
   (`crud_order.get_scoped` and `_apply_scope_and_filters`; the `is_active`
   override on `GET /products/`). A staffer without the capability must
   **degrade to customer scope**, never 403: these routes serve customers and
   the storefront, and a staffer has to keep seeing their own orders.

⚠️ This is the SEC-016 blind spot restated. The write tripwire in
``test_products_capability_gate.py`` walks the dependency tree for
``get_current_admin``, which a dual-purpose route never declares — that is how
an ungated cancel route survived an RBAC sweep scoring ``orders.py`` 170/171.
A read tripwire that walked only ``admin_*`` would be blind to ``orders.py`` and
``products.py``, the two most sensitive read surfaces in the app.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi.routing import APIRoute

import app as app_package
from app.core.capabilities import (
    CATALOG_WRITE,
    CUSTOMERS_WRITE,
    MARKETING_WRITE,
    ORDERS_WRITE,
    SETTINGS_WRITE,
)
from app.core.security import create_access_token
from app.crud import order as crud_order
from app.main import app
from app.models.order import Order
from app.models.user import User, UserRole
from app.schemas.order import OrderCreate, OrderItemCreate

pytestmark = pytest.mark.integration


def _headers(user: User) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _dependency_call_qualnames(dependant) -> list[str]:
    """Every callable reachable in a route's dependency tree, by ``__qualname__``."""
    names: list[str] = []

    def _walk(dep) -> None:
        if dep.call is not None:
            names.append(getattr(dep.call, "__qualname__", "") or "")
        for sub in dep.dependencies:
            _walk(sub)

    _walk(dependant)
    return names


# ---------------------------------------------------------------------------
# Mechanism 1 — structural tripwire over admin-only GET routes
# ---------------------------------------------------------------------------


def test_every_admin_read_route_is_capability_gated():
    """Enumerate every GET route whose dependency tree includes
    ``get_current_admin`` and assert each also requires a ``require_read`` gate.
    Fails loudly when a new admin read route lands ungated — the mirror of
    ``test_every_admin_write_route_is_capability_gated``.
    """
    ungated: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if "GET" not in (route.methods or set()):
            continue
        qualnames = _dependency_call_qualnames(route.dependant)
        if not any("get_current_admin" in name for name in qualnames):
            continue
        if not any("require_read.<locals>._dep" in name for name in qualnames):
            ungated.append(f"GET {route.path}")

    assert not ungated, "Admin read routes missing require_read() — add a read gate:\n" + "\n".join(
        ungated
    )


def test_the_tripwire_actually_sees_routes():
    """Positive control. An assertion over an empty set passes for the wrong
    reason, so pin the population it walks — if a refactor moved every admin GET
    out of the dependency-tree shape this test looks for, the check above would
    go quietly green while gating nothing.
    """
    admin_gets = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and "GET" in (route.methods or set())
        and any("get_current_admin" in n for n in _dependency_call_qualnames(route.dependant))
    ]
    assert len(admin_gets) >= 60, f"expected the full admin read surface, found {len(admin_gets)}"


# ---------------------------------------------------------------------------
# Mechanism 2 — the role check must not come back
# ---------------------------------------------------------------------------

# Every function in the app that still compares against ``UserRole.ADMIN``, with
# the reason it is legitimately a *role* question rather than a *capability*
# one. Scope widening is NOT on this list: `crud_order.get_scoped`,
# `_apply_scope_and_filters` and `products.list_products` all key on
# ``staff_can_read`` now, and a new entry appearing here is the signal that
# someone reintroduced a role-keyed authorisation decision.
_ROLE_CHECK_ALLOWLIST = {
    # Refuses impersonation of another staff account — a role question.
    "app/api/v1/admin_customers.py::impersonate_customer",
    # Lists / promotes / demotes staff accounts; the role IS the subject.
    "app/api/v1/admin_staff.py::list_staff",
    "app/api/v1/admin_staff.py::update_staff",
    # SEC-016's dual-purpose write branch — gated on ORDERS_WRITE inside it.
    "app/api/v1/orders.py::cancel_order",
    # The gates themselves.
    "app/core/capabilities.py::staff_can_read",
    "app/core/dependencies.py::get_current_admin",
    # Staff-pool bookkeeping (last-active-admin guard).
    "app/crud/user.py::is_admin",
    "app/crud/user.py::count_active_admins",
    # Bootstrap CLI.
    "app/scripts/create_admin.py::create_admin",
}


# Resolved from the package, not the cwd — these tests must not depend on where
# pytest was invoked from.
_APP_DIR = pathlib.Path(app_package.__file__ or "").parent
_REPO_ROOT = _APP_DIR.parent


def _functions_referencing_admin_role() -> set[str]:
    found: set[str] = set()
    for path in sorted(_APP_DIR.rglob("*.py")):
        source = path.read_text()
        if "UserRole.ADMIN" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            segment = ast.get_source_segment(source, node) or ""
            if "UserRole.ADMIN" in segment:
                rel = path.relative_to(_REPO_ROOT).as_posix()
                found.add(f"{rel}::{node.name}")
    return found


def test_no_new_role_keyed_authorisation_decisions():
    """The drift guard for mechanism 2.

    A structural check can't tell "widen this query for staff" from "refuse to
    impersonate a staff account" — both are `role == ADMIN`. So pin the whole
    population instead: any *new* function comparing against ``UserRole.ADMIN``
    fails this test and has to be classified deliberately, which is exactly the
    review moment SEC-016 never got.
    """
    found = _functions_referencing_admin_role()
    unexpected = found - _ROLE_CHECK_ALLOWLIST
    stale = _ROLE_CHECK_ALLOWLIST - found
    assert not unexpected, (
        "New role-keyed check(s). If this is a scope widening, key it on "
        "staff_can_read(user, <cap>) instead; if it is a genuine role question, "
        "add it to _ROLE_CHECK_ALLOWLIST with a reason:\n" + "\n".join(sorted(unexpected))
    )
    assert not stale, "Allowlist entries no longer present — remove them:\n" + "\n".join(
        sorted(stale)
    )


@pytest.mark.parametrize(
    ("module", "func"),
    [
        ("app/crud/order.py", "get_scoped"),
        ("app/crud/order.py", "_apply_scope_and_filters"),
        ("app/api/v1/products.py", "list_products"),
    ],
)
def test_widening_choke_points_key_on_the_capability(module: str, func: str):
    """The three places where staff privilege widens a query for a caller who
    reached a customer-facing route. Each must consult ``staff_can_read``."""
    source = (_REPO_ROOT / module).read_text()
    node = next(
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == func
    )
    segment = ast.get_source_segment(source, node) or ""
    assert "staff_can_read" in segment, f"{module}::{func} no longer consults staff_can_read"


# ---------------------------------------------------------------------------
# Behaviour — mechanism 1
# ---------------------------------------------------------------------------


async def test_ungranted_staffer_403s_on_an_admin_read(client, user_factory):
    staffer = user_factory(
        email="read-none@example.com", role=UserRole.ADMIN, is_owner=False, capabilities=[]
    )
    resp = await client.get("/api/v1/admin/customers/", headers=_headers(staffer))
    assert resp.status_code == 403


async def test_write_capability_implies_the_matching_read(client, user_factory):
    """The no-migration guarantee, end to end: nothing granted ``customers:read``."""
    staffer = user_factory(
        email="read-customers@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CUSTOMERS_WRITE],
    )
    resp = await client.get("/api/v1/admin/customers/", headers=_headers(staffer))
    assert resp.status_code == 200


async def test_order_staff_can_read_shipping_and_companies(client, user_factory):
    """Cross-module reads from the Phase 0 inventory: composing a draft order
    needs shipping methods, quoting needs the company directory. A gate keyed to
    the matching write capability alone would 403 both."""
    staffer = user_factory(
        email="read-orders@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[ORDERS_WRITE],
    )
    assert (
        await client.get("/api/v1/admin/shipping/methods", headers=_headers(staffer))
    ).status_code == 200
    assert (
        await client.get("/api/v1/admin/companies/", headers=_headers(staffer))
    ).status_code == 200


async def test_catalog_staff_can_read_tax_categories(client, user_factory):
    """The per-product VAT picker on the product form."""
    staffer = user_factory(
        email="read-catalog@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE],
    )
    resp = await client.get("/api/v1/admin/tax/categories", headers=_headers(staffer))
    assert resp.status_code == 200


async def test_cross_module_reads_do_not_leak_the_sensitive_surfaces(client, user_factory):
    """An order staffer reads companies and shipping — but not the customer
    database, and not store settings."""
    staffer = user_factory(
        email="read-orders-limits@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[ORDERS_WRITE],
    )
    assert (
        await client.get("/api/v1/admin/customers/", headers=_headers(staffer))
    ).status_code == 403
    assert (
        await client.get("/api/v1/admin/settings", headers=_headers(staffer))
    ).status_code == 403


async def test_dashboard_reports_and_audit_need_an_explicit_grant(client, user_factory):
    """The SEC-021 tightening. No write capability implies these, so the widest
    write grant in the taxonomy still cannot read them."""
    staffer = user_factory(
        email="read-standalone@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[SETTINGS_WRITE, MARKETING_WRITE, CATALOG_WRITE],
    )
    for path in (
        "/api/v1/admin/dashboard/kpis",
        "/api/v1/admin/reports/sales",
        "/api/v1/admin/audit-events/",
    ):
        assert (await client.get(path, headers=_headers(staffer))).status_code == 403, path


async def test_owner_reads_everything(client, user_factory):
    owner = user_factory(email="read-owner@example.com", role=UserRole.ADMIN)
    assert owner.is_owner is True
    for path in (
        "/api/v1/admin/customers/",
        "/api/v1/admin/dashboard/kpis",
        "/api/v1/admin/audit-events/",
        "/api/v1/admin/settings",
    ):
        assert (await client.get(path, headers=_headers(owner))).status_code == 200, path


# ---------------------------------------------------------------------------
# Behaviour — mechanism 2 (degrade to customer scope, never 403)
# ---------------------------------------------------------------------------


def _order_for(db, user: User, *, product, shipping_method_id: int) -> Order:
    """A real order for ``user``, built through the checkout CRUD so every
    required column and snapshot is populated the way production does it."""
    return crud_order.create_with_items(
        db,
        user_id=user.id,
        obj_in=OrderCreate(
            items=[OrderItemCreate(product_id=product.id, quantity=1)],
            currency="USD",
            shipping_address="1 Test St",
            shipping_city="Testville",
            shipping_postal_code="00-001",
            shipping_country="PL",
            shipping_method_id=shipping_method_id,
        ),
    )


async def test_staffer_without_orders_read_sees_only_their_own_orders(
    client, db_session, user_factory, product_factory, test_shipping_method_id
):
    """The whole reason this route uses ``staff_can_read`` and not a 403 gate."""
    staffer = user_factory(
        email="scope-staffer@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE],  # no orders:read
    )
    customer = user_factory(email="scope-customer@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=10, price_usd=20.0)
    own = _order_for(
        db_session, staffer, product=product, shipping_method_id=test_shipping_method_id
    )
    other = _order_for(
        db_session, customer, product=product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.get("/api/v1/orders/", headers=_headers(staffer))
    assert resp.status_code == 200, "a staffer must not be 403'd off their own orders"
    ids = {item["id"] for item in resp.json()["items"]}
    assert own.id in ids
    assert other.id not in ids

    # …and the single-order read is scoped the same way: 404, not 403, so the
    # existence of someone else's order is not leaked.
    assert (
        await client.get(f"/api/v1/orders/{other.id}", headers=_headers(staffer))
    ).status_code == 404


async def test_staffer_with_orders_read_sees_every_order(
    client, db_session, user_factory, product_factory, test_shipping_method_id
):
    """The positive control for the test above — without it, "sees only their
    own" is equally satisfied by a route that returns nothing to anyone."""
    staffer = user_factory(
        email="scope-orders@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[ORDERS_WRITE],
    )
    customer = user_factory(email="scope-customer2@example.com", role=UserRole.CUSTOMER)
    product = product_factory(stock=10, price_usd=20.0)
    other = _order_for(
        db_session, customer, product=product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.get("/api/v1/orders/", headers=_headers(staffer))
    assert resp.status_code == 200
    assert other.id in {item["id"] for item in resp.json()["items"]}
    assert (
        await client.get(f"/api/v1/orders/{other.id}", headers=_headers(staffer))
    ).status_code == 200


async def test_staffer_without_catalog_read_gets_the_public_product_view(
    client, user_factory, product_factory
):
    """``GET /products/`` is the storefront. A staffer lacking ``catalog:read``
    is served the public view rather than 403 — the page must still render."""
    inactive = product_factory(is_active=False, stock=1)

    staffer = user_factory(
        email="scope-catalogless@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[MARKETING_WRITE],  # no catalog:read
    )
    resp = await client.get("/api/v1/products/?is_active=false", headers=_headers(staffer))
    assert resp.status_code == 200, "the storefront must not 403 a logged-in staff member"
    assert inactive.id not in {item["id"] for item in resp.json()["items"]}

    capable = user_factory(
        email="scope-catalog@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE],
    )
    resp = await client.get("/api/v1/products/?is_active=false", headers=_headers(capable))
    assert inactive.id in {item["id"] for item in resp.json()["items"]}
