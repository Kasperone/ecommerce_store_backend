"""Unit tests for the order CSV export service (pure — no DB, no HTTP).

``build_orders_csv`` reads only model attributes, so these build in-memory
``Order``/``OrderItem``/``User`` instances and assert on the rendered bytes.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from app.models.order import Order, OrderItem, OrderStatus
from app.models.user import User
from app.services.order_export import CSV_HEADERS, build_orders_csv


def _item(name: str = "Widget", sku: str | None = "WD-1", qty: int = 2, unit: float = 50.0):
    return OrderItem(
        product_name=name,
        product_sku=sku,
        quantity=qty,
        unit_price=unit,
        total_price=round(unit * qty, 2),
    )


def _user(first: str = "Jan", last: str = "Kowalski", email: str = "jan@example.com"):
    return User(first_name=first, last_name=last, email=email)


def _order(
    *,
    items: list[OrderItem] | None = None,
    created: datetime = datetime(2026, 5, 28, 14, 30, 0),
    **overrides,
):
    defaults = dict(
        order_number="ORD-20260528-ABC123",
        status=OrderStatus.PAID,
        subtotal=100.0,
        tax=0.0,
        shipping=0.0,
        total=100.0,
        currency="PLN",
        shipping_address="ul. Łąkowa 1",
        shipping_city="Kraków",
        shipping_postal_code="30-001",
        shipping_country="Poland",
        payment_method="stripe",
        notes=None,
    )
    defaults.update(overrides)
    order = Order(**defaults)
    order.items = items if items is not None else [_item()]
    order.user = _user()
    order.created_at = created
    return order


def _parse(csv_bytes: bytes) -> list[list[str]]:
    """Decode (stripping the BOM) and parse the CSV back into rows."""
    text = csv_bytes.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def test_header_row_is_first_and_in_contract_order():
    rows = _parse(build_orders_csv([_order()]))
    assert rows[0] == CSV_HEADERS


def test_empty_list_renders_header_only():
    rows = _parse(build_orders_csv([]))
    assert rows == [CSV_HEADERS]


def test_bom_prefix_present():
    # utf-8-sig prepends the BOM so Excel on Windows detects UTF-8.
    assert build_orders_csv([_order()]).startswith(b"\xef\xbb\xbf")


def test_single_order_column_values():
    rows = _parse(build_orders_csv([_order()]))
    row = dict(zip(CSV_HEADERS, rows[1], strict=True))
    assert row["order_number"] == "ORD-20260528-ABC123"
    assert row["status"] == "paid"  # raw enum value, not localized
    assert row["customer_email"] == "jan@example.com"
    assert row["customer_name"] == "Jan Kowalski"
    assert row["currency"] == "PLN"
    assert row["total"] == "100.00"
    assert row["item_count"] == "1"
    assert row["items"] == "2× Widget (WD-1)"
    assert row["payment_method"] == "stripe"


def test_items_packed_multiple_and_null_sku():
    order = _order(
        items=[_item(name="Widget", sku="WD-1", qty=2), _item(name="Gadget", sku=None, qty=1)]
    )
    rows = _parse(build_orders_csv([order]))
    row = dict(zip(CSV_HEADERS, rows[1], strict=True))
    assert row["items"] == "2× Widget (WD-1); 1× Gadget"
    assert row["item_count"] == "2"


def test_money_columns_are_two_decimals():
    order = _order(subtotal=5.0, tax=0.0, shipping=0.0, total=5.0)
    rows = _parse(build_orders_csv([order]))
    row = dict(zip(CSV_HEADERS, rows[1], strict=True))
    assert row["subtotal"] == "5.00"
    assert row["tax"] == "0.00"
    assert row["shipping"] == "0.00"
    assert row["total"] == "5.00"


def test_created_at_is_iso8601_utc():
    rows = _parse(build_orders_csv([_order(created=datetime(2026, 5, 28, 14, 30, 0))]))
    row = dict(zip(CSV_HEADERS, rows[1], strict=True))
    assert row["created_at"] == "2026-05-28T14:30:00Z"


def test_created_at_blank_when_unset():
    order = _order()
    # created_at is a non-optional Mapped column, but an unflushed/malformed
    # snapshot could be None — exercise the exporter's guard.
    order.created_at = None  # type: ignore[assignment]
    rows = _parse(build_orders_csv([order]))
    row = dict(zip(CSV_HEADERS, rows[1], strict=True))
    assert row["created_at"] == ""


def test_rfc4180_quoting_round_trips_comma_and_newline():
    # A comma + embedded newline in notes must survive a parse round-trip.
    tricky = 'Fragile, handle with care\nLeave at "back" door'
    rows = _parse(build_orders_csv([_order(notes=tricky)]))
    row = dict(zip(CSV_HEADERS, rows[1], strict=True))
    assert row["notes"] == tricky


def test_polish_diacritics_preserved():
    rows = _parse(build_orders_csv([_order(shipping_city="Kraków", shipping_address="ul. Łąka 5")]))
    row = dict(zip(CSV_HEADERS, rows[1], strict=True))
    assert row["shipping_city"] == "Kraków"
    assert row["shipping_address"] == "ul. Łąka 5"


def test_blank_customer_name_when_no_user():
    order = _order()
    # user is a non-optional Mapped relationship; clear it to exercise the
    # exporter's no-user guard (customer_name/email fall back to blank).
    order.user = None  # type: ignore[assignment]
    rows = _parse(build_orders_csv([order]))
    row = dict(zip(CSV_HEADERS, rows[1], strict=True))
    assert row["customer_name"] == ""
    assert row["customer_email"] == ""


def test_formula_injection_in_notes_is_neutralized():
    # A customer-controlled note starting with '=' must be quoted so Excel
    # treats it as literal text, not a formula (SEC-008 / CWE-1236).
    rows = _parse(build_orders_csv([_order(notes="=cmd|'/c calc'!A1")]))
    row = dict(zip(CSV_HEADERS, rows[1], strict=True))
    assert row["notes"] == "'=cmd|'/c calc'!A1"


def test_formula_injection_neutralized_across_user_text_columns():
    order = _order(
        order_number="=ORD",
        shipping_address="@evil",
        shipping_city="+city",
        coupon_code="-SAVE",
        notes="=HYPERLINK(1)",
    )
    rows = _parse(build_orders_csv([order]))
    row = dict(zip(CSV_HEADERS, rows[1], strict=True))
    assert row["order_number"] == "'=ORD"
    assert row["shipping_address"] == "'@evil"
    assert row["shipping_city"] == "'+city"
    assert row["coupon_code"] == "'-SAVE"
    assert row["notes"] == "'=HYPERLINK(1)"


def test_multiple_orders_one_row_each():
    rows = _parse(
        build_orders_csv(
            [
                _order(order_number="ORD-1"),
                _order(order_number="ORD-2"),
                _order(order_number="ORD-3"),
            ]
        )
    )
    assert len(rows) == 4  # header + 3
    assert [r[0] for r in rows[1:]] == ["ORD-1", "ORD-2", "ORD-3"]
