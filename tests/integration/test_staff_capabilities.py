"""Integration tests for granular staff capabilities (Back-office maturity A1).

Covers the ``require_capability`` boundary on write/DELETE admin routes and the
owner/capability management + escalation guards on ``PATCH /admin/staff/{id}``:

- an owner passes every gated write; a scoped staffer passes only granted caps
  and 403s the rest; reads stay open to any staff
- ``/auth/me`` exposes ``is_owner``, ``capabilities`` and the expanded
  ``effective_capabilities`` so the FE can gate
- staff PATCH: owner grants caps/owner; unknown cap → 422; a non-owner
  ``staff:write`` holder can't touch owner/capabilities nor promote anyone into
  the admin pool (escalation → 403, SEC-022) while demotion and edits to
  existing staff still work; the last active owner can't be removed (400)
- a backfilled existing admin (factory owner) is unchanged (regression)
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy.orm import Session

from app.core.capabilities import (
    CATALOG_READ,
    CATALOG_WRITE,
    CUSTOMERS_READ,
    CUSTOMERS_WRITE,
    MARKETING_READ,
    MARKETING_WRITE,
    STAFF_WRITE,
    TAX_READ,
)
from app.core.security import create_access_token
from app.models.user import User, UserRole

pytestmark = pytest.mark.integration

_tag_seq = itertools.count(1)


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def _create_tag(client, actor):
    """POST a uniquely-named tag as ``actor`` and return the response."""
    name = f"cap-tag-{next(_tag_seq)}"
    return await client.post("/api/v1/admin/tags/", json={"name": name}, headers=_headers(actor))


# ---------------------------------------------------------------------------
# require_capability — write-route gating matrix (POST /admin/tags → customers:write)
# ---------------------------------------------------------------------------


async def test_owner_passes_gated_write(client, user_factory):
    owner = user_factory(email="cap-owner@example.com", role=UserRole.ADMIN)  # owner via factory
    assert owner.is_owner is True
    resp = await _create_tag(client, owner)
    assert resp.status_code == 201


async def test_scoped_staffer_with_capability_passes(client, user_factory):
    staffer = user_factory(
        email="cap-customers@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CUSTOMERS_WRITE],
    )
    resp = await _create_tag(client, staffer)
    assert resp.status_code == 201


async def test_scoped_staffer_without_capability_403(client, user_factory):
    staffer = user_factory(
        email="cap-marketing-only@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[MARKETING_WRITE],  # wrong cap for a customers write
    )
    resp = await _create_tag(client, staffer)
    assert resp.status_code == 403


async def test_customer_403_and_anonymous_401(client, user_factory):
    customer = user_factory(email="cap-cust@example.com", role=UserRole.CUSTOMER)
    assert (await _create_tag(client, customer)).status_code == 403
    anon = await client.post("/api/v1/admin/tags/", json={"name": "cap-anon"})
    assert anon.status_code == 401


async def test_reads_are_capability_scoped_too(client, user_factory):
    """SEC-021 changed this rule. Reads used to be open to any staff; a staffer
    with no capabilities now 403s, and a ``customers:write`` holder still reads
    the tag vocabulary because write implies read (no grant had to change).
    """
    ungranted = user_factory(
        email="cap-reader@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[],
    )
    resp = await client.get("/api/v1/admin/tags/", headers=_headers(ungranted))
    assert resp.status_code == 403

    customers_staffer = user_factory(
        email="cap-reader-customers@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CUSTOMERS_WRITE],
    )
    resp = await client.get("/api/v1/admin/tags/", headers=_headers(customers_staffer))
    assert resp.status_code == 200


async def test_read_only_customers_grant_reads_the_tag_vocabulary(client, user_factory):
    """``/admin/customers`` renders the tag chips, and the sidebar links it on
    ``customers:read`` — so the vocabulary has to be readable from that grant
    alone, with no write capability behind it."""
    reader = user_factory(
        email="cap-customers-reader@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CUSTOMERS_READ],
    )
    resp = await client.get("/api/v1/admin/tags/", headers=_headers(reader))
    assert resp.status_code == 200


async def test_customer_tag_vocabulary_is_not_a_catalog_surface(client, user_factory):
    """Regression for the mis-gate this PR fixes. ``/admin/tags`` is the
    *customer*-tag vocabulary; it was on ``catalog:*`` from the original rollout,
    which 403'd the customers index for the staff who own that page. Product tags
    live at ``/admin/product-tags`` and stay on ``catalog:*``.
    """
    catalog_staffer = user_factory(
        email="cap-catalog-tags@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE],
    )
    assert (await _create_tag(client, catalog_staffer)).status_code == 403
    resp = await client.get("/api/v1/admin/tags/", headers=_headers(catalog_staffer))
    assert resp.status_code == 403

    resp = await client.get("/api/v1/admin/product-tags/", headers=_headers(catalog_staffer))
    assert resp.status_code == 200


async def test_cross_capability_isolation(client, user_factory):
    # customers:write staffer may create tags but NOT create coupons (marketing:write).
    staffer = user_factory(
        email="cap-customers2@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CUSTOMERS_WRITE],
    )
    assert (await _create_tag(client, staffer)).status_code == 201
    coupon = await client.post(
        "/api/v1/admin/coupons/",
        json={
            "code": "CAPX10",
            "discount_type": "percentage",
            "value": 10,
        },
        headers=_headers(staffer),
    )
    assert coupon.status_code == 403


# ---------------------------------------------------------------------------
# /auth/me exposes the gating fields
# ---------------------------------------------------------------------------


async def test_me_exposes_owner_and_capabilities(client, user_factory):
    staffer = user_factory(
        email="cap-me@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE, MARKETING_WRITE],
    )
    resp = await client.get("/api/v1/auth/me", headers=_headers(staffer))
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_owner"] is False
    assert set(body["capabilities"]) == {CATALOG_WRITE, MARKETING_WRITE}
    # The raw grants stay raw — the expansion is an *additional* field, so
    # anything still reading `capabilities` (e.g. the staff edit form, which
    # round-trips the stored set) is unaffected.
    assert set(body["effective_capabilities"]) == {
        CATALOG_WRITE,
        MARKETING_WRITE,
        CATALOG_READ,
        TAX_READ,  # catalog:write reaches the per-product VAT picker
        MARKETING_READ,
    }


async def test_me_effective_capabilities_empty_for_a_customer(client, user_factory):
    customer = user_factory(email="cap-me-customer@example.com")
    resp = await client.get("/api/v1/auth/me", headers=_headers(customer))
    assert resp.status_code == 200
    assert resp.json()["effective_capabilities"] == []


async def test_me_does_not_special_case_the_owner(client, user_factory):
    """An owner's expansion reflects their *grants*, not their bypass.

    Owners hold everything by virtue of ``is_owner`` — ``require_read`` never
    consults their capability array, and the frontend's ``can()`` short-circuits
    on the same flag. Returning the full taxonomy here would be a second, silent
    definition of ownership; pinning the current shape stops someone "fixing"
    that later and coupling the FE's owner handling to this field.
    """
    owner = user_factory(email="cap-me-owner@example.com", role=UserRole.ADMIN, is_owner=True)
    resp = await client.get("/api/v1/auth/me", headers=_headers(owner))
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_owner"] is True
    assert body["effective_capabilities"] == []


# ---------------------------------------------------------------------------
# staff PATCH — owner manages capabilities + is_owner
# ---------------------------------------------------------------------------


async def test_owner_grants_capabilities(client, user_factory, db_session: Session):
    owner = user_factory(email="grantor@example.com", role=UserRole.ADMIN)
    target = user_factory(email="grantee@example.com", role=UserRole.ADMIN, is_owner=False)

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"capabilities": [CATALOG_WRITE, MARKETING_WRITE]},
        headers=_headers(owner),
    )
    assert resp.status_code == 200
    assert set(resp.json()["capabilities"]) == {CATALOG_WRITE, MARKETING_WRITE}
    assert resp.json()["is_owner"] is False

    fresh = db_session.get(User, target.id)
    assert fresh is not None
    db_session.refresh(fresh)
    assert set(fresh.capabilities) == {CATALOG_WRITE, MARKETING_WRITE}


async def test_owner_promotes_to_owner(client, user_factory):
    owner = user_factory(email="grantor2@example.com", role=UserRole.ADMIN)
    target = user_factory(email="new-owner@example.com", role=UserRole.ADMIN, is_owner=False)

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"is_owner": True},
        headers=_headers(owner),
    )
    assert resp.status_code == 200
    assert resp.json()["is_owner"] is True


async def test_unknown_capability_rejected_422(client, user_factory):
    owner = user_factory(email="grantor3@example.com", role=UserRole.ADMIN)
    target = user_factory(email="grantee3@example.com", role=UserRole.ADMIN, is_owner=False)

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"capabilities": ["catalog:write", "bogus:cap"]},
        headers=_headers(owner),
    )
    assert resp.status_code == 422


async def test_demotion_clears_owner_and_capabilities(client, user_factory, db_session: Session):
    owner = user_factory(email="grantor4@example.com", role=UserRole.ADMIN)
    # A second owner so demoting this one is not the last-owner case.
    user_factory(email="safety-owner@example.com", role=UserRole.ADMIN)
    target = user_factory(
        email="demote-owner@example.com",
        role=UserRole.ADMIN,
        is_owner=True,
        capabilities=[CATALOG_WRITE],
    )

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"role": "CUSTOMER"},
        headers=_headers(owner),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "CUSTOMER"
    assert resp.json()["is_owner"] is False
    assert resp.json()["capabilities"] == []


# ---------------------------------------------------------------------------
# staff PATCH — privilege-escalation guard
# ---------------------------------------------------------------------------


async def test_non_owner_staff_writer_cannot_grant_owner(client, user_factory):
    # A scoped staffer WITH staff:write (so passes the route gate) still can't
    # grant the owner flag — that's owner-only.
    staff_writer = user_factory(
        email="staffmgr@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[STAFF_WRITE],
    )
    target = user_factory(
        email="escalation-target@example.com", role=UserRole.ADMIN, is_owner=False
    )

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"is_owner": True},
        headers=_headers(staff_writer),
    )
    assert resp.status_code == 403


async def test_non_owner_staff_writer_cannot_set_capabilities(client, user_factory):
    staff_writer = user_factory(
        email="staffmgr2@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[STAFF_WRITE],
    )
    target = user_factory(
        email="escalation-target2@example.com", role=UserRole.ADMIN, is_owner=False
    )

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"capabilities": [CATALOG_WRITE]},
        headers=_headers(staff_writer),
    )
    assert resp.status_code == 403


async def test_non_owner_staff_writer_cannot_edit_an_owner(client, user_factory):
    staff_writer = user_factory(
        email="staffmgr3@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[STAFF_WRITE],
    )
    # Target is an owner — a non-owner may not touch it even for role/active.
    owner_target = user_factory(email="an-owner@example.com", role=UserRole.ADMIN, is_owner=True)

    resp = await client.patch(
        f"/api/v1/admin/staff/{owner_target.id}",
        json={"is_active": False},
        headers=_headers(staff_writer),
    )
    assert resp.status_code == 403


async def test_non_owner_staff_writer_can_toggle_non_owner_admin(client, user_factory):
    # The scoped staff:write holder CAN still do basic role/active management on
    # non-owner accounts (positive control for the escalation guard).
    staff_writer = user_factory(
        email="staffmgr4@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[STAFF_WRITE],
    )
    # A second admin remains active, so deactivating the target isn't last-admin.
    user_factory(email="net-admin@example.com", role=UserRole.ADMIN)
    target = user_factory(email="toggle-target@example.com", role=UserRole.ADMIN, is_owner=False)

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"is_active": False},
        headers=_headers(staff_writer),
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


async def test_non_owner_staff_writer_cannot_promote_customer_to_admin(
    client, user_factory, db_session: Session
):
    # SEC-022: minting staff is owner-only. A scoped staff:write holder passes
    # the route gate but may not add anyone to the admin pool.
    staff_writer = user_factory(
        email="staffmgr5@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[STAFF_WRITE],
    )
    target = user_factory(email="promo-target@example.com", role=UserRole.CUSTOMER)

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"role": "ADMIN"},
        headers=_headers(staff_writer),
    )
    assert resp.status_code == 403
    assert "owner" in resp.json()["detail"].lower()

    # Assert the effect, not just the status: the row must be untouched.
    db_session.refresh(target)
    assert target.role == UserRole.CUSTOMER


async def test_owner_can_promote_customer_to_admin(client, user_factory, db_session: Session):
    # Positive control for the guard above — the operation itself works, it is
    # only the actor that is restricted.
    owner = user_factory(email="promoting-owner@example.com", role=UserRole.ADMIN, is_owner=True)
    target = user_factory(email="promo-target2@example.com", role=UserRole.CUSTOMER)

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"role": "ADMIN"},
        headers=_headers(owner),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "ADMIN"

    db_session.refresh(target)
    assert target.role == UserRole.ADMIN
    # Minted with nothing granted — the promotion is not itself an escalation.
    assert target.is_owner is False
    assert not (target.capabilities or [])


async def test_non_owner_staff_writer_may_resend_admin_role_on_existing_admin(client, user_factory):
    # Precision control: the guard fires on *promotion*, not on any PATCH that
    # happens to carry role=admin. A staff:write holder editing an account that
    # is already staff must keep working.
    staff_writer = user_factory(
        email="staffmgr6@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[STAFF_WRITE],
    )
    target = user_factory(email="already-admin@example.com", role=UserRole.ADMIN, is_owner=False)

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"role": "ADMIN", "is_active": True},
        headers=_headers(staff_writer),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "ADMIN"


async def test_non_owner_staff_writer_can_still_demote_an_admin(client, user_factory):
    # Demotion is not restricted — only minting is. (A second active admin keeps
    # the last-active-admin guard out of the way.)
    staff_writer = user_factory(
        email="staffmgr7@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[STAFF_WRITE],
    )
    user_factory(email="spare-admin@example.com", role=UserRole.ADMIN)
    target = user_factory(email="demote-target@example.com", role=UserRole.ADMIN, is_owner=False)

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"role": "CUSTOMER"},
        headers=_headers(staff_writer),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "CUSTOMER"


# ---------------------------------------------------------------------------
# staff PATCH — last-owner guard
# ---------------------------------------------------------------------------


async def test_cannot_remove_last_active_owner(client, user_factory):
    # Only one active owner exists; clearing its owner flag is refused.
    owner = user_factory(email="lonely-owner@example.com", role=UserRole.ADMIN, is_owner=True)
    # Another admin exists but is NOT an owner — doesn't count toward the owner pool.
    user_factory(email="plain-admin@example.com", role=UserRole.ADMIN, is_owner=False)

    resp = await client.patch(
        f"/api/v1/admin/staff/{owner.id}",
        json={"is_owner": False},
        headers=_headers(owner),
    )
    assert resp.status_code == 400
    assert "owner" in resp.json()["detail"].lower()


async def test_remove_owner_when_another_owner_exists(client, user_factory):
    owner = user_factory(email="owner-a@example.com", role=UserRole.ADMIN, is_owner=True)
    target = user_factory(email="owner-b@example.com", role=UserRole.ADMIN, is_owner=True)

    resp = await client.patch(
        f"/api/v1/admin/staff/{target.id}",
        json={"is_owner": False},
        headers=_headers(owner),
    )
    assert resp.status_code == 200
    assert resp.json()["is_owner"] is False


# ---------------------------------------------------------------------------
# count_active_owners — the last-owner guard's CRUD helper (focused unit)
# ---------------------------------------------------------------------------


async def test_count_active_owners(user_factory, db_session: Session):
    from app.crud import user as crud_user

    o1 = user_factory(email="co-1@example.com", role=UserRole.ADMIN, is_owner=True)
    o2 = user_factory(email="co-2@example.com", role=UserRole.ADMIN, is_owner=True)
    user_factory(
        email="co-inactive@example.com", role=UserRole.ADMIN, is_owner=True, is_active=False
    )
    user_factory(email="co-plain-admin@example.com", role=UserRole.ADMIN, is_owner=False)

    assert crud_user.count_active_owners(db_session) == 2
    assert crud_user.count_active_owners(db_session, exclude_id=o1.id) == 1
    assert crud_user.count_active_owners(db_session, exclude_id=o2.id) == 1
