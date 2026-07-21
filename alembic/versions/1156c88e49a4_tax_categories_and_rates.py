"""Tax categories + per-country rates; backfill from product.vat_rate

Phase 0 of the tax program. ``product.vat_rate: int`` stores *the answer for
Poland* having discarded *the question* — a book is 5% in PL, 7% in DE, 0% in the
UK — so a literal rate cannot be reprojected onto another country. This migration
introduces the question (``tax_categories``) and resolves the answer per country
and date (``tax_rates``).

The backfill is mechanical **now**, while the catalog is small and
``vat_rate → category`` is still obvious. Once a second country is live, or the
catalog has grown, the same mapping becomes a manual per-SKU audit. That is the
whole reason this ships before it is needed.

**No behaviour change for a Poland-only store**: the seeded rates reproduce
exactly what ``product.vat_rate`` returned. ``product.vat_rate`` is retained as a
mirror and dropped in a later migration, once the admin UI edits categories.

Revision ID: 1156c88e49a4
Revises: a3e30325b426
Create Date: 2026-07-21

"""

import sqlalchemy as sa
from alembic import op

revision = "1156c88e49a4"
down_revision = "a3e30325b426"
branch_labels = None
depends_on = None


# vat_rate → (category code, English name, Polish name, is_default).
# Poland's rates as of 2026; the standard rate has been 23% since 2011-01-01,
# which is the validity start used for the seeded rows.
CATEGORIES = [
    (23, "STANDARD", "Standard rate", "Stawka podstawowa", True),
    (8, "REDUCED_8", "Reduced rate (8%)", "Stawka obniżona (8%)", False),
    (5, "REDUCED_5", "Reduced rate (5%)", "Stawka obniżona (5%)", False),
    (0, "ZERO", "Zero-rated", "Stawka zerowa", False),
]

HOME_COUNTRY = "PL"
RATES_VALID_FROM = "2011-01-01"


def upgrade() -> None:
    op.create_table(
        "tax_categories",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("name_pl", sa.String(length=100), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_tax_categories_code", "tax_categories", ["code"], unique=True)
    # Exactly one default, enforced by the database rather than by application
    # logic — two defaults must not be possible even transiently.
    op.create_index(
        "uq_tax_categories_single_default",
        "tax_categories",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.create_table(
        "tax_rates",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("tax_category_id", sa.Integer(), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("rate_pct", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tax_category_id"], ["tax_categories.id"], ondelete="CASCADE"),
        sa.CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="ck_tax_rates_country_iso2"),
        sa.CheckConstraint("rate_pct >= 0 AND rate_pct <= 100", name="ck_tax_rates_rate_range"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_tax_rates_valid_window"
        ),
        sa.UniqueConstraint(
            "tax_category_id",
            "country_code",
            "valid_from",
            name="uq_tax_rates_category_country_from",
        ),
    )
    op.create_index("ix_tax_rates_tax_category_id", "tax_rates", ["tax_category_id"])
    op.create_index("ix_tax_rates_country_code", "tax_rates", ["country_code"])

    conn = op.get_bind()
    category_ids: dict[int, int] = {}
    for rate, code, name, name_pl, is_default in CATEGORIES:
        category_id = conn.execute(
            sa.text(
                "INSERT INTO tax_categories (code, name, name_pl, is_default, "
                "created_at, updated_at) VALUES (:code, :name, :name_pl, :is_default, "
                "NOW(), NOW()) RETURNING id"
            ),
            {"code": code, "name": name, "name_pl": name_pl, "is_default": is_default},
        ).scalar_one()
        category_ids[rate] = category_id
        conn.execute(
            sa.text(
                "INSERT INTO tax_rates (tax_category_id, country_code, rate_pct, "
                "valid_from, created_at, updated_at) VALUES (:cid, :country, :rate, "
                ":valid_from, NOW(), NOW())"
            ),
            {
                "cid": category_id,
                "country": HOME_COUNTRY,
                "rate": rate,
                "valid_from": RATES_VALID_FROM,
            },
        )

    op.add_column("products", sa.Column("tax_category_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_products_tax_category_id",
        "products",
        "tax_categories",
        ["tax_category_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_products_tax_category_id", "products", ["tax_category_id"])

    # Backfill. A product carrying a rate with no matching category would be
    # silently mis-taxed if defaulted, so it aborts instead — the operator adds
    # the category and re-runs.
    known = ", ".join(str(rate) for rate in category_ids)  # ints from CATEGORIES above
    unmapped = conn.execute(
        sa.text(f"SELECT DISTINCT vat_rate FROM products WHERE vat_rate NOT IN ({known})")  # noqa: S608 -- interpolates ints from a module constant
    ).fetchall()
    if unmapped:
        raise RuntimeError(
            f"products.vat_rate holds rates with no seeded tax category: "
            f"{sorted(r[0] for r in unmapped)}. Add them to CATEGORIES in this migration "
            "(code + names) and re-run, rather than letting them fall back to the default."
        )

    for rate, category_id in category_ids.items():
        conn.execute(
            sa.text("UPDATE products SET tax_category_id = :cid WHERE vat_rate = :rate"),
            {"cid": category_id, "rate": rate},
        )


def downgrade() -> None:
    op.drop_index("ix_products_tax_category_id", table_name="products")
    op.drop_constraint("fk_products_tax_category_id", "products", type_="foreignkey")
    op.drop_column("products", "tax_category_id")
    op.drop_table("tax_rates")
    op.drop_index("uq_tax_categories_single_default", table_name="tax_categories")
    op.drop_table("tax_categories")
