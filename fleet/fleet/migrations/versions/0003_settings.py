"""Server settings (Wi-Fi credentials for provisioning, label template) (#4).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("settings")
