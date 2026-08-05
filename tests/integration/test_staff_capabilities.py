"""Integration tests for granular staff capabilities (Back-office maturity A1).

Covers the ``require_capability`` boundary on write/DELETE admin routes and the
owner/capability management + escalation guards on ``PATCH /admin/staff/{id}``:

- an owner passes every gated write; a scoped staffer passes only granted caps
  and 403s the rest; reads stay open to any staff
- ``/auth/me`` exposes ``is_owner`` + ``capabilities`` so the FE can gate
- staff PATCH: owner grants caps/owner; unknown cap → 422; a non-owner
  ``staff:write`` holder can't touch owner/capabilities (escalation → 403);
  the last active owner can't be removed (400)
- a backfilled existing admin (factory owner) is unchanged (regression)
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy.orm import Session

from app.core.capabilities import (
    CATALOG_WRITE,
    MARKETING_WRITE,
    STAFF_WRITE,
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
# require_capability — write-route gating matrix (POST /admin/tags → catalog:write)
# ---------------------------------------------------------------------------


async def test_owner_passes_gated_write(client, user_factory):
    owner = user_factory(email="cap-owner@example.com", role=UserRole.ADMIN)  # owner via factory
    assert owner.is_owner is True
    resp = await _create_tag(client, owner)
    assert resp.status_code == 201


async def test_scoped_staffer_with_capability_passes(client, user_factory):
    staffer = user_factory(
        email="cap-catalog@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE],
    )
    resp = await _create_tag(client, staffer)
    assert resp.status_code == 201


async def test_scoped_staffer_without_capability_403(client, user_factory):
    staffer = user_factory(
        email="cap-marketing-only@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[MARKETING_WRITE],  # wrong cap for a catalog write
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
    with no capabilities now 403s, and a ``catalog:write`` holder still reads
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

    catalog_staffer = user_factory(
        email="cap-reader-catalog@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE],
    )
    resp = await client.get("/api/v1/admin/tags/", headers=_headers(catalog_staffer))
    assert resp.status_code == 200


async def test_cross_capability_isolation(client, user_factory):
    # catalog:write staffer may create tags but NOT create coupons (marketing:write).
    staffer = user_factory(
        email="cap-catalog2@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE],
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
