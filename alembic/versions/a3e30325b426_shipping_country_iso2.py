"""Tighten shipping_country to ISO 3166-1 alpha-2

``orders.shipping_country`` was ``String(100)`` free text, but it is passed
*directly* as ``country_code`` into the shipping-rate resolver
(``app/crud/order.py`` → ``resolve_shipping_for``), which matches
``shipping_zone_countries.country_code`` — ``String(2)`` with an ISO-2 CHECK.
Rows holding ``'Poland'`` therefore never matched a zone and silently got no
rate. Destination VAT resolution will key off the same field, so the looseness
has to go before tax work starts.

The column also becomes **nullable**: quote-converted orders genuinely have no
address yet (``app/crud/quote.py`` used to write the placeholder ``'TBD'``),
and NULL states that honestly instead of smuggling a non-country through a
country column.

Legacy values are backfilled from a small alias map. Anything left unmapped
**aborts the migration** with the offending values listed — a country cannot be
guessed, and silently dropping or defaulting one would be exactly the quiet
data corruption this migration exists to end.

Revision ID: a3e30325b426
Revises: 595be6ef39a4
Create Date: 2026-07-21

"""

import sqlalchemy as sa
from alembic import op

revision = "a3e30325b426"
down_revision = "595be6ef39a4"
branch_labels = None
depends_on = None


# Free-text values seen in the wild, mapped to their ISO-2 code. Deliberately
# tiny and explicit — this is one-time cleanup, not a general country library.
ALIASES = {
    "POLAND": "PL",
    "POLSKA": "PL",
    "PL": "PL",
    "USA": "US",
    "UNITED STATES": "US",
    "US": "US",
    "GERMANY": "DE",
    "DEUTSCHLAND": "DE",
    "NIEMCY": "DE",
    "DE": "DE",
    "UNITED KINGDOM": "GB",
    "GB": "GB",
    "UK": "GB",
}

# Placeholders written by paths that had no address to record. These become NULL.
PLACEHOLDERS = {"TBD", "TBD — SET AT FULFILMENT", "", "-", "N/A"}

TABLES = ("orders", "draft_orders")


def _clean(table: str) -> None:
    """Normalize one table's country column, aborting on anything unrecognised."""
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            f"SELECT DISTINCT shipping_country FROM {table} "  # noqa: S608 -- table from a fixed local tuple
            "WHERE shipping_country IS NOT NULL"
        )
    ).fetchall()

    unmapped = []
    for (value,) in rows:
        key = value.strip().upper()
        if key in PLACEHOLDERS:
            target = None
        elif key in ALIASES:
            target = ALIASES[key]
        elif len(key) == 2 and key.isalpha():
            target = key  # already a code, possibly lower-cased or padded
        else:
            unmapped.append(value)
            continue
        conn.execute(
            sa.text(
                f"UPDATE {table} SET shipping_country = :target "  # noqa: S608 -- see above
                "WHERE shipping_country = :value"
            ),
            {"target": target, "value": value},
        )

    if unmapped:
        raise RuntimeError(
            f"{table}.shipping_country holds values that are not ISO-2 codes and have no "
            f"alias mapping: {sorted(unmapped)}. Map them in this migration's ALIASES or "
            "correct the rows by hand, then re-run. Refusing to guess a country."
        )


def upgrade() -> None:
    # Drop NOT NULL first: the cleanup below turns placeholders into NULL, and
    # ``orders`` was NOT NULL. Quote-converted orders now legitimately hold NULL.
    op.alter_column(
        "orders",
        "shipping_country",
        existing_type=sa.String(length=100),
        existing_nullable=False,
        nullable=True,
    )

    for table in TABLES:
        _clean(table)

    # Narrowing to String(2) only after cleanup, so a legacy 'Poland' can't be
    # silently truncated to 'Po' by the type change.
    for table in TABLES:
        op.alter_column(
            table,
            "shipping_country",
            existing_type=sa.String(length=100),
            type_=sa.String(length=2),
            existing_nullable=True,
        )

    op.create_check_constraint(
        "ck_orders_shipping_country_iso2",
        "orders",
        "shipping_country IS NULL OR shipping_country ~ '^[A-Z]{2}$'",
    )
    op.create_check_constraint(
        "ck_draft_orders_shipping_country_iso2",
        "draft_orders",
        "shipping_country IS NULL OR shipping_country ~ '^[A-Z]{2}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_draft_orders_shipping_country_iso2", "draft_orders", type_="check")
    op.drop_constraint("ck_orders_shipping_country_iso2", "orders", type_="check")
    # Widen both columns before restoring the placeholder — 'TBD' does not fit
    # in String(2).
    for table in TABLES:
        op.alter_column(
            table,
            "shipping_country",
            existing_type=sa.String(length=2),
            type_=sa.String(length=100),
            existing_nullable=True,
        )
    # Restoring NOT NULL requires a value for the quote-converted rows; the old
    # placeholder is the only truthful thing available. Note the downgrade does
    # not restore free-text spellings ('Poland' stays 'PL') — normalization is
    # deliberately one-way.
    op.execute("UPDATE orders SET shipping_country = 'TBD' WHERE shipping_country IS NULL")
    op.alter_column(
        "orders",
        "shipping_country",
        existing_type=sa.String(length=100),
        existing_nullable=True,
        nullable=False,
    )
