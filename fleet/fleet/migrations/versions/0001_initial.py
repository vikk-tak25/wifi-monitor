"""Initial schema: units, admin sessions, ingest batches, events and measurement hypertables.

Revision ID: 0001
Revises:
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "units",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("mac", sa.String(17)),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
        sa.Column("last_seen_at", TS),
        sa.Column("last_hello_at", TS),
        sa.Column("last_ip", sa.String(45)),
        sa.Column("online", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("agent_version", sa.String(32)),
        sa.Column("fw_version", sa.String(32)),
        sa.Column("chip", sa.String(64)),
        sa.Column("last_rssi", sa.SmallInteger),
        sa.Column("last_bssid", sa.String(17)),
        sa.Column("last_channel", sa.SmallInteger),
        sa.Column("last_ssid", sa.String(32)),
    )
    op.create_index("units_mac", "units", ["mac"])

    op.create_table(
        "admin_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("client", sa.String(45)),
    )

    op.create_table(
        "ingest_batches",
        sa.Column("unit_id", sa.String(16), sa.ForeignKey("units.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("clock_skew_s", sa.Float),
        sa.Column("samples", sa.Integer, nullable=False),
        sa.Column("scans", sa.Integer, nullable=False),
        sa.Column("events", sa.Integer, nullable=False),
    )
    op.create_index("ingest_batches_received", "ingest_batches", ["received_at"])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("time", TS, nullable=False),
        sa.Column("unit_id", sa.String(16), sa.ForeignKey("units.id", ondelete="CASCADE")),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("detail", JSONB),
    )
    op.create_index("events_time", "events", [sa.text("time DESC")])
    op.create_index("events_unit_time", "events", ["unit_id", sa.text("time DESC")])

    op.create_table(
        "link_samples",
        sa.Column("time", TS, nullable=False),
        sa.Column("unit_id", sa.String(16), nullable=False),
        sa.Column("connected", sa.Boolean, nullable=False),
        sa.Column("rssi", sa.SmallInteger),
        sa.Column("bssid", sa.String(17)),
        sa.Column("channel", sa.SmallInteger),
        sa.Column("gw_ms", sa.Float),
        sa.Column("gw_lost", sa.Boolean),
        sa.Column("srv_ms", sa.Float),
        sa.Column("srv_lost", sa.Boolean),
    )
    op.create_table(
        "scans",
        sa.Column("time", TS, nullable=False),
        sa.Column("unit_id", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("ap_count", sa.SmallInteger, nullable=False),
    )
    op.create_table(
        "scan_observations",
        sa.Column("time", TS, nullable=False),
        sa.Column("unit_id", sa.String(16), nullable=False),
        sa.Column("bssid", sa.String(17), nullable=False),
        sa.Column("ssid", sa.String(32), nullable=False),
        sa.Column("channel", sa.SmallInteger, nullable=False),
        sa.Column("rssi", sa.SmallInteger, nullable=False),
        sa.Column("auth", sa.SmallInteger, nullable=False),
        sa.Column("hidden", sa.Boolean, nullable=False),
    )
    for table, chunk in (("link_samples", "1 day"), ("scans", "7 days"), ("scan_observations", "7 days")):
        op.execute(f"SELECT create_hypertable('{table}', by_range('time', INTERVAL '{chunk}'))")
        op.execute(f"CREATE INDEX {table}_unit_time ON {table} (unit_id, time DESC)")


def downgrade() -> None:
    for table in ("scan_observations", "scans", "link_samples", "events", "ingest_batches",
                  "admin_sessions", "units"):
        op.drop_table(table)
