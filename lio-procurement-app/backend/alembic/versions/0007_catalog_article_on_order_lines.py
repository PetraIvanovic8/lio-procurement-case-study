"""record the negotiated catalog article applied to an order line

Revision ID: 0007
Revises: 0006
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "order_lines",
        sa.Column("catalog_article_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_order_lines_catalog_article_id",
        "order_lines",
        "articles",
        ["catalog_article_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_order_lines_catalog_article_id", "order_lines", ["catalog_article_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_order_lines_catalog_article_id", table_name="order_lines")
    op.drop_constraint(
        "fk_order_lines_catalog_article_id", "order_lines", type_="foreignkey"
    )
    op.drop_column("order_lines", "catalog_article_id")
