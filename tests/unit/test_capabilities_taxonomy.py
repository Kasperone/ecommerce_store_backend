"""The read-capability taxonomy and its write→read implication algebra (SEC-021).

Phase 1 of the RBAC read-scoping program: the taxonomy and the two gates
(``require_read`` for admin-only routes, ``staff_can_read`` for dual-purpose /
public routes) are tested here in isolation — no routes are annotated yet.

The invariants below are the ones that make the taxonomy safe to roll out:

- **write implies read**, so no stored grant set loses a surface it has today
  and no backfill migration is needed
- the implication map is **closed**: every value is a read capability, and
  ``effective_capabilities`` takes its transitive closure, so a write reaches
  the cross-module reads hanging off the read it implies
- every read capability is either reachable from a write capability or a
  **deliberate** standalone grant — a new read cap that is neither fails here
  rather than silently locking a surface to owners

Phase 5 moved the cross-module edges from the write capabilities onto the
*reads* they belong to, because a read-only grant is a real grant now: the
sidebar links every section on ``<domain>:read``, so an ``orders:read`` staffer
reaches ``/admin/draft-orders/{id}`` and it prices from the shipping methods.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core import capabilities as caps
from app.core.capabilities import (
    AUDIT_READ,
    CAPABILITIES,
    CATALOG_READ,
    CATALOG_WRITE,
    COMPANIES_READ,
    CONTENT_WRITE,
    CUSTOMERS_READ,
    CUSTOMERS_WRITE,
    IMPLIED_READS,
    ORDERS_READ,
    ORDERS_REFUND,
    ORDERS_WRITE,
    READ_CAPABILITIES,
    REPORTS_READ,
    SETTINGS_READ,
    SETTINGS_WRITE,
    SHIPPING_READ,
    STAFF_READ,
    STAFF_WRITE,
    STANDALONE_READS,
    TAX_READ,
    WRITE_CAPABILITIES,
    effective_capabilities,
)
from app.core.dependencies import require_read, staff_can_read
from app.models.user import User, UserRole


def _staff(*, is_owner: bool = False, capabilities: list[str] | None = None) -> User:
    """An unsaved staff row — these gates never touch the DB."""
    return User(
        email="staffer@example.com",
        role=UserRole.ADMIN,
        is_owner=is_owner,
        capabilities=capabilities,
    )


# ---------------------------------------------------------------------------
# Taxonomy shape
# ---------------------------------------------------------------------------


def test_capabilities_is_the_union_of_the_two_halves():
    assert CAPABILITIES == WRITE_CAPABILITIES | READ_CAPABILITIES


def test_read_and_write_halves_are_disjoint():
    assert not (WRITE_CAPABILITIES & READ_CAPABILITIES)


def test_every_capability_is_namespaced_by_its_half():
    assert all(cap.endswith(":read") for cap in READ_CAPABILITIES)
    # `orders:refund` is the one write capability that is not `<domain>:write` —
    # it is a narrower slice of the orders domain, not a separate domain.
    assert {cap for cap in WRITE_CAPABILITIES if not cap.endswith(":write")} == {ORDERS_REFUND}


def test_every_exported_capability_constant_is_in_the_closed_set():
    """A constant that is not in ``CAPABILITIES`` can never be granted, so a
    route gated on it would be owner-only forever."""
    exported = {
        value
        for name, value in vars(caps).items()
        if name.isupper() and isinstance(value, str) and ":" in value
    }
    assert exported == CAPABILITIES


# ---------------------------------------------------------------------------
# The implication map
# ---------------------------------------------------------------------------


def test_every_write_capability_is_a_key_of_the_implication_map():
    """A write capability missing from the map would stop implying its own
    domain's read — the no-migration guarantee, silently withdrawn."""
    assert set(IMPLIED_READS) >= WRITE_CAPABILITIES


def test_implication_map_keys_are_capabilities():
    """Read keys are allowed (they carry the cross-module edges); a key outside
    the taxonomy is a typo that can never fire."""
    assert set(IMPLIED_READS) <= CAPABILITIES


def test_implication_map_values_are_read_capabilities():
    implied = frozenset().union(*IMPLIED_READS.values())
    assert implied <= READ_CAPABILITIES


