"""Integration tests for the admin Translations workspace API (Phase 1).

Covers authz, the resource-types completeness summary, the per-type listing
(source+translation rows, ``missing`` filter, search, pagination), and the
update path (writes ``*_pl`` columns, clears on blank, validation 422s,
not-found 404s, bad-locale 400, and audit-row recording).

Scoping note: the conftest seed commits a ``code="test"`` shipping method with
no ``name_pl`` (always incomplete), so assertions use membership / monotonic
deltas rather than absolute global counts.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.shipping import ShippingMethod
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _make_method(db, **kwargs) -> ShippingMethod:
    m = ShippingMethod(is_active=True, **kwargs)
    db.add(m)
    db.flush()
    return m


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_endpoints_require_admin(client, user_factory):
    customer = user_factory(email="tr-cust@example.com", role=UserRole.CUSTOMER)
    for url in (
        "/api/v1/admin/translations/resource-types",
        "/api/v1/admin/translations/shipping_method",
    ):
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers=_headers(customer))).status_code == 403


async def test_update_requires_admin(client, user_factory):
    customer = user_factory(email="tr-cust2@example.com", role=UserRole.CUSTOMER)
    url = "/api/v1/admin/translations/shipping_method/1"
    assert (await client.put(url, json={"fields": {}})).status_code == 401
    assert (
        await client.put(url, json={"fields": {}}, headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# resource-types summary
# ---------------------------------------------------------------------------


async def test_resource_types_summary_counts(client, user_factory, db_session):
    admin = user_factory(email="tr-admin1@example.com", role=UserRole.ADMIN)
    _make_method(
        db_session,
        name="Complete Method",
        code="tr-complete",
        description="Fast",
        name_pl="Metoda kompletna",
        description_pl="Szybko",
    )
    _make_method(db_session, name="Gap Method", code="tr-gap", description="Door")

    resp = await client.get("/api/v1/admin/translations/resource-types", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["locale"] == "pl"
    types = {t["type"]: t for t in body["types"]}
    assert {"shipping_method", "product", "category", "collection"} <= set(types)
    ship = types["shipping_method"]
    # seed "Test Shipping" + our gap method are incomplete; our complete one counts.
    assert ship["resource_count"] >= 3
    assert ship["complete_count"] >= 1
    assert ship["incomplete_count"] >= 2
    assert body["resource_count"] == sum(t["resource_count"] for t in body["types"])


async def test_resource_types_bad_locale_400(client, user_factory):
    admin = user_factory(email="tr-admin-loc@example.com", role=UserRole.ADMIN)
    resp = await client.get(
        "/api/v1/admin/translations/resource-types?locale=fr", headers=_headers(admin)
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_returns_source_and_translation_rows(client, user_factory, db_session):
    admin = user_factory(email="tr-admin2@example.com", role=UserRole.ADMIN)
    m = _make_method(
        db_session,
        name="Listed Method",
        code="tr-listed",
        description="Pickup point",
        name_pl="Wymieniona metoda",
    )
    resp = await client.get(
        "/api/v1/admin/translations/shipping_method?page_size=100", headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource_type"] == "shipping_method"
    row = next(r for r in body["items"] if r["id"] == m.id)
    assert row["label"] == "Listed Method"
    assert row["complete"] is False  # description has no PL
    by_key = {f["key"]: f for f in row["fields"]}
    assert by_key["name"]["source"] == "Listed Method"
    assert by_key["name"]["translation"] == "Wymieniona metoda"
    assert by_key["name"]["translated"] is True
    assert by_key["name"]["max_len"] == 80
    assert by_key["description"]["source"] == "Pickup point"
    assert by_key["description"]["translation"] is None
    assert by_key["description"]["needs_translation"] is True
    assert by_key["description"]["translated"] is False


async def test_list_missing_filter_excludes_complete(client, user_factory, db_session):
    admin = user_factory(email="tr-admin3@example.com", role=UserRole.ADMIN)
    complete = _make_method(
        db_session,
        name="All Translated",
        code="tr-all",
        description="x",
        name_pl="Wszystko",
        description_pl="x-pl",
    )
    gap = _make_method(db_session, name="Has Gap", code="tr-hasgap", description="y")

    resp = await client.get(
        "/api/v1/admin/translations/shipping_method?status=missing&page_size=100",
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()["items"]}
    assert gap.id in ids
    assert complete.id not in ids


async def test_list_search_by_label(client, user_factory, db_session):
    admin = user_factory(email="tr-admin4@example.com", role=UserRole.ADMIN)
    m = _make_method(db_session, name="Zatzenheimer Express", code="tr-zat", description="z")
    resp = await client.get(
        "/api/v1/admin/translations/shipping_method?q=Zatzenheimer", headers=_headers(admin)
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [r["id"] for r in items] == [m.id]


async def test_list_unknown_type_404(client, user_factory):
    admin = user_factory(email="tr-admin5@example.com", role=UserRole.ADMIN)
    resp = await client.get("/api/v1/admin/translations/nope", headers=_headers(admin))
    assert resp.status_code == 404


async def test_list_invalid_status_422(client, user_factory):
    admin = user_factory(email="tr-admin-st@example.com", role=UserRole.ADMIN)
    resp = await client.get(
        "/api/v1/admin/translations/shipping_method?status=weird", headers=_headers(admin)
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_writes_pl_columns_and_audits(client, user_factory, db_session):
    admin = user_factory(email="tr-admin6@example.com", role=UserRole.ADMIN)
    m = _make_method(db_session, name="Edit Me", code="tr-edit", description="Old desc")

    resp = await client.put(
        f"/api/v1/admin/translations/shipping_method/{m.id}",
        json={"fields": {"name": "Edytuj mnie", "description": "Stary opis"}},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["complete"] is True
    by_key = {f["key"]: f for f in row["fields"]}
    assert by_key["name"]["translation"] == "Edytuj mnie"
    assert by_key["description"]["translation"] == "Stary opis"

    db_session.expire_all()
    refreshed = db_session.get(ShippingMethod, m.id)
    assert refreshed.name_pl == "Edytuj mnie"
    assert refreshed.description_pl == "Stary opis"

    event = (
        db_session.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "shipping_method",
            AdminAuditEvent.action == "update",
            AdminAuditEvent.resource_id == m.id,
        )
        .first()
    )
    assert event is not None
    assert "name_pl" in event.payload["after"]


async def test_update_blank_clears_translation(client, user_factory, db_session):
    admin = user_factory(email="tr-admin7@example.com", role=UserRole.ADMIN)
    m = _make_method(
        db_session, name="Clear Me", code="tr-clear", description="d", name_pl="Wyczyść"
    )

    resp = await client.put(
        f"/api/v1/admin/translations/shipping_method/{m.id}",
        json={"fields": {"name": "   "}},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(ShippingMethod, m.id).name_pl is None


async def test_update_unknown_field_422(client, user_factory, db_session):
    admin = user_factory(email="tr-admin8@example.com", role=UserRole.ADMIN)
    m = _make_method(db_session, name="Unknown Field", code="tr-unk", description="d")
    resp = await client.put(
        f"/api/v1/admin/translations/shipping_method/{m.id}",
        json={"fields": {"nope": "x"}},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_update_too_long_422(client, user_factory, db_session):
    admin = user_factory(email="tr-admin9@example.com", role=UserRole.ADMIN)
    m = _make_method(db_session, name="Too Long", code="tr-long", description="d")
    resp = await client.put(
        f"/api/v1/admin/translations/shipping_method/{m.id}",
        json={"fields": {"name": "x" * 81}},  # name max_len = 80
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_update_unknown_id_404(client, user_factory):
    admin = user_factory(email="tr-admin10@example.com", role=UserRole.ADMIN)
    resp = await client.put(
        "/api/v1/admin/translations/shipping_method/99999999",
        json={"fields": {"name": "x"}},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_update_unknown_type_404(client, user_factory):
    admin = user_factory(email="tr-admin11@example.com", role=UserRole.ADMIN)
    resp = await client.put(
        "/api/v1/admin/translations/nope/1",
        json={"fields": {"name": "x"}},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_update_bad_locale_400(client, user_factory, db_session):
    admin = user_factory(email="tr-admin12@example.com", role=UserRole.ADMIN)
    m = _make_method(db_session, name="Bad Locale", code="tr-loc", description="d")
    resp = await client.put(
        f"/api/v1/admin/translations/shipping_method/{m.id}?locale=fr",
        json={"fields": {"name": "x"}},
        headers=_headers(admin),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# outdated detection (Phase 2 — source-digest sidecar)
# ---------------------------------------------------------------------------


async def _put_fields(client, admin, method_id, fields):
    return await client.put(
        f"/api/v1/admin/translations/shipping_method/{method_id}",
        json={"fields": fields},
        headers=_headers(admin),
    )


def _row_for(body, mid):
    return next(r for r in body["items"] if r["id"] == mid)


def _field(row, key):
    return next(f for f in row["fields"] if f["key"] == key)


async def _list(client, admin, **query):
    qs = "&".join(f"{k}={v}" for k, v in {"page_size": 100, **query}.items())
    resp = await client.get(
        f"/api/v1/admin/translations/shipping_method?{qs}", headers=_headers(admin)
    )
    return resp.json()


async def test_save_stamps_digest_and_not_outdated(client, user_factory, db_session):
    admin = user_factory(email="tr-out1@example.com", role=UserRole.ADMIN)
    m = _make_method(db_session, name="Outdated A", code="tr-out-a", description="d")
    resp = await _put_fields(client, admin, m.id, {"name": "Tłumaczenie"})
    row = resp.json()
    assert _field(row, "name")["outdated"] is False
    assert row["has_outdated"] is False


async def test_source_change_marks_outdated(client, user_factory, db_session):
    admin = user_factory(email="tr-out2@example.com", role=UserRole.ADMIN)
    m = _make_method(db_session, name="Outdated B", code="tr-out-b", description="d")
    await _put_fields(client, admin, m.id, {"name": "Tłumaczenie"})

    # Change the English source after the translation was stamped.
    m.name = "Outdated B (changed)"
    db_session.commit()

    body = await _list(client, admin)
    row = _row_for(body, m.id)
    assert _field(row, "name")["outdated"] is True
    assert row["has_outdated"] is True


async def test_resave_clears_outdated(client, user_factory, db_session):
    admin = user_factory(email="tr-out3@example.com", role=UserRole.ADMIN)
    m = _make_method(db_session, name="Outdated C", code="tr-out-c", description="d")
    await _put_fields(client, admin, m.id, {"name": "Tłumaczenie"})
    m.name = "Outdated C (changed)"
    db_session.commit()

    # Re-save the translation against the new source -> digest re-stamped.
    resp = await _put_fields(client, admin, m.id, {"name": "Nowe tłumaczenie"})
    assert _field(resp.json(), "name")["outdated"] is False


async def test_outdated_status_filter(client, user_factory, db_session):
    admin = user_factory(email="tr-out4@example.com", role=UserRole.ADMIN)
    stale = _make_method(db_session, name="Stale One", code="tr-out-s", description="d")
    fresh = _make_method(db_session, name="Fresh One", code="tr-out-f", description="d")
    await _put_fields(client, admin, stale.id, {"name": "Nieaktualne"})
    await _put_fields(client, admin, fresh.id, {"name": "Aktualne"})
    stale.name = "Stale One (changed)"
    db_session.commit()

    body = await _list(client, admin, status="outdated")
    ids = {r["id"] for r in body["items"]}
    assert stale.id in ids
    assert fresh.id not in ids


async def test_unchanged_sibling_preserves_outdated(client, user_factory, db_session):
    admin = user_factory(email="tr-out5@example.com", role=UserRole.ADMIN)
    m = _make_method(db_session, name="Sibling", code="tr-out-sib", description="Desc")
    # Translate both fields, then change both English sources.
    await _put_fields(client, admin, m.id, {"name": "Nazwa", "description": "Opis"})
    m.name = "Sibling (changed)"
    m.description = "Desc (changed)"
    db_session.commit()

    # Re-save only description (a no-op send of name keeps name outdated).
    resp = await _put_fields(client, admin, m.id, {"name": "Nazwa", "description": "Opis nowy"})
    row = resp.json()
    assert _field(row, "description")["outdated"] is False
    assert _field(row, "name")["outdated"] is True


async def test_clearing_translation_drops_digest(client, user_factory, db_session):
    admin = user_factory(email="tr-out6@example.com", role=UserRole.ADMIN)
    m = _make_method(db_session, name="Cleared", code="tr-out-clr", description="d")
    await _put_fields(client, admin, m.id, {"name": "Do usunięcia"})
    # Clear the translation, then change the source: no digest -> not outdated.
    await _put_fields(client, admin, m.id, {"name": ""})
    m.name = "Cleared (changed)"
    db_session.commit()

    body = await _list(client, admin)
    row = _row_for(body, m.id)
    assert _field(row, "name")["outdated"] is False
