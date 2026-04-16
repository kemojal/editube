"""add stripe catalog tables for product/price sync

Revision ID: q1w2e3r4t5y6
Revises: p9q8r7s6t5u4
Create Date: 2026-04-16 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "q1w2e3r4t5y6"
down_revision = "p9q8r7s6t5u4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stripe_products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stripe_product_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_stripe_products_id"), "stripe_products", ["id"], unique=False)
    op.create_index(
        op.f("ix_stripe_products_stripe_product_id"),
        "stripe_products",
        ["stripe_product_id"],
        unique=True,
    )

    op.create_table(
        "stripe_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stripe_price_id", sa.String(), nullable=False),
        sa.Column("stripe_product_id", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=True),
        sa.Column("unit_amount", sa.Integer(), nullable=True),
        sa.Column("nickname", sa.String(), nullable=True),
        sa.Column("recurring_interval", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("editube_plan", sa.String(), nullable=True),
        sa.Column("editube_interval", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["stripe_product_id"],
            ["stripe_products.stripe_product_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_stripe_prices_id"), "stripe_prices", ["id"], unique=False)
    op.create_index(
        op.f("ix_stripe_prices_stripe_price_id"),
        "stripe_prices",
        ["stripe_price_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_stripe_prices_stripe_product_id"),
        "stripe_prices",
        ["stripe_product_id"],
        unique=False,
    )
    op.create_index(op.f("ix_stripe_prices_editube_plan"), "stripe_prices", ["editube_plan"], unique=False)
    op.create_index(
        op.f("ix_stripe_prices_editube_interval"),
        "stripe_prices",
        ["editube_interval"],
        unique=False,
    )

    op.create_index(
        "uq_stripe_prices_active_plan_interval",
        "stripe_prices",
        ["editube_plan", "editube_interval"],
        unique=True,
        postgresql_where=sa.text(
            "active IS TRUE AND editube_plan IS NOT NULL AND editube_interval IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_stripe_prices_active_plan_interval", table_name="stripe_prices")
    op.drop_index(op.f("ix_stripe_prices_editube_interval"), table_name="stripe_prices")
    op.drop_index(op.f("ix_stripe_prices_editube_plan"), table_name="stripe_prices")
    op.drop_index(op.f("ix_stripe_prices_stripe_product_id"), table_name="stripe_prices")
    op.drop_index(op.f("ix_stripe_prices_stripe_price_id"), table_name="stripe_prices")
    op.drop_index(op.f("ix_stripe_prices_id"), table_name="stripe_prices")
    op.drop_table("stripe_prices")
    op.drop_index(op.f("ix_stripe_products_stripe_product_id"), table_name="stripe_products")
    op.drop_index(op.f("ix_stripe_products_id"), table_name="stripe_products")
    op.drop_table("stripe_products")