def test_a_write_capability_implies_only_its_own_domain_read_directly():
    """Cross-module reads belong on the *read* key, so they reach a read-only
    grant too. A write entry naming a second domain means someone added an edge
    to the write side, where an ``orders:read`` staffer can't see it."""
    for write_cap in WRITE_CAPABILITIES:
        domain = write_cap.split(":")[0]
        assert IMPLIED_READS[write_cap] == frozenset({f"{domain}:read"})


def test_expansion_is_the_transitive_closure_of_the_map():
    """The map holds direct edges only; ``effective_capabilities`` must follow
    them to exhaustion, or a cross-module read hanging off a read capability
    would never reach the write capability above it."""
    every_cap = effective_capabilities(CAPABILITIES)
    assert effective_capabilities(every_cap) == every_cap
    for cap, direct in IMPLIED_READS.items():
        expanded = effective_capabilities([cap])
        assert direct <= expanded
        for implied in direct:
            assert effective_capabilities([implied]) <= expanded


@pytest.mark.parametrize("write_cap", sorted(WRITE_CAPABILITIES))
def test_every_write_capability_implies_its_own_domain_read(write_cap: str):
    """The no-migration guarantee: holding ``x:write`` keeps ``x:read``."""
    own_domain_read = f"{write_cap.split(':')[0]}:read"
    assert own_domain_read in effective_capabilities([write_cap])


def test_standalone_reads_are_exactly_the_reads_no_write_implies():
    """A read capability that no write implies is owner-only until granted by
    hand. That is a deliberate choice for reports/audit — anything else showing
    up here is an oversight."""
    implied = frozenset().union(*IMPLIED_READS.values())
    assert READ_CAPABILITIES - implied == STANDALONE_READS


def test_dashboard_and_audit_reads_are_not_granted_by_any_write_capability():
    """Explicit form of the tightening SEC-021 asks for: before this change the
    dashboard, reports and audit log carried no capability at all."""
    every_write = effective_capabilities(WRITE_CAPABILITIES)
    assert REPORTS_READ not in every_write
    assert AUDIT_READ not in every_write


# ---------------------------------------------------------------------------
# Cross-module reads found by the Phase 0 call-graph inventory
# ---------------------------------------------------------------------------


def test_order_staff_can_read_companies_and_shipping():
    """/admin/quotes picks a company; /admin/draft-orders prices with shipping
    methods. A read gate keyed to the matching write cap alone breaks both."""
    granted = effective_capabilities([ORDERS_WRITE])
    assert COMPANIES_READ in granted
    assert SHIPPING_READ in granted


def test_catalog_staff_can_read_tax_rates():
    """The per-product VAT-rate picker on /admin/products/new."""
    assert TAX_READ in effective_capabilities([CATALOG_WRITE])


def test_settings_staff_can_read_shipping_and_tax():
    granted = effective_capabilities([SETTINGS_WRITE])
    assert {SHIPPING_READ, TAX_READ} <= granted


def test_customer_staff_can_read_companies():
    assert COMPANIES_READ in effective_capabilities([CUSTOMERS_WRITE])


def test_cross_module_reads_do_not_leak_the_sensitive_surfaces():
    """The over-grant this taxonomy is shaped to avoid: an order staffer reads
    companies and shipping, but not the customer database or store config."""
    granted = effective_capabilities([ORDERS_WRITE])
    assert CUSTOMERS_READ not in granted
    assert SETTINGS_READ not in granted


def test_refund_capability_reaches_the_orders_reads():
    """``orders:refund`` implies ``orders:read`` and therefore, transitively,
    everything an order surface reads. Widened in Phase 5 — before the closure
    a refunds staffer stopped at ``orders:read`` and would have hit the same
    empty shipping picker on a draft order."""
    assert effective_capabilities([ORDERS_REFUND]) == {
        ORDERS_REFUND,
        ORDERS_READ,
        COMPANIES_READ,
        SHIPPING_READ,
    }


# ---------------------------------------------------------------------------
# The same cross-module reads, reached from a read-only grant (Phase 5)
# ---------------------------------------------------------------------------


def test_read_only_order_staff_can_read_companies_and_shipping():
    """The gap Phase 5 closes: the sidebar links Orders on ``orders:read``, so a
    staffer can reach ``/admin/draft-orders/{id}`` — a viewable detail page —
    with no write capability at all. Its shipping-method query has no error
    branch, so a 403 there renders an empty picker and says nothing."""
    granted = effective_capabilities([ORDERS_READ])
    assert {COMPANIES_READ, SHIPPING_READ} <= granted


