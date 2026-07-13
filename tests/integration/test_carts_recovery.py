"""Integration tests for abandoned-cart recovery (`/api/v1/carts` + the send job).

Covers the three surfaces of the v1 feature:

- ``PUT /carts/mine`` — authed snapshot upsert (create / change resets the
  token pair / deep-equal no-op keeps ``updated_at`` / empty deletes / 422 on
  bad shapes / 401 anonymous);
- ``GET /carts/recover/{token}`` — public token resolve with machine-code
  errors (``cart_not_found`` / ``recovery_link_expired`` /
  ``cart_already_ordered``), fresh catalog data, dead lines dropped;
- ``send_abandoned_cart_emails`` — the cron-driven detection job (disabled
  flag, threshold, one-email latch, explicit ``marketing_email`` opt-in gate,
  newer-order skip, per-user best-effort sends).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.crud import store_config as crud_store_config
from app.crud import user_consent as crud_user_consent
from app.models.order import Order
from app.models.product_variant import ProductVariant
from app.models.saved_cart import SavedCart
from app.services.abandoned_cart_email import send_abandoned_cart_emails

pytestmark = pytest.mark.integration

SYNC_URL = "/api/v1/carts/mine"
RECOVER_URL = "/api/v1/carts/recover/{token}"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _get_cart(db, user) -> SavedCart | None:
    return db.query(SavedCart).filter(SavedCart.user_id == user.id).first()


def _make_cart(
    db,
    user,
    *,
    items: list[dict],
    age_hours: float = 0,
    token: str | None = None,
    sent_at: datetime | None = None,
) -> SavedCart:
    """Insert a snapshot row directly, optionally backdated / token-armed."""
    cart = SavedCart(
        user_id=user.id,
        items=items,
        recovery_token=token,
        recovery_email_sent_at=sent_at,
    )
    db.add(cart)
    db.commit()
    if age_hours:
        # Explicit assignment wins over the ``onupdate`` default.
        cart.updated_at = _utcnow_naive() - timedelta(hours=age_hours)
        db.add(cart)
        db.commit()
    db.refresh(cart)
    return cart


def _make_variant(db, product, **overrides) -> ProductVariant:
    """Create a minimal active variant for ``product``."""
    defaults = {
        "product_id": product.id,
        "title": "Red / M",
        "option_signature": f"sig-{product.id}",
        "stock": 5,
        "is_active": True,
    }
    defaults.update(overrides)
    variant = ProductVariant(**defaults)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    return variant


def _make_order(db, user, *, order_number: str) -> Order:
    """Insert a minimal order for ``user`` (created now)."""
    order = Order(
        user_id=user.id,
        order_number=order_number,
        subtotal=100.0,
        total=100.0,
        currency="USD",
        shipping_address="1 Main St",
        shipping_city="Warsaw",
        shipping_postal_code="00-001",
        shipping_country="PL",
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _opt_in(db, user, granted: bool = True) -> None:
    crud_user_consent.record(
        db, user_id=user.id, consent_type="marketing_email", granted=granted, source="test"
    )
    db.commit()


def _enable_feature(db, *, threshold_hours: int = 24) -> None:
    config = crud_store_config.get_singleton(db)
    config.abandoned_cart_enabled = True
    config.abandoned_cart_threshold_hours = threshold_hours
    db.add(config)
    db.commit()


# ---------------------------------------------------------------------------
# PUT /carts/mine — sync upsert
# ---------------------------------------------------------------------------


async def test_sync_requires_auth(client):
    resp = await client.put(SYNC_URL, json={"items": []})
    assert resp.status_code == 401


async def test_sync_creates_row_and_counts_quantities(
    client, db_session, user_factory, product_factory
):
    user = user_factory(email="cart-sync1@example.com")
    product = product_factory()
    resp = await client.put(
        SYNC_URL,
        json={"items": [{"product_id": product.id, "variant_id": None, "quantity": 2}]},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "item_count": 2}
    cart = _get_cart(db_session, user)
    assert cart is not None
    assert cart.items == [{"product_id": product.id, "variant_id": None, "quantity": 2}]
    assert cart.recovery_token is None
    assert cart.recovery_email_sent_at is None


async def test_sync_change_resets_token_pair(client, db_session, user_factory, product_factory):
    user = user_factory(email="cart-sync2@example.com")
    product = product_factory()
    cart = _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        token="tok-sync-reset",
        sent_at=_utcnow_naive(),
    )
    resp = await client.put(
        SYNC_URL,
        json={"items": [{"product_id": product.id, "variant_id": None, "quantity": 3}]},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json()["item_count"] == 3
    db_session.refresh(cart)
    assert cart.recovery_token is None
    assert cart.recovery_email_sent_at is None


async def test_sync_deep_equal_does_not_touch_row(
    client, db_session, user_factory, product_factory
):
    user = user_factory(email="cart-sync3@example.com")
    product = product_factory()
    items = [{"product_id": product.id, "variant_id": None, "quantity": 2}]
    cart = _make_cart(
        db_session, user, items=items, age_hours=5, token="tok-keep", sent_at=_utcnow_naive()
    )
    before = cart.updated_at
    resp = await client.put(SYNC_URL, json={"items": items}, headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["item_count"] == 2
    db_session.refresh(cart)
    assert cart.updated_at == before
    # Unchanged upsert must not re-arm either.
    assert cart.recovery_token == "tok-keep"


async def test_sync_empty_items_deletes_row(client, db_session, user_factory, product_factory):
    user = user_factory(email="cart-sync4@example.com")
    product = product_factory()
    _make_cart(
        db_session, user, items=[{"product_id": product.id, "variant_id": None, "quantity": 1}]
    )
    resp = await client.put(SYNC_URL, json={"items": []}, headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "item_count": 0}
    assert _get_cart(db_session, user) is None


@pytest.mark.parametrize(
    "bad_item",
    [
        {"product_id": 0, "variant_id": None, "quantity": 1},
        {"product_id": 1, "variant_id": 0, "quantity": 1},
        {"product_id": 1, "variant_id": None, "quantity": 0},
        {"product_id": 1, "variant_id": None, "quantity": 1000},
        {"variant_id": None, "quantity": 1},
    ],
)
async def test_sync_rejects_bad_shapes(client, user_factory, bad_item):
    user = user_factory()
    resp = await client.put(SYNC_URL, json={"items": [bad_item]}, headers=_headers(user))
    assert resp.status_code == 422


async def test_sync_rejects_over_100_lines(client, user_factory):
    user = user_factory()
    items = [{"product_id": i + 1, "variant_id": None, "quantity": 1} for i in range(101)]
    resp = await client.put(SYNC_URL, json={"items": items}, headers=_headers(user))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /carts/recover/{token}
# ---------------------------------------------------------------------------


async def test_recover_happy_path_returns_fresh_data(
    client, db_session, user_factory, product_factory, reset_limiter
):
    user = user_factory(email="cart-rec1@example.com")
    product = product_factory(price_usd=19.99, stock=10)
    _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 2}],
        token="tok-happy",
        sent_at=_utcnow_naive(),
    )
    resp = await client.get(RECOVER_URL.format(token="tok-happy"))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    line = body["items"][0]
    assert line["quantity"] == 2
    assert line["variant"] is None
    # Fresh catalog data — the public product shape, live price/stock.
    assert line["product"]["id"] == product.id
    assert line["product"]["slug"] == product.slug
    assert line["product"]["price_usd"] == 19.99
    assert line["product"]["in_stock"] is True


async def test_recover_returns_variant_line(
    client, db_session, user_factory, product_factory, reset_limiter
):
    user = user_factory(email="cart-rec2@example.com")
    product = product_factory()
    variant = _make_variant(db_session, product)
    _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": variant.id, "quantity": 1}],
        token="tok-variant",
        sent_at=_utcnow_naive(),
    )
    resp = await client.get(RECOVER_URL.format(token="tok-variant"))
    assert resp.status_code == 200
    line = resp.json()["items"][0]
    assert line["variant"]["id"] == variant.id
    assert line["variant"]["title"] == "Red / M"


async def test_recover_does_not_consume_token(
    client, db_session, user_factory, product_factory, reset_limiter
):
    user = user_factory(email="cart-rec3@example.com")
    product = product_factory()
    _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        token="tok-twice",
        sent_at=_utcnow_naive(),
    )
    first = await client.get(RECOVER_URL.format(token="tok-twice"))
    second = await client.get(RECOVER_URL.format(token="tok-twice"))
    assert first.status_code == 200
    assert second.status_code == 200


async def test_recover_unknown_token_404_machine_code(client, reset_limiter):
    resp = await client.get(RECOVER_URL.format(token="no-such-token"))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "cart_not_found"


async def test_recover_expired_410(
    client, db_session, user_factory, product_factory, reset_limiter
):
    user = user_factory(email="cart-rec4@example.com")
    product = product_factory()
    _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        token="tok-old",
        sent_at=_utcnow_naive() - timedelta(days=31),
    )
    resp = await client.get(RECOVER_URL.format(token="tok-old"))
    assert resp.status_code == 410
    assert resp.json()["detail"] == "recovery_link_expired"


async def test_recover_already_ordered_410(
    client, db_session, user_factory, product_factory, reset_limiter
):
    user = user_factory(email="cart-rec5@example.com")
    product = product_factory()
    _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        age_hours=48,
        token="tok-ordered",
        sent_at=_utcnow_naive(),
    )
    # An order created after the cart's updated_at (ordered on another device).
    _make_order(db_session, user, order_number="ORD-CARTREC-0001")
    resp = await client.get(RECOVER_URL.format(token="tok-ordered"))
    assert resp.status_code == 410
    assert resp.json()["detail"] == "cart_already_ordered"


async def test_recover_drops_dead_lines(
    client, db_session, user_factory, product_factory, reset_limiter
):
    user = user_factory(email="cart-rec6@example.com")
    live = product_factory()
    dead = product_factory(is_active=False)
    product_with_gone_variant = product_factory()
    inactive_variant = _make_variant(db_session, product_with_gone_variant, is_active=False)
    _make_cart(
        db_session,
        user,
        items=[
            {"product_id": live.id, "variant_id": None, "quantity": 1},
            {"product_id": dead.id, "variant_id": None, "quantity": 1},
            {"product_id": 999_999, "variant_id": None, "quantity": 1},
            {
                "product_id": product_with_gone_variant.id,
                "variant_id": inactive_variant.id,
                "quantity": 1,
            },
        ],
        token="tok-dead",
        sent_at=_utcnow_naive(),
    )
    resp = await client.get(RECOVER_URL.format(token="tok-dead"))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [line["product"]["id"] for line in items] == [live.id]


async def test_recover_all_lines_dead_returns_empty(
    client, db_session, user_factory, product_factory, reset_limiter
):
    user = user_factory(email="cart-rec7@example.com")
    dead = product_factory(is_active=False)
    _make_cart(
        db_session,
        user,
        items=[{"product_id": dead.id, "variant_id": None, "quantity": 1}],
        token="tok-empty",
        sent_at=_utcnow_naive(),
    )
    resp = await client.get(RECOVER_URL.format(token="tok-empty"))
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# send_abandoned_cart_emails — the detection job
# ---------------------------------------------------------------------------


def test_job_disabled_flag_sends_nothing(
    db_session, user_factory, product_factory, captured_emails
):
    user = user_factory(email="cart-job1@example.com")
    product = product_factory()
    _opt_in(db_session, user)
    _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        age_hours=48,
    )
    # store_config seeds abandoned_cart_enabled=false — leave it.
    assert send_abandoned_cart_emails(db_session) == 0
    assert captured_emails == []


def test_job_fresh_cart_not_sent(db_session, user_factory, product_factory, captured_emails):
    user = user_factory(email="cart-job2@example.com")
    product = product_factory()
    _opt_in(db_session, user)
    _enable_feature(db_session, threshold_hours=24)
    _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        age_hours=1,
    )
    assert send_abandoned_cart_emails(db_session) == 0
    assert captured_emails == []


def test_job_sends_once_and_stamps_latch(
    db_session, user_factory, product_factory, captured_emails
):
    user = user_factory(email="cart-job3@example.com")
    product = product_factory()
    _opt_in(db_session, user)
    _enable_feature(db_session, threshold_hours=24)
    cart = _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 2}],
        age_hours=48,
    )
    assert send_abandoned_cart_emails(db_session) == 1
    assert len(captured_emails) == 1
    assert captured_emails[0]["email_to"] == user.email
    assert product.name in captured_emails[0]["html_content"]
    db_session.refresh(cart)
    assert cart.recovery_token is not None
    assert cart.recovery_email_sent_at is not None
    assert f"/cart/recover/{cart.recovery_token}" in captured_emails[0]["html_content"]
    # Second run: the latch holds — never a second email for the same cart.
    assert send_abandoned_cart_emails(db_session) == 0
    assert len(captured_emails) == 1


def test_job_pl_locale_copy_and_link_prefix(
    db_session, user_factory, product_factory, captured_emails
):
    user = user_factory(email="cart-job-pl@example.com", locale="pl")
    product = product_factory(name_pl="Produkt PL")
    _opt_in(db_session, user)
    _enable_feature(db_session)
    cart = _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        age_hours=48,
    )
    assert send_abandoned_cart_emails(db_session) == 1
    sent = captured_emails[0]
    assert "Twój koszyk czeka" in sent["subject"]
    assert "Produkt PL" in sent["html_content"]
    db_session.refresh(cart)
    assert f"/pl/cart/recover/{cart.recovery_token}" in sent["html_content"]


@pytest.mark.parametrize("consent_state", ["unset", "revoked"])
def test_job_skips_without_explicit_opt_in(
    db_session, user_factory, product_factory, captured_emails, consent_state
):
    """Explicit opt-in only (UŚUDE): unset AND revoked both mean no send."""
    user = user_factory(email=f"cart-job-{consent_state}@example.com")
    product = product_factory()
    if consent_state == "revoked":
        _opt_in(db_session, user, granted=True)
        _opt_in(db_session, user, granted=False)  # latest row wins
    _enable_feature(db_session)
    _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        age_hours=48,
    )
    assert send_abandoned_cart_emails(db_session) == 0
    assert captured_emails == []


def test_job_skips_user_with_newer_order(
    db_session, user_factory, product_factory, captured_emails
):
    user = user_factory(email="cart-job5@example.com")
    product = product_factory()
    _opt_in(db_session, user)
    _enable_feature(db_session)
    _make_cart(
        db_session,
        user,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        age_hours=48,
    )
    _make_order(db_session, user, order_number="ORD-CARTJOB-0001")
    assert send_abandoned_cart_emails(db_session) == 0
    assert captured_emails == []


def test_job_resend_failure_skips_user_not_run(
    db_session, user_factory, product_factory, captured_emails, monkeypatch
):
    user_a = user_factory(email="cart-job-fail@example.com")
    user_b = user_factory(email="cart-job-ok@example.com")
    product = product_factory()
    for user in (user_a, user_b):
        _opt_in(db_session, user)
    _enable_feature(db_session)
    cart_a = _make_cart(
        db_session,
        user_a,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        age_hours=48,
    )
    cart_b = _make_cart(
        db_session,
        user_b,
        items=[{"product_id": product.id, "variant_id": None, "quantity": 1}],
        age_hours=48,
    )

    import app.core.email as email_mod

    real_send = email_mod.send_email

    def _flaky(*, email_to: str, **kwargs):
        if email_to == user_a.email:
            raise RuntimeError("resend down")
        return real_send(email_to=email_to, **kwargs)

    monkeypatch.setattr(email_mod, "send_email", _flaky)

    assert send_abandoned_cart_emails(db_session) == 1
    assert [entry["email_to"] for entry in captured_emails] == [user_b.email]
    db_session.refresh(cart_a)
    db_session.refresh(cart_b)
    # Failed send leaves the cart re-armed (no live token without an email out).
    assert cart_a.recovery_token is None
    assert cart_a.recovery_email_sent_at is None
    assert cart_b.recovery_token is not None
