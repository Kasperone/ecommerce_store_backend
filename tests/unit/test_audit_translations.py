"""Tests for the translation-completeness registry + audit gate (Phase 0).

Two layers:

- Pure registry semantics (:mod:`app.services.translation_registry`) on transient
  model instances — needs no DB.
- ``run_audit`` against a savepoint ``db_session`` and ``main``'s arg→scope→exit
  wiring (stubbed, no DB).

Scoping note: the session-scoped conftest seed commits a ``code="test"`` /
``name="Test Shipping"`` shipping method with no ``name_pl``, so it is *always*
incomplete in ``shipping_method`` scope. DB assertions therefore test membership
of ids these tests create (and, for the all-complete case, backfill the seeded
method within the savepoint) rather than global totals.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.shipping import ShippingMethod
from app.scripts import audit_translations as audit_mod
from app.scripts.audit_translations import AuditReport, IncompleteResource, run_audit
from app.services.translation_registry import (
    TRANSLATABLE_RESOURCES,
    field_is_translated,
    field_needs_translation,
    get_resource_def,
    missing_fields,
    resource_completeness,
    resource_is_complete,
    source_digest,
)

SHIPPING = get_resource_def("shipping_method")
NAME_FIELD = next(f for f in SHIPPING.fields if f.key == "name")
DESC_FIELD = next(f for f in SHIPPING.fields if f.key == "description")


# ---------------------------------------------------------------------------
# Pure registry semantics (no DB)
# ---------------------------------------------------------------------------


def test_field_needs_translation_only_when_source_present():
    has_source = ShippingMethod(name="Express", description="Fast")
    assert field_needs_translation(has_source, NAME_FIELD) is True
    assert field_needs_translation(has_source, DESC_FIELD) is True

    no_desc = ShippingMethod(name="Express", description=None)
    assert field_needs_translation(no_desc, DESC_FIELD) is False


def test_field_is_translated_requires_nonempty_pl():
    m = ShippingMethod(name="Express", name_pl="Ekspres", description="Fast", description_pl=None)
    assert field_is_translated(m, NAME_FIELD, "pl") is True
    assert field_is_translated(m, DESC_FIELD, "pl") is False


def test_whitespace_translation_treated_as_missing():
    m = ShippingMethod(name="Express", name_pl="   ")
    assert field_is_translated(m, NAME_FIELD, "pl") is False
    assert NAME_FIELD in missing_fields(m, SHIPPING, "pl")


def test_unsupported_locale_attr_is_missing():
    m = ShippingMethod(name="Express", name_pl="Ekspres")
    # No German column exists -> attr_for returns None -> treated as untranslated.
    assert field_is_translated(m, NAME_FIELD, "de") is False


def test_empty_source_field_not_flagged():
    # EN name translated, no EN description at all -> description is not a gap.
    m = ShippingMethod(name="Express", name_pl="Ekspres", description=None, description_pl=None)
    assert missing_fields(m, SHIPPING, "pl") == []
    assert resource_is_complete(m, SHIPPING, "pl") is True


def test_missing_fields_flags_untranslated_source_present():
    m = ShippingMethod(name="Express", description="Fast", name_pl=None, description_pl=None)
    gaps = {f.key for f in missing_fields(m, SHIPPING, "pl")}
    assert gaps == {"name", "description"}
    assert resource_is_complete(m, SHIPPING, "pl") is False


def test_resource_completeness_counts_applicable_only():
    # name + description sources present, only name translated -> 1 of 2.
    m = ShippingMethod(name="Express", name_pl="Ekspres", description="Fast", description_pl=None)
    assert resource_completeness(m, SHIPPING, "pl") == (1, 2)

    # name source present + translated, no description source -> 1 of 1.
    m2 = ShippingMethod(name="Express", name_pl="Ekspres", description=None)
    assert resource_completeness(m2, SHIPPING, "pl") == (1, 1)


def test_resource_is_complete_true_when_all_translated():
    m = ShippingMethod(
        name="Express", name_pl="Ekspres", description="Fast", description_pl="Szybko"
    )
    assert resource_is_complete(m, SHIPPING, "pl") is True


def test_get_resource_def_unknown_raises():
    with pytest.raises(KeyError, match="Unknown translatable resource type"):
        get_resource_def("nope")


def test_registry_covers_expected_types():
    types = {r.type for r in TRANSLATABLE_RESOURCES}
    assert {"shipping_method", "product", "category", "collection"} <= types


def test_source_digest_stable_and_change_sensitive():
    assert source_digest("Express") == source_digest("Express")
    assert source_digest("Express") != source_digest("Express!")
    # None and "" normalise to the same (empty) digest.
    assert source_digest(None) == source_digest("")
    # 64-char hex (sha256).
    assert len(source_digest("x")) == 64


# ---------------------------------------------------------------------------
# run_audit against the DB (savepoint session)
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.integration


def _add_method(db: Session, **kwargs) -> ShippingMethod:
    m = ShippingMethod(is_active=True, **kwargs)
    db.add(m)
    db.flush()
    return m


def _incomplete_ids(report: AuditReport) -> set[int]:
    return {i.id for i in report.incomplete}


@pytest.mark.integration
def test_run_audit_flags_method_missing_pl(db_session: Session):
    m = _add_method(db_session, name="DPD Courier", code="dpd-x", description="Door to door")
    report = run_audit(db_session, locale="pl", resource_types=["shipping_method"])
    assert m.id in _incomplete_ids(report)
    item = next(i for i in report.incomplete if i.id == m.id)
    assert set(item.missing_field_keys) == {"name", "description"}
    assert report.ok is False


@pytest.mark.integration
def test_run_audit_complete_method_not_flagged(db_session: Session):
    m = _add_method(
        db_session,
        name="InPost Locker",
        code="inpost-x",
        description="Pickup point",
        name_pl="Paczkomat InPost",
        description_pl="Punkt odbioru",
    )
    report = run_audit(db_session, locale="pl", resource_types=["shipping_method"])
    assert m.id not in _incomplete_ids(report)


@pytest.mark.integration
def test_run_audit_partial_translation_flags_only_gap(db_session: Session):
    m = _add_method(
        db_session,
        name="Cash on delivery",
        code="cod-x",
        description="Pay on arrival",
        name_pl="Pobranie",  # name translated, description not
    )
    report = run_audit(db_session, locale="pl", resource_types=["shipping_method"])
    item = next(i for i in report.incomplete if i.id == m.id)
    assert item.missing_field_keys == ["description"]


@pytest.mark.integration
def test_run_audit_ignores_inactive(db_session: Session):
    m = ShippingMethod(name="Retired", code="retired-x", description="old", is_active=False)
    db_session.add(m)
    db_session.flush()
    report = run_audit(db_session, locale="pl", resource_types=["shipping_method"])
    assert m.id not in _incomplete_ids(report)


@pytest.mark.integration
def test_run_audit_ok_when_all_complete(db_session: Session):
    # Backfill every existing active method (incl. the conftest "Test Shipping"
    # seed) within the savepoint so the whole shipping_method scope is complete.
    for existing in db_session.query(ShippingMethod).filter(ShippingMethod.is_active.is_(True)):
        if existing.name and not (existing.name_pl or "").strip():
            existing.name_pl = f"{existing.name} (PL)"
        if existing.description and not (existing.description_pl or "").strip():
            existing.description_pl = f"{existing.description} (PL)"
    _add_method(
        db_session,
        name="Self pickup",
        code="pickup-x",
        description="Collect in store",
        name_pl="Odbiór osobisty",
        description_pl="Odbiór w sklepie",
    )
    db_session.flush()
    report = run_audit(db_session, locale="pl", resource_types=["shipping_method"])
    assert report.ok is True
    assert report.scanned >= 2


# ---------------------------------------------------------------------------
# main() wiring: arg -> scope -> exit code (stubbed, no DB)
# ---------------------------------------------------------------------------


class _DummySession:
    def close(self) -> None:  # pragma: no cover - trivial
        pass


@pytest.fixture
def stub_run_audit(monkeypatch):
    """Replace SessionLocal + run_audit; capture the resource_types main resolves."""
    captured: dict[str, object] = {}

    def fake_run_audit(db, *, locale, resource_types):
        captured["locale"] = locale
        captured["resource_types"] = resource_types
        report = AuditReport(locale=locale, resource_types=list(resource_types))
        report.scanned = 1
        return report

    monkeypatch.setattr(audit_mod, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(audit_mod, "run_audit", fake_run_audit)
    return captured


def test_main_default_scope_is_shipping_method(stub_run_audit):
    assert audit_mod.main([]) == 0
    assert stub_run_audit["resource_types"] == ["shipping_method"]
    assert stub_run_audit["locale"] == "pl"


def test_main_all_flag_widens_to_every_type(stub_run_audit):
    assert audit_mod.main(["--all"]) == 0
    assert stub_run_audit["resource_types"] == [r.type for r in TRANSLATABLE_RESOURCES]


def test_main_resource_type_repeatable(stub_run_audit):
    assert audit_mod.main(["--resource-type", "product", "--resource-type", "category"]) == 0
    assert stub_run_audit["resource_types"] == ["product", "category"]


def test_main_returns_1_when_incomplete(monkeypatch):
    def fake_run_audit(db, *, locale, resource_types):
        report = AuditReport(locale=locale, resource_types=list(resource_types))
        report.scanned = 1
        report.incomplete = [
            IncompleteResource(
                resource_type="shipping_method", id=1, label="X", missing_field_keys=["name"]
            )
        ]
        return report

    monkeypatch.setattr(audit_mod, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(audit_mod, "run_audit", fake_run_audit)
    assert audit_mod.main([]) == 1


def test_main_returns_2_on_unexpected_error(monkeypatch):
    def boom(db, *, locale, resource_types):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(audit_mod, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(audit_mod, "run_audit", boom)
    assert audit_mod.main([]) == 2
