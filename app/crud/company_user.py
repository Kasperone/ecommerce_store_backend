"""CompanyUser CRUD — B2B company membership (the company portal foundation).

Wave D / B2B-2 slice 3. Resolves the caller's company + role from the
``company_users`` membership table, with a **backward-compatible fallback** to
the denormalized ``companies.primary_user_id`` pointer: a user who is a
company's primary contact but has no explicit membership row resolves
identically as an ``active`` ``COMPANY_ADMIN`` (per D-D-1 the primary contact IS
the COMPANY_ADMIN, and the migration backfills exactly that row for
already-existing data — the fallback covers rows created after the cutover, e.g.
admin-set primary contacts that never went through the membership path).

No commit anywhere (the route owns the transaction). Foundational helpers
(``get_active_membership`` / ``list_for_company`` / ``get_membership`` /
``create``) are what CP-be (company profile) and TM-be (team management) build
on.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_user import CompanyMemberStatus, CompanyRole, CompanyUser


@dataclass(frozen=True)
class CompanyMembership:
    """The resolved membership context for a caller: their company + B2B role.

    ``company_user`` is the concrete ``company_users`` row when one exists, or
    ``None`` when the context was synthesized from the denormalized
    ``companies.primary_user_id`` pointer (a primary contact with no explicit
    membership row — resolves as ``COMPANY_ADMIN``). Callers that only need
    "which company + what role" use ``company`` / ``role`` and never have to care
    which path resolved it.
    """

    company: Company
    role: str
    company_user: CompanyUser | None


class CRUDCompanyUser:
    """Company membership rows (no commit; caller owns the txn)."""

    def get_active_membership(self, db: Session, *, user_id: int) -> CompanyMembership | None:
        """The caller's single active membership (company + role), else ``None``.

        Resolution order (D-D-2):

        1. An ``active`` ``company_users`` row for ``user_id`` (newest wins if a
           user somehow has several — the MVP has at most one per company and a
           user belongs to one company, so this is defensive ordering only).
        2. **Fallback** — the company whose ``primary_user_id == user_id``,
           synthesized as a ``COMPANY_ADMIN`` context (the backfill makes a
           pre-existing primary contact resolve identically; the fallback covers
           contacts created after the migration).

        ``None`` means "not a B2B account" — the caller turns that into a 404 on
        ``/companies/me`` (and the FE gates the B2B surfaces on it).
        """
        membership = (
            db.query(CompanyUser)
            .filter(
                CompanyUser.user_id == user_id,
                CompanyUser.status == CompanyMemberStatus.ACTIVE.value,
            )
            .order_by(CompanyUser.id.desc())
            .first()
        )
        if membership is not None:
            return CompanyMembership(
                company=membership.company,
                role=membership.role,
                company_user=membership,
            )

        # Fallback: the denormalized primary-contact pointer → COMPANY_ADMIN.
        company = db.query(Company).filter(Company.primary_user_id == user_id).first()
        if company is not None:
            return CompanyMembership(
                company=company,
                role=CompanyRole.COMPANY_ADMIN.value,
                company_user=None,
            )
        return None

    def get_membership(self, db: Session, *, company_id: int, user_id: int) -> CompanyUser | None:
        """The membership row for ``(company_id, user_id)`` regardless of status.

        ``None`` when the user is not a member of that company (TM-be uses this to
        404 on a non-member and to enforce the unique ``(company_id, user_id)``
        before inserting an invite)."""
        return (
            db.query(CompanyUser)
            .filter(
                CompanyUser.company_id == company_id,
                CompanyUser.user_id == user_id,
            )
            .first()
        )

    def list_for_company(self, db: Session, *, company_id: int) -> list[CompanyUser]:
        """All membership rows for a company (newest first), users eager-loaded.

        Foundational for TM-be's team-list route."""
        return (
            db.query(CompanyUser)
            .filter(CompanyUser.company_id == company_id)
            .order_by(CompanyUser.id.desc())
            .all()
        )

    def count_active_admins(self, db: Session, *, company_id: int) -> int:
        """Number of ``active`` ``COMPANY_ADMIN`` memberships for a company.

        Foundational for a last-admin guard (TM-be) — don't let the final
        COMPANY_ADMIN demote/remove themselves and orphan the company."""
        return (
            db.query(CompanyUser)
            .filter(
                CompanyUser.company_id == company_id,
                CompanyUser.role == CompanyRole.COMPANY_ADMIN.value,
                CompanyUser.status == CompanyMemberStatus.ACTIVE.value,
            )
            .count()
        )

    def create(
        self,
        db: Session,
        *,
        company_id: int,
        user_id: int,
        role: str = CompanyRole.COMPANY_ADMIN.value,
        status: str = CompanyMemberStatus.ACTIVE.value,
        invited_email: str | None = None,
    ) -> CompanyUser:
        """Stage a new membership (id populated via flush; caller commits).

        Used by the business-signup flow (CP-be) and team invites (TM-be). The
        unique ``(company_id, user_id)`` constraint guards duplicates at the DB."""
        membership = CompanyUser(
            company_id=company_id,
            user_id=user_id,
            role=role,
            status=status,
            invited_email=invited_email,
        )
        db.add(membership)
        db.flush()
        db.refresh(membership)
        return membership


company_user = CRUDCompanyUser()
