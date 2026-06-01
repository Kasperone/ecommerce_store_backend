"""Unit tests for ``CRUDQuote`` (DB-backed, no HTTP).

Covers create-with-items (id + computed line_totals + subtotal/total), nested
get, the ``status`` / ``company_id`` filters, pagination ordering (newest first),
partial scalar update, items-replace (recompute) vs items-omitted (untouched),
cascade delete, and the ``company_exists`` helper. Validation (status/currency in
the allowed set, non-blank description, positive quantity, empty-items) is
enforced by the Pydantic schema, so it lives in the endpoint tests — the CRUD
layer trusts a validated ``QuoteCreate`` / ``QuoteUpdate``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.crud import quote as crud_quote
from app.crud.order import InsufficientStockError, ProductUnavailableError
from app.crud.quote import QuoteNotConvertibleError
from app.models.inventory_movement import InventoryMovement
from app.models.order import OrderStatus
from app.models.quote import Quote, QuoteItem
from app.schemas.quote import QuoteCreate, QuoteItemCreate, QuoteUpdate
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration


@pytest.fixture
def company_factory(db_session: Session):
    """Callable creating a Company row via the factory (session bound per-test)."""
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attrs aren't surfaced on the FactoryOptions stub
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


def _create_in(company_id: int, **overrides) -> QuoteCreate:
    """Build a valid ``QuoteCreate`` with a single $10 x2 line by default."""
    base = {
        "company_id": company_id,
        "currency": "USD",
        "items": [QuoteItemCreate(description="Widget", quantity=2, unit_price=10.0)],
    }
    base.update(overrides)
    return QuoteCreate(**base)


def test_create_computes_line_totals_and_subtotal(db_session: Session, company_factory):
    company = company_factory(name="Acme")
    obj_in = _create_in(
        company.id,
        items=[
            QuoteItemCreate(description="A", quantity=3, unit_price=5.0),
            QuoteItemCreate(description="B", quantity=2, unit_price=7.5),
        ],
    )
    created = crud_quote.create_with_items(db_session, obj_in=obj_in)
    assert created.id is not None
    assert created.status == "draft"  # default
    assert created.currency == "USD"
    # line_total = qty * unit_price
    assert [i.line_total for i in created.items] == [15.0, 15.0]
    # subtotal = sum(line_totals); total == subtotal in MVP
    assert created.subtotal == 30.0
    assert created.total == 30.0


def test_create_rounds_to_two_decimals(db_session: Session, company_factory):
    company = company_factory()
    obj_in = _create_in(
        company.id,
        items=[QuoteItemCreate(description="Odd", quantity=3, unit_price=0.1)],
    )
    created = crud_quote.create_with_items(db_session, obj_in=obj_in)
    # 3 * 0.1 = 0.30000000000000004 → rounded to 0.3
    assert created.items[0].line_total == 0.3
    assert created.subtotal == 0.3
    assert created.total == 0.3


def test_create_stores_optional_product_id(db_session: Session, company_factory, product_factory):
    company = company_factory()
    product = product_factory()
    obj_in = _create_in(
        company.id,
        items=[
            QuoteItemCreate(
                description="Linked", quantity=1, unit_price=9.0, product_id=product.id
            ),
            QuoteItemCreate(description="Freeform", quantity=1, unit_price=4.0),
        ],
    )
    created = crud_quote.create_with_items(db_session, obj_in=obj_in)
    assert created.items[0].product_id == product.id
    assert created.items[1].product_id is None


def test_get_returns_quote_with_items(db_session: Session, company_factory):
    company = company_factory()
    created = crud_quote.create_with_items(db_session, obj_in=_create_in(company.id))
    db_session.commit()
    fetched = crud_quote.get(db_session, created.id)
    assert fetched is not None
    assert fetched.company_name == company.name
    assert fetched.item_count == 1
    assert len(fetched.items) == 1


def test_get_missing_returns_none(db_session: Session):
    assert crud_quote.get(db_session, 999999) is None


def test_company_exists(db_session: Session, company_factory):
    company = company_factory()
    assert crud_quote.company_exists(db_session, company.id) is True
    assert crud_quote.company_exists(db_session, 999999) is False


def test_get_multi_filters_status(db_session: Session, company_factory):
    company = company_factory()
    q1 = crud_quote.create_with_items(db_session, obj_in=_create_in(company.id))
    q2 = crud_quote.create_with_items(db_session, obj_in=_create_in(company.id))
    crud_quote.update(db_session, quote=q2, obj_in=QuoteUpdate(status="sent"))
    db_session.commit()
    drafts = crud_quote.get_multi(db_session, status="draft")
    sents = crud_quote.get_multi(db_session, status="sent")
    assert q1.id in {q.id for q in drafts}
    assert q2.id in {q.id for q in sents}
    assert q2.id not in {q.id for q in drafts}


def test_get_multi_filters_company(db_session: Session, company_factory):
    c1 = company_factory(name="C1")
    c2 = company_factory(name="C2")
    q1 = crud_quote.create_with_items(db_session, obj_in=_create_in(c1.id))
    q2 = crud_quote.create_with_items(db_session, obj_in=_create_in(c2.id))
    db_session.commit()
    only_c1 = crud_quote.get_multi(db_session, company_id=c1.id)
    assert q1.id in {q.id for q in only_c1}
    assert q2.id not in {q.id for q in only_c1}


def test_count_respects_filters(db_session: Session, company_factory):
    c1 = company_factory()
    c2 = company_factory()
    crud_quote.create_with_items(db_session, obj_in=_create_in(c1.id))
    crud_quote.create_with_items(db_session, obj_in=_create_in(c1.id))
    crud_quote.create_with_items(db_session, obj_in=_create_in(c2.id))
    db_session.commit()
    assert crud_quote.count(db_session, company_id=c1.id) == 2
    assert crud_quote.count(db_session, company_id=c2.id) == 1


def test_get_multi_pagination_newest_first(db_session: Session, company_factory):
    company = company_factory()
    first = crud_quote.create_with_items(db_session, obj_in=_create_in(company.id))
    second = crud_quote.create_with_items(db_session, obj_in=_create_in(company.id))
    db_session.commit()
    page = crud_quote.get_multi(db_session, company_id=company.id, skip=0, limit=1)
    assert len(page) == 1
    assert page[0].id == max(first.id, second.id)


def test_update_partial_scalar_only(db_session: Session, company_factory):
    company = company_factory()
    q = crud_quote.create_with_items(db_session, obj_in=_create_in(company.id))
    db_session.commit()
    original_subtotal = q.subtotal
    crud_quote.update(db_session, quote=q, obj_in=QuoteUpdate(status="accepted", notes="LGTM"))
    db_session.commit()
    db_session.refresh(q)
    assert q.status == "accepted"
    assert q.notes == "LGTM"
    # items untouched → totals unchanged
    assert q.subtotal == original_subtotal
    assert len(q.items) == 1


def test_update_replaces_items_and_recomputes(db_session: Session, company_factory):
    company = company_factory()
    q = crud_quote.create_with_items(db_session, obj_in=_create_in(company.id))
    db_session.commit()
    old_item_ids = {i.id for i in q.items}
    crud_quote.update(
        db_session,
        quote=q,
        obj_in=QuoteUpdate(
            items=[
                QuoteItemCreate(description="New1", quantity=4, unit_price=2.5),
                QuoteItemCreate(description="New2", quantity=1, unit_price=1.0),
            ]
        ),
    )
    db_session.commit()
    db_session.refresh(q)
    assert len(q.items) == 2
    # old rows removed (delete-orphan)
    assert not (old_item_ids & {i.id for i in q.items})
    assert q.subtotal == 11.0
    assert q.total == 11.0
    # old item rows are gone from the table
    remaining = db_session.query(QuoteItem).filter(QuoteItem.id.in_(old_item_ids)).all()
    assert remaining == []


# ---------------------------------------------------------------------------
# convert_to_order
# ---------------------------------------------------------------------------


def _accepted_quote_with_product(
    db_session: Session,
    *,
    company_id: int,
    product,
    quantity: int = 2,
    unit_price: float = 12.0,
) -> Quote:
    """Create an accepted, product-backed quote ready to convert."""
    q = crud_quote.create_with_items(
        db_session,
        obj_in=_create_in(
            company_id,
            items=[
                QuoteItemCreate(
                    description=product.name,
                    quantity=quantity,
                    unit_price=unit_price,
                    product_id=product.id,
                )
            ],
        ),
    )
    crud_quote.update(db_session, quote=q, obj_in=QuoteUpdate(status="accepted"))
    db_session.commit()
    reloaded = crud_quote.get(db_session, q.id)
    assert reloaded is not None
    return reloaded


def test_convert_happy_path(db_session: Session, company_factory, user_factory, product_factory):
    actor = user_factory(email="actor-convert@example.com")
    contact = user_factory(email="contact-convert@example.com")
    company = company_factory(name="Convertible Inc", primary_user_id=contact.id)
    product = product_factory(stock=10, name="ConvWidget", sku="CONV-1")
    quote = _accepted_quote_with_product(
        db_session, company_id=company.id, product=product, quantity=3, unit_price=12.0
    )

    order = crud_quote.convert_to_order(db_session, quote=quote, actor_user_id=actor.id)
    db_session.commit()

    # Attribution = the company's primary contact, NOT the actor.
    assert order.user_id == contact.id
    assert order.status == OrderStatus.PENDING
    assert order.order_number.startswith("ORD-")
    assert order.currency == "USD"
    # Quoted price honoured (NOT re-priced from the product's price_usd=19.99).
    assert order.items[0].unit_price == 12.0
    assert order.items[0].quantity == 3
    assert order.items[0].total_price == 36.0
    # Snapshots come from the live product.
    assert order.items[0].product_name == "ConvWidget"
    assert order.items[0].product_sku == "CONV-1"
    # Totals from the quote; no shipping/discount/tax.
    assert order.subtotal == quote.subtotal == 36.0
    assert order.total == quote.total == 36.0
    assert order.shipping == 0.0
    assert order.discount == 0.0
    assert order.tax == 0.0
    assert order.shipping_method_id is None
    assert order.notes is not None
    assert f"Converted from quote #{quote.id}" in order.notes
    assert "Convertible Inc" in order.notes

    # Stock decremented from the default warehouse + ledger written.
    db_session.refresh(product)
    assert product.stock == 7
    movements = db_session.query(InventoryMovement).filter_by(product_id=product.id).all()
    assert len(movements) == 1
    assert movements[0].delta == -3
    assert movements[0].reason == "order_placed"
    assert movements[0].order_id == order.id
    assert movements[0].actor_user_id == actor.id  # the admin actor

    # Conversion marker set; quote status stays accepted.
    db_session.refresh(quote)
    assert quote.converted_order_id == order.id
    assert quote.status == "accepted"


def test_convert_not_accepted_raises(
    db_session: Session, company_factory, user_factory, product_factory
):
    contact = user_factory(email="na-contact@example.com")
    company = company_factory(primary_user_id=contact.id)
    product = product_factory(stock=10)
    # draft (default) — not accepted
    q = crud_quote.create_with_items(
        db_session,
        obj_in=_create_in(
            company.id,
            items=[
                QuoteItemCreate(description="x", quantity=1, unit_price=5.0, product_id=product.id)
            ],
        ),
    )
    db_session.commit()
    quote = crud_quote.get(db_session, q.id)
    assert quote is not None
    with pytest.raises(QuoteNotConvertibleError) as exc:
        crud_quote.convert_to_order(db_session, quote=quote, actor_user_id=1)
    assert exc.value.reason == "not_accepted"
    assert exc.value.status_code == 422


def test_convert_already_converted_raises(
    db_session: Session, company_factory, user_factory, product_factory
):
    actor = user_factory(email="dc-actor@example.com")
    contact = user_factory(email="dc-contact@example.com")
    company = company_factory(primary_user_id=contact.id)
    product = product_factory(stock=10)
    quote = _accepted_quote_with_product(db_session, company_id=company.id, product=product)
    crud_quote.convert_to_order(db_session, quote=quote, actor_user_id=actor.id)
    db_session.commit()
    db_session.refresh(quote)
    with pytest.raises(QuoteNotConvertibleError) as exc:
        crud_quote.convert_to_order(db_session, quote=quote, actor_user_id=actor.id)
    assert exc.value.reason == "already_converted"
    assert exc.value.status_code == 409


def test_convert_free_text_line_raises(
    db_session: Session, company_factory, user_factory, product_factory
):
    contact = user_factory(email="ft-contact@example.com")
    company = company_factory(primary_user_id=contact.id)
    product = product_factory(stock=10)
    q = crud_quote.create_with_items(
        db_session,
        obj_in=_create_in(
            company.id,
            items=[
                QuoteItemCreate(
                    description="backed", quantity=1, unit_price=5.0, product_id=product.id
                ),
                QuoteItemCreate(description="free text", quantity=1, unit_price=2.0),
            ],
        ),
    )
    crud_quote.update(db_session, quote=q, obj_in=QuoteUpdate(status="accepted"))
    db_session.commit()
    quote = crud_quote.get(db_session, q.id)
    assert quote is not None
    with pytest.raises(QuoteNotConvertibleError) as exc:
        crud_quote.convert_to_order(db_session, quote=quote, actor_user_id=1)
    assert exc.value.reason == "free_text_line"
    assert exc.value.status_code == 422


def test_convert_no_primary_contact_raises(db_session: Session, company_factory, product_factory):
    company = company_factory(primary_user_id=None)  # no contact
    product = product_factory(stock=10)
    quote = _accepted_quote_with_product(db_session, company_id=company.id, product=product)
    with pytest.raises(QuoteNotConvertibleError) as exc:
        crud_quote.convert_to_order(db_session, quote=quote, actor_user_id=1)
    assert exc.value.reason == "no_primary_contact"
    assert exc.value.status_code == 422


def test_convert_insufficient_stock_raises(
    db_session: Session, company_factory, user_factory, product_factory
):
    contact = user_factory(email="stock-contact@example.com")
    company = company_factory(primary_user_id=contact.id)
    product = product_factory(stock=1, allow_backorder=False)
    quote = _accepted_quote_with_product(
        db_session, company_id=company.id, product=product, quantity=5
    )
    with pytest.raises(InsufficientStockError):
        crud_quote.convert_to_order(db_session, quote=quote, actor_user_id=1)


def test_convert_inactive_product_raises(
    db_session: Session, company_factory, user_factory, product_factory
):
    contact = user_factory(email="inactive-contact@example.com")
    company = company_factory(primary_user_id=contact.id)
    product = product_factory(stock=10, is_active=False)
    quote = _accepted_quote_with_product(db_session, company_id=company.id, product=product)
    with pytest.raises(ProductUnavailableError):
        crud_quote.convert_to_order(db_session, quote=quote, actor_user_id=1)


def test_convert_no_items_raises(db_session: Session, company_factory, user_factory):
    contact = user_factory(email="noitems-contact@example.com")
    company = company_factory(primary_user_id=contact.id)
    # Build an accepted quote then strip its items to exercise the no_items guard
    # (the schema blocks empty items on create, so construct the empty state directly).
    quote = Quote(
        company_id=company.id,
        status="accepted",
        currency="USD",
        subtotal=0.0,
        total=0.0,
        items=[],
    )
    db_session.add(quote)
    db_session.commit()
    quote = crud_quote.get(db_session, quote.id)
    assert quote is not None
    with pytest.raises(QuoteNotConvertibleError) as exc:
        crud_quote.convert_to_order(db_session, quote=quote, actor_user_id=1)
    assert exc.value.reason == "no_items"


def test_delete_cascades_items(db_session: Session, company_factory):
    company = company_factory()
    q = crud_quote.create_with_items(db_session, obj_in=_create_in(company.id))
    db_session.commit()
    qid = q.id
    item_ids = [i.id for i in q.items]
    crud_quote.delete(db_session, quote=q)
    db_session.commit()
    assert db_session.get(Quote, qid) is None
    assert db_session.query(QuoteItem).filter(QuoteItem.id.in_(item_ids)).all() == []
