"""Integration tests for product image behaviour.

Acceptance coverage for PR0b of the admin-catalog foundation plan
(``ecommerce_store/.claude/notes/plans/admin/pending/2026-05-18-admin-catalog-foundation.md``):

* ``products.images`` JSON column is gone; ``product_images`` table is its
  replacement.
* Public GET endpoints return ``images: []`` for products without any image.
* When images exist, they serialise in ``sort_order`` and the response
  carries the full ``ProductImageResponse`` metadata.
* Partial unique index ``uq_product_images_primary_per_product`` prevents
  two ``is_primary=true`` rows for the same product.
* ``CRUDProductImage.set_primary`` demotes siblings transactionally.
* Cascade-delete on the product removes its images.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.crud import product_image as crud_product_image
from app.models.product_image import ProductImage


def _image(product_id: int, **overrides) -> ProductImage:
    """Build a ProductImage with required column defaults filled in."""
    defaults = {
        "product_id": product_id,
        "url": "https://cdn.example.com/img.webp",
        "alt_text": None,
        "sort_order": 0,
        "is_primary": False,
        "width": 800,
        "height": 600,
        "bytes": 12_345,
        "mime": "image/webp",
    }
    defaults.update(overrides)
    return ProductImage(**defaults)


# ---------------------------------------------------------------------------
# Public read endpoints
# ---------------------------------------------------------------------------


async def test_get_product_by_id_returns_empty_images_when_none(client, product_factory):
    product = product_factory()

    response = await client.get(f"/api/v1/products/{product.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == product.id
    assert body["images"] == []


async def test_get_product_by_slug_returns_empty_images_when_none(client, product_factory):
    product = product_factory(slug="empty-image-product")

    response = await client.get(f"/api/v1/products/slug/{product.slug}")

    assert response.status_code == 200
    assert response.json()["images"] == []


async def test_list_products_returns_empty_images_per_row(client, product_factory):
    p1 = product_factory(slug="list-empty-1")
    p2 = product_factory(slug="list-empty-2")

    response = await client.get("/api/v1/products/")

    assert response.status_code == 200
    body = response.json()
    by_id = {item["id"]: item for item in body["items"]}
    for pid in (p1.id, p2.id):
        assert pid in by_id
        assert by_id[pid]["images"] == []


async def test_product_response_serialises_images_in_sort_order(
    client, db_session, product_factory
):
    product = product_factory(slug="ordered-images")
    db_session.add_all(
        [
            _image(product.id, url="https://cdn/2.webp", sort_order=2),
            _image(product.id, url="https://cdn/0.webp", sort_order=0),
            _image(product.id, url="https://cdn/1.webp", sort_order=1, is_primary=True),
        ]
    )
    db_session.commit()

    response = await client.get(f"/api/v1/products/{product.id}")

    assert response.status_code == 200
    urls = [img["url"] for img in response.json()["images"]]
    assert urls == [
        "https://cdn/0.webp",
        "https://cdn/1.webp",
        "https://cdn/2.webp",
    ]


# ---------------------------------------------------------------------------
# Partial unique index — at most one primary per product
# ---------------------------------------------------------------------------


def test_two_primary_images_for_same_product_trip_integrity_error(db_session, product_factory):
    product = product_factory(slug="dual-primary")
    db_session.add(_image(product.id, url="https://cdn/a.webp", is_primary=True))
    db_session.commit()

    db_session.add(_image(product.id, url="https://cdn/b.webp", is_primary=True))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_two_non_primary_images_for_same_product_are_allowed(db_session, product_factory):
    """Partial index only covers ``is_primary=true`` — non-primary duplicates are fine."""
    product = product_factory(slug="dual-non-primary")
    db_session.add(_image(product.id, url="https://cdn/a.webp", is_primary=False))
    db_session.add(_image(product.id, url="https://cdn/b.webp", is_primary=False))
    db_session.commit()  # no IntegrityError

    rows = crud_product_image.list_for_product(db_session, product_id=product.id)
    assert len(rows) == 2


def test_primary_image_allowed_for_each_distinct_product(db_session, product_factory):
    """The unique constraint is scoped per product_id."""
    p1 = product_factory(slug="distinct-1")
    p2 = product_factory(slug="distinct-2")
    db_session.add(_image(p1.id, is_primary=True))
    db_session.add(_image(p2.id, is_primary=True))
    db_session.commit()  # no IntegrityError


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


def test_set_primary_demotes_existing_primary(db_session, product_factory):
    product = product_factory(slug="set-primary")
    old_primary = _image(product.id, url="https://cdn/old.webp", is_primary=True)
    new_primary = _image(product.id, url="https://cdn/new.webp", is_primary=False)
    db_session.add_all([old_primary, new_primary])
    db_session.commit()

    result = crud_product_image.set_primary(
        db_session, product_id=product.id, image_id=new_primary.id
    )
    db_session.commit()

    assert result is not None
    assert result.id == new_primary.id
    db_session.refresh(old_primary)
    db_session.refresh(new_primary)
    assert new_primary.is_primary is True
    assert old_primary.is_primary is False


def test_set_primary_returns_none_for_image_outside_product(db_session, product_factory):
    p1 = product_factory(slug="set-primary-mismatch-1")
    p2 = product_factory(slug="set-primary-mismatch-2")
    img = _image(p1.id, is_primary=True)
    db_session.add(img)
    db_session.commit()

    # Caller asks to set primary on p2 for an image that belongs to p1.
    result = crud_product_image.set_primary(db_session, product_id=p2.id, image_id=img.id)
    assert result is None


# ---------------------------------------------------------------------------
# Cascade
# ---------------------------------------------------------------------------


def test_delete_product_cascades_to_its_images(db_session, product_factory):
    product = product_factory(slug="cascade-delete")
    db_session.add_all(
        [
            _image(product.id, url="https://cdn/keep1.webp"),
            _image(product.id, url="https://cdn/keep2.webp", is_primary=True),
        ]
    )
    db_session.commit()

    assert db_session.query(ProductImage).filter(ProductImage.product_id == product.id).count() == 2

    db_session.delete(product)
    db_session.commit()

    assert db_session.query(ProductImage).filter(ProductImage.product_id == product.id).count() == 0


# ---------------------------------------------------------------------------
# Negative stock under backorder serialises cleanly
#
# Regression for the response-vs-runtime invariant collision documented in
# ``notes/plans/test/fixed/2026-05-30-product-stock-ge0-vs-backorder-backend.md``:
# ``crud_inventory.apply_delta(..., allow_negative=True)`` may legitimately
# land ``Product.stock`` below zero when an order is placed on a backorder
# product for more units than are on hand. Before the fix, ``ProductBase``
# carried ``Field(ge=0)`` on ``stock``, which ``ProductResponse`` inherited
# verbatim — so the very next ``GET /products/`` page that included that row
# raised ``ResponseValidationError`` and the route 500'd. The fix moves the
# floor onto ``ProductCreate`` (where caller-supplied stock genuinely can't
# be negative) and lets the read shape accept the real value.
# ---------------------------------------------------------------------------


async def test_list_does_not_500_on_negative_stock_backorder(client, product_factory, db_session):
    """A backorder product with negative stock must serialise cleanly."""
    product = product_factory(slug="backorder-negative-stock-list", stock=10, allow_backorder=True)
    # Direct write — simulates the legal end-state of ``apply_delta`` with
    # ``allow_negative=True`` for an order whose quantity exceeded stock.
    product.stock = -3
    db_session.commit()

    response = await client.get("/api/v1/products/")

    assert response.status_code == 200
    items = response.json()["items"]
    found = next((it for it in items if it["id"] == product.id), None)
    assert found is not None
    assert found["stock"] == -3
    # ``in_stock`` is the computed_field on ProductResponse — flips at <=0.
    assert found["in_stock"] is False


async def test_get_by_slug_does_not_500_on_negative_stock_backorder(
    client, product_factory, db_session
):
    """Detail endpoint mirrors the list path — same response schema."""
    product = product_factory(slug="backorder-negative-stock-detail", stock=5, allow_backorder=True)
    product.stock = -1
    db_session.commit()

    response = await client.get(f"/api/v1/products/slug/{product.slug}")

    assert response.status_code == 200
    body = response.json()
    assert body["stock"] == -1
    assert body["in_stock"] is False


def test_product_create_still_rejects_negative_stock():
    """The ``ge=0`` floor was moved off the base, not deleted — ``ProductCreate``
    keeps the create-time invariant. Negative initial stock is never the
    customer-facing API caller's prerogative; orders own that path.
    """
    import pytest as _pytest
    from pydantic import ValidationError

    from app.schemas.product import ProductCreate

    with _pytest.raises(ValidationError) as exc_info:
        ProductCreate(
            name="Bad",
            slug="bad",
            price_usd=1.0,
            stock=-1,
        )
    assert any(err["loc"] == ("stock",) for err in exc_info.value.errors())
