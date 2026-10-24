"""persist quote adjustments and currency on procurement requests

Revision ID: 0008
Revises: 0007
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "procurement_requests",
        sa.Column("adjustments", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.alter_column("procurement_requests", "adjustments", server_default=None)
    op.add_column(
        "procurement_requests",
        sa.Column("currency", sa.String(length=3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("procurement_requests", "currency")
    op.drop_column("procurement_requests", "adjustments")
