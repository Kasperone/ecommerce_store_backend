"""Integration tests for the company team-management API
(``/api/v1/companies/me/users``, TM-be / Wave D slice 5).

Covers the spec's acceptance contract:

- list the roster by an active member;
- invite by COMPANY_ADMIN → 201 ``invited`` row linked to an existing user;
- invite / patch / delete by a BUYER or VIEWER → 403 (member, not admin);
- a non-member → 404 on every route (B2B surface doesn't exist for them);
- the last active COMPANY_ADMIN can't be demoted / suspended / removed → 409;
- a cross-company ``member_id`` → 404 (never act on another company's row);
- suspending a member revokes their access (they 404 on the B2B surfaces);
- invite of an unknown email → 404; invite of an existing member → 409.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company_user import CompanyMemberStatus, CompanyRole
from app.models.user import UserRole
from tests.factories.company import CompanyFactory
from tests.factories.company_user import CompanyUserFactory

pytestmark = pytest.mark.integration

BASE = "/api/v1/companies/me/users"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


@pytest.fixture
def company_user_factory(db_session: Session):
    CompanyUserFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyUserFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyUserFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# GET /companies/me/users — list the roster
# ---------------------------------------------------------------------------


async def test_list_requires_auth(client):
    assert (await client.get(BASE)).status_code == 401


async def test_list_404_for_non_member(client, user_factory):
    user = user_factory(email="team-none@example.com", role=UserRole.CUSTOMER)
    resp = await client.get(BASE, headers=_headers(user))
    assert resp.status_code == 404


async def test_member_can_list_roster(client, user_factory, company_factory, company_user_factory):
    admin = user_factory(email="team-admin@example.com", role=UserRole.CUSTOMER)
    buyer = user_factory(email="team-buyer@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Roster Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    company_user_factory(company_id=company.id, user_id=buyer.id, role=CompanyRole.BUYER.value)

    # A non-admin active member may read.
    resp = await client.get(BASE, headers=_headers(buyer))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    by_user = {row["user_id"]: row for row in body}
    assert by_user[admin.id]["role"] == "COMPANY_ADMIN"
    assert by_user[admin.id]["user_email"] == "team-admin@example.com"
    assert by_user[buyer.id]["role"] == "BUYER"


async def test_list_is_company_scoped(client, user_factory, company_factory, company_user_factory):
    """The roster only contains the caller's own company's members."""
    admin = user_factory(email="scope-admin@example.com", role=UserRole.CUSTOMER)
    other = user_factory(email="scope-other@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Scope Co")
    other_company = company_factory(name="Other Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    company_user_factory(
        company_id=other_company.id, user_id=other.id, role=CompanyRole.COMPANY_ADMIN.value
    )

    body = (await client.get(BASE, headers=_headers(admin))).json()
    assert [row["user_id"] for row in body] == [admin.id]


# ---------------------------------------------------------------------------
# POST /companies/me/users — invite
# ---------------------------------------------------------------------------


async def test_admin_invites_existing_user(
    client, user_factory, company_factory, company_user_factory
):
    admin = user_factory(email="inv-admin@example.com", role=UserRole.CUSTOMER)
    invitee = user_factory(email="inv-invitee@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Invite Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )

    resp = await client.post(
        BASE,
        headers=_headers(admin),
        json={"email": "inv-invitee@example.com", "role": "BUYER"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["user_id"] == invitee.id
    assert body["role"] == "BUYER"
    assert body["status"] == CompanyMemberStatus.INVITED.value
    assert body["invited_email"] == "inv-invitee@example.com"
    assert body["invited_at"] is not None
    assert body["company_id"] == company.id


async def test_invite_normalises_email_case(
    client, user_factory, company_factory, company_user_factory
):
    admin = user_factory(email="case-admin@example.com", role=UserRole.CUSTOMER)
    invitee = user_factory(email="case-invitee@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Case Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )

    resp = await client.post(
        BASE,
        headers=_headers(admin),
        json={"email": "  CASE-Invitee@Example.com ", "role": "VIEWER"},
    )
    assert resp.status_code == 201
    assert resp.json()["user_id"] == invitee.id


async def test_invite_unknown_email_404(
    client, user_factory, company_factory, company_user_factory
):
    admin = user_factory(email="u404-admin@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Unknown Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )

    resp = await client.post(
        BASE,
        headers=_headers(admin),
        json={"email": "nobody@example.com", "role": "BUYER"},
    )
    assert resp.status_code == 404


async def test_invite_existing_member_409(
    client, user_factory, company_factory, company_user_factory
):
    admin = user_factory(email="dup-admin@example.com", role=UserRole.CUSTOMER)
    member = user_factory(email="dup-member@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Dup Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    company_user_factory(company_id=company.id, user_id=member.id, role=CompanyRole.BUYER.value)

    resp = await client.post(
        BASE,
        headers=_headers(admin),
        json={"email": "dup-member@example.com", "role": "VIEWER"},
    )
    assert resp.status_code == 409


@pytest.mark.parametrize("role", [CompanyRole.BUYER.value, CompanyRole.VIEWER.value])
async def test_invite_forbidden_for_non_admin(
    client, user_factory, company_factory, company_user_factory, role
):
    member = user_factory(email=f"403inv-{role}@example.com", role=UserRole.CUSTOMER)
    invitee = user_factory(email=f"403inv-target-{role}@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name=f"403 Invite {role}")
    company_user_factory(company_id=company.id, user_id=member.id, role=role)

    resp = await client.post(
        BASE,
        headers=_headers(member),
        json={"email": invitee.email, "role": "BUYER"},
    )
    assert resp.status_code == 403


async def test_invite_404_for_non_member(client, user_factory):
    stranger = user_factory(email="inv-stranger@example.com", role=UserRole.CUSTOMER)
    resp = await client.post(
        BASE,
        headers=_headers(stranger),
        json={"email": "whoever@example.com", "role": "BUYER"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /companies/me/users/{id} — role / status change
# ---------------------------------------------------------------------------


async def test_admin_promotes_member(client, user_factory, company_factory, company_user_factory):
    admin = user_factory(email="promo-admin@example.com", role=UserRole.CUSTOMER)
    buyer = user_factory(email="promo-buyer@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Promote Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    member = company_user_factory(
        company_id=company.id, user_id=buyer.id, role=CompanyRole.BUYER.value
    )

    resp = await client.patch(
        f"{BASE}/{member.id}",
        headers=_headers(admin),
        json={"role": "COMPANY_ADMIN"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "COMPANY_ADMIN"


async def test_admin_suspends_member(client, user_factory, company_factory, company_user_factory):
    admin = user_factory(email="susp-admin@example.com", role=UserRole.CUSTOMER)
    buyer = user_factory(email="susp-buyer@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Suspend Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    member = company_user_factory(
        company_id=company.id, user_id=buyer.id, role=CompanyRole.BUYER.value
    )

    resp = await client.patch(
        f"{BASE}/{member.id}",
        headers=_headers(admin),
        json={"status": "suspended"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"

    # The suspended member loses access to the B2B surfaces (no active membership).
    lost = await client.get(BASE, headers=_headers(buyer))
    assert lost.status_code == 404


async def test_patch_empty_body_422(client, user_factory, company_factory, company_user_factory):
    admin = user_factory(email="empty-admin@example.com", role=UserRole.CUSTOMER)
    buyer = user_factory(email="empty-buyer@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Empty Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    member = company_user_factory(
        company_id=company.id, user_id=buyer.id, role=CompanyRole.BUYER.value
    )

    resp = await client.patch(f"{BASE}/{member.id}", headers=_headers(admin), json={})
    assert resp.status_code == 422


async def test_patch_cross_company_404(client, user_factory, company_factory, company_user_factory):
    admin = user_factory(email="xc-admin@example.com", role=UserRole.CUSTOMER)
    other_admin = user_factory(email="xc-other@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="XC Co")
    other_company = company_factory(name="XC Other Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    other_member = company_user_factory(
        company_id=other_company.id, user_id=other_admin.id, role=CompanyRole.BUYER.value
    )

    resp = await client.patch(
        f"{BASE}/{other_member.id}",
        headers=_headers(admin),
        json={"role": "VIEWER"},
    )
    assert resp.status_code == 404


async def test_patch_forbidden_for_non_admin(
    client, user_factory, company_factory, company_user_factory
):
    buyer = user_factory(email="403patch-buyer@example.com", role=UserRole.CUSTOMER)
    other = user_factory(email="403patch-other@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="403 Patch Co")
    company_user_factory(company_id=company.id, user_id=buyer.id, role=CompanyRole.BUYER.value)
    target = company_user_factory(
        company_id=company.id, user_id=other.id, role=CompanyRole.VIEWER.value
    )

    resp = await client.patch(
        f"{BASE}/{target.id}", headers=_headers(buyer), json={"role": "BUYER"}
    )
    assert resp.status_code == 403


async def test_cannot_demote_last_admin(
    client, user_factory, company_factory, company_user_factory
):
    admin = user_factory(email="last-demote@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Last Demote Co")
    member = company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )

    resp = await client.patch(
        f"{BASE}/{member.id}",
        headers=_headers(admin),
        json={"role": "BUYER"},
    )
    assert resp.status_code == 409


async def test_cannot_suspend_last_admin(
    client, user_factory, company_factory, company_user_factory
):
    admin = user_factory(email="last-suspend@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Last Suspend Co")
    member = company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )

    resp = await client.patch(
        f"{BASE}/{member.id}",
        headers=_headers(admin),
        json={"status": "suspended"},
    )
    assert resp.status_code == 409


async def test_can_demote_admin_when_another_admin_exists(
    client, user_factory, company_factory, company_user_factory
):
    admin1 = user_factory(email="demote-ok-1@example.com", role=UserRole.CUSTOMER)
    admin2 = user_factory(email="demote-ok-2@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Demote OK Co")
    company_user_factory(
        company_id=company.id, user_id=admin1.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    member2 = company_user_factory(
        company_id=company.id, user_id=admin2.id, role=CompanyRole.COMPANY_ADMIN.value
    )

    resp = await client.patch(
        f"{BASE}/{member2.id}",
        headers=_headers(admin1),
        json={"role": "BUYER"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "BUYER"


# ---------------------------------------------------------------------------
# DELETE /companies/me/users/{id} — remove
# ---------------------------------------------------------------------------


async def test_admin_removes_member(client, user_factory, company_factory, company_user_factory):
    admin = user_factory(email="rm-admin@example.com", role=UserRole.CUSTOMER)
    buyer = user_factory(email="rm-buyer@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Remove Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    member = company_user_factory(
        company_id=company.id, user_id=buyer.id, role=CompanyRole.BUYER.value
    )

    resp = await client.delete(f"{BASE}/{member.id}", headers=_headers(admin))
    assert resp.status_code == 204

    # Gone from the roster.
    body = (await client.get(BASE, headers=_headers(admin))).json()
    assert member.id not in [row["id"] for row in body]


async def test_cannot_remove_last_admin(
    client, user_factory, company_factory, company_user_factory
):
    admin = user_factory(email="rm-last@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Remove Last Co")
    member = company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )

    resp = await client.delete(f"{BASE}/{member.id}", headers=_headers(admin))
    assert resp.status_code == 409


async def test_delete_cross_company_404(
    client, user_factory, company_factory, company_user_factory
):
    admin = user_factory(email="rmxc-admin@example.com", role=UserRole.CUSTOMER)
    other = user_factory(email="rmxc-other@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="RM XC Co")
    other_company = company_factory(name="RM XC Other Co")
    company_user_factory(
        company_id=company.id, user_id=admin.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    other_member = company_user_factory(
        company_id=other_company.id, user_id=other.id, role=CompanyRole.BUYER.value
    )

    resp = await client.delete(f"{BASE}/{other_member.id}", headers=_headers(admin))
    assert resp.status_code == 404


async def test_delete_forbidden_for_non_admin(
    client, user_factory, company_factory, company_user_factory
):
    viewer = user_factory(email="403del-viewer@example.com", role=UserRole.CUSTOMER)
    other = user_factory(email="403del-other@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="403 Del Co")
    company_user_factory(company_id=company.id, user_id=viewer.id, role=CompanyRole.VIEWER.value)
    target = company_user_factory(
        company_id=company.id, user_id=other.id, role=CompanyRole.BUYER.value
    )

    resp = await client.delete(f"{BASE}/{target.id}", headers=_headers(viewer))
    assert resp.status_code == 403
