"""Audit translation completeness across translatable resources (pre-deploy gate).

    python -m app.scripts.audit_translations [--locale pl]
                                             [--resource-type shipping_method ...]
                                             [--all]
                                             [--purge-orphans]

Scans **active** resources via the translatable-resource registry
(:mod:`app.services.translation_registry`) and reports any whose source-present
fields lack a translation in the target locale. Exits non-zero when any resource
is incomplete, so CI / scripted provisioning fails loudly before a deploy ships
English copy on ``/pl/`` via the EN fallback.

Default scope is ``shipping_method`` — the finite, must-be-100%-translated set
that motivated this gate: an admin-added method with a blank ``name_pl`` renders
English on ``/pl/`` checkout silently (the original bug; see BE #179). Widen with
``--resource-type product`` (repeatable) or ``--all`` to gate products,
categories and collections too.

The audit logic lives in :func:`run_audit`, which takes an explicit ``Session``
so it is unit-testable against a constructed dataset; :func:`main` wires up
argument parsing, opens the configured ``SessionLocal``, and maps the report to
an exit code.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.services.translation_admin import purge_orphan_translation_meta
from app.services.translation_registry import (
    TRANSLATABLE_RESOURCES,
    TRANSLATION_LOCALES,
    get_resource_def,
    missing_fields,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DEFAULT_RESOURCE_TYPES: tuple[str, ...] = ("shipping_method",)


@dataclass
class IncompleteResource:
    """One resource row missing at least one translation."""

    resource_type: str
    id: int
    label: str
    missing_field_keys: list[str]


@dataclass
class AuditReport:
    """Result of an audit run over one locale and a set of resource types."""

    locale: str
    resource_types: list[str]
    scanned: int = 0
    incomplete: list[IncompleteResource] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no scanned resource is missing a translation."""
        return not self.incomplete


def run_audit(
    db: Session,
    *,
    locale: str,
    resource_types: list[str],
) -> AuditReport:
    """Scan ``resource_types`` for resources missing translations in ``locale``.

    Only active rows are scanned (when the resource has an active flag). A row is
    incomplete when any source-present field lacks a translation. Pure with
    respect to its ``db`` argument, so tests can pass a savepoint session.
    """
    report = AuditReport(locale=locale, resource_types=list(resource_types))
    for resource_type in resource_types:
        resource_def = get_resource_def(resource_type)
        query = db.query(resource_def.model)
        if resource_def.active_attr is not None:
            active_col = getattr(resource_def.model, resource_def.active_attr)
            query = query.filter(active_col.is_(True))
        for instance in query.all():
            report.scanned += 1
            gaps = missing_fields(instance, resource_def, locale)
            if gaps:
                label = getattr(instance, resource_def.label_attr, "") or f"#{instance.id}"
                report.incomplete.append(
                    IncompleteResource(
                        resource_type=resource_type,
                        id=instance.id,
                        label=str(label),
                        missing_field_keys=[f.key for f in gaps],
                    )
                )
    return report


def _log_report(report: AuditReport) -> None:
    scope = ", ".join(report.resource_types)
    if report.ok:
        logger.info(
            "Translation audit OK [locale=%s, types=%s]: %d active resource(s), all complete.",
            report.locale,
            scope,
            report.scanned,
        )
        return
    logger.error(
        "Translation audit FAILED [locale=%s, types=%s]: %d of %d active resource(s) incomplete.",
        report.locale,
        scope,
        len(report.incomplete),
        report.scanned,
    )
    for item in report.incomplete:
        logger.error(
            "  - %s #%d %r missing %s: %s",
            item.resource_type,
            item.id,
            item.label,
            report.locale,
            ", ".join(item.missing_field_keys),
        )


def _purge_orphans() -> int:
    """Sweep sidecar rows whose resource is gone. Returns a process exit code.

    Exits 0 even when it deletes nothing — this is maintenance, not a gate, so
    it must never fail a deploy the way the audit itself is meant to.
    """
    db = SessionLocal()
    try:
        deleted = purge_orphan_translation_meta(db)
        db.commit()
    except Exception:
        logger.exception("translation_meta orphan purge failed")
        return 2
    finally:
        db.close()

    total = sum(deleted.values())
    if total == 0:
        logger.info("translation_meta: no orphan rows")
    else:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(deleted.items()) if v)
        logger.info("translation_meta: purged %d orphan row(s) — %s", total, breakdown)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    known_types = sorted(r.type for r in TRANSLATABLE_RESOURCES)
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.audit_translations",
        description="Fail-loud pre-deploy gate for missing translations.",
    )
    parser.add_argument(
        "--locale",
        default=TRANSLATION_LOCALES[0],
        choices=list(TRANSLATION_LOCALES),
        help=f"Target translation locale (default: {TRANSLATION_LOCALES[0]}).",
    )
    parser.add_argument(
        "--resource-type",
        action="append",
        dest="resource_types",
        choices=known_types,
        metavar="TYPE",
        help=(
            "Resource type to audit (repeatable). "
            f"Choices: {', '.join(known_types)}. Default: shipping_method."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Audit every registered resource type (overrides --resource-type).",
    )
    parser.add_argument(
        "--purge-orphans",
        action="store_true",
        help=(
            "Delete translation_meta rows whose resource no longer exists, then exit. "
            "The sidecar is polymorphic so it carries no FK; the delete paths purge "
            "explicitly and this is the self-healing backstop."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.purge_orphans:
        return _purge_orphans()
    if args.all:
        resource_types = [r.type for r in TRANSLATABLE_RESOURCES]
    else:
        resource_types = args.resource_types or list(DEFAULT_RESOURCE_TYPES)

    db = SessionLocal()
    try:
        report = run_audit(db, locale=args.locale, resource_types=resource_types)
    except Exception:
        logger.exception("translation audit failed to run")
        return 2
    finally:
        db.close()

    _log_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