def test_read_only_catalog_staff_can_read_tax_rates():
    assert TAX_READ in effective_capabilities([CATALOG_READ])


def test_read_only_customer_staff_can_read_companies():
    assert COMPANIES_READ in effective_capabilities([CUSTOMERS_READ])


def test_read_only_settings_staff_can_read_shipping_and_tax():
    assert {SHIPPING_READ, TAX_READ} <= effective_capabilities([SETTINGS_READ])


def test_a_read_only_grant_never_reaches_a_write_capability():
    """Closure over reads must not walk back up. Every capability reachable from
    a read grant is itself a read."""
    for read_cap in READ_CAPABILITIES:
        assert effective_capabilities([read_cap]) <= READ_CAPABILITIES


def test_read_only_grants_do_not_leak_the_sensitive_surfaces():
    """The read half stays finer than the write half: reading orders does not
    hand over the customer database or store config."""
    granted = effective_capabilities([ORDERS_READ])
    assert CUSTOMERS_READ not in granted
    assert SETTINGS_READ not in granted


def test_staff_write_does_not_imply_reading_anything_else():
    """Managing staff is not a licence to read the store."""
    assert effective_capabilities([STAFF_WRITE]) == {STAFF_WRITE, STAFF_READ}


# ---------------------------------------------------------------------------
# effective_capabilities — edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty", [None, [], set()])
def test_no_grants_expands_to_nothing(empty):
    assert effective_capabilities(empty) == frozenset()


def test_expansion_preserves_the_original_grants():
    granted = [ORDERS_WRITE, CUSTOMERS_WRITE]
    assert set(granted) <= effective_capabilities(granted)


def test_unknown_capability_is_passed_through_but_implies_nothing():
    """Stored grants are schema-validated ⊆ the taxonomy, so this is defensive:
    an unrecognised string must not crash the expansion or grant anything."""
    assert effective_capabilities(["totally:bogus"]) == {"totally:bogus"}


def test_expansion_is_idempotent():
    once = effective_capabilities([CATALOG_WRITE, ORDERS_REFUND])
    assert effective_capabilities(once) == once


# ---------------------------------------------------------------------------
# staff_can_read — the dual-purpose / public-route gate
# ---------------------------------------------------------------------------


def test_owner_can_read_every_domain():
    owner = _staff(is_owner=True)
    assert all(staff_can_read(owner, cap) for cap in READ_CAPABILITIES)


def test_scoped_staffer_can_read_only_what_they_hold_or_imply():
    staffer = _staff(capabilities=[CATALOG_WRITE])
    assert staff_can_read(staffer, CATALOG_READ)
    assert staff_can_read(staffer, TAX_READ)
    assert not staff_can_read(staffer, ORDERS_READ)
    assert not staff_can_read(staffer, CUSTOMERS_READ)


def test_explicit_read_grant_works_without_the_write_capability():
    """An analyst gets reports without any write capability."""
    analyst = _staff(capabilities=[REPORTS_READ])
    assert staff_can_read(analyst, REPORTS_READ)
    assert not staff_can_read(analyst, CUSTOMERS_READ)


def test_staffer_with_no_grants_reads_nothing():
    staffer = _staff(capabilities=None)
    assert not any(staff_can_read(staffer, cap) for cap in READ_CAPABILITIES)


def test_customer_never_takes_the_widened_branch():
    customer = User(email="shopper@example.com", role=UserRole.CUSTOMER, capabilities=[ORDERS_READ])
    assert not staff_can_read(customer, ORDERS_READ)


def test_anonymous_caller_never_takes_the_widened_branch():
    assert not staff_can_read(None, ORDERS_READ)


# ---------------------------------------------------------------------------
# require_read — the admin-only-route gate
# ---------------------------------------------------------------------------


async def test_require_read_returns_the_user_when_the_capability_is_implied():
    staffer = _staff(capabilities=[ORDERS_WRITE])
    dep = require_read(SHIPPING_READ)
    assert await dep(staffer) is staffer


async def test_require_read_403s_a_staffer_without_the_capability():
    staffer = _staff(capabilities=[CONTENT_WRITE])
    dep = require_read(CUSTOMERS_READ)
    with pytest.raises(HTTPException) as exc:
        await dep(staffer)
    assert exc.value.status_code == 403


async def test_require_read_lets_an_owner_through_any_gate():
    owner = _staff(is_owner=True)
    for cap in sorted(READ_CAPABILITIES):
        assert await require_read(cap)(owner) is owner
