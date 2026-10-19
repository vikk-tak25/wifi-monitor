"""Floor plans and unit placements for the heat map (#6).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "floor_plans",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("image", sa.LargeBinary, nullable=False),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "unit_placements",
        sa.Column("unit_id", sa.String(16), sa.ForeignKey("units.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("floor_id", sa.Integer, sa.ForeignKey("floor_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("x", sa.Float, nullable=False),
        sa.Column("y", sa.Float, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint("x >= 0 AND x <= 1 AND y >= 0 AND y <= 1", name="placement_inside_plan"),
    )
    op.create_index("unit_placements_floor", "unit_placements", ["floor_id"])


def downgrade() -> None:
    op.drop_table("unit_placements")
    op.drop_table("floor_plans")
