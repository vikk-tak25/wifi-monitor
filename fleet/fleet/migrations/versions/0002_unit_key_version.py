"""Unit key versions for derived signing keys (#9).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("units", sa.Column("key_version", sa.Integer, nullable=False, server_default="1"))


def downgrade() -> None:
    op.drop_column("units", "key_version")
