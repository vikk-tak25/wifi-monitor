"""Database tables (SQLAlchemy Core). The schema itself is created by Alembic migrations."""

from __future__ import annotations

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, LargeBinary, MetaData,
                        SmallInteger, String, Table, Text)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

JSONType = JSON().with_variant(JSONB(), "postgresql")

units = Table(
    "units", metadata,
    Column("id", String(16), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("notes", Text),
    Column("mac", String(17)),
    Column("key_hash", String(64), nullable=False),
    Column("key_version", Integer, nullable=False),
    Column("config", JSONType, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    Column("last_seen_at", DateTime(timezone=True)),
    Column("last_hello_at", DateTime(timezone=True)),
    Column("last_ip", String(45)),
    Column("online", Boolean, nullable=False),
    Column("agent_version", String(32)),
    Column("fw_version", String(32)),
    Column("chip", String(64)),
    Column("last_rssi", SmallInteger),
    Column("last_bssid", String(17)),
    Column("last_channel", SmallInteger),
    Column("last_ssid", String(32)),
)

admin_sessions = Table(
    "admin_sessions", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("client", String(45)),
)

ingest_batches = Table(
    "ingest_batches", metadata,
    Column("unit_id", String(16), primary_key=True),
    Column("batch_id", String(64), primary_key=True),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("clock_skew_s", Float),
    Column("samples", Integer, nullable=False),
    Column("scans", Integer, nullable=False),
    Column("events", Integer, nullable=False),
)

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("time", DateTime(timezone=True), nullable=False),
    Column("unit_id", String(16)),
    Column("source", String(8), nullable=False),  # "unit" or "server"
    Column("level", String(8), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("message", Text, nullable=False),
    Column("detail", JSONType),
)

settings = Table(
    "settings", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", JSONType, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

floor_plans = Table(
    "floor_plans", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(80), nullable=False),
    Column("image", LargeBinary, nullable=False),
    Column("content_type", String(32), nullable=False),
    Column("size", Integer, nullable=False),
    Column("sort_order", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

unit_placements = Table(
    "unit_placements", metadata,
    Column("unit_id", String(16), ForeignKey("units.id", ondelete="CASCADE"), primary_key=True),
    Column("floor_id", Integer, ForeignKey("floor_plans.id", ondelete="CASCADE"), nullable=False),
    Column("x", Float, nullable=False),
    Column("y", Float, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Hypertables (TimescaleDB): no primary keys, partitioned by time.

link_samples = Table(
    "link_samples", metadata,
    Column("time", DateTime(timezone=True), nullable=False),
    Column("unit_id", String(16), nullable=False),
    Column("connected", Boolean, nullable=False),
    Column("rssi", SmallInteger),
    Column("bssid", String(17)),
    Column("channel", SmallInteger),
    Column("gw_ms", Float),
    Column("gw_lost", Boolean),
    Column("srv_ms", Float),
    Column("srv_lost", Boolean),
)

scans = Table(
    "scans", metadata,
    Column("time", DateTime(timezone=True), nullable=False),
    Column("unit_id", String(16), nullable=False),
    Column("duration_ms", Integer),
    Column("ap_count", SmallInteger, nullable=False),
)

scan_observations = Table(
    "scan_observations", metadata,
    Column("time", DateTime(timezone=True), nullable=False),
    Column("unit_id", String(16), nullable=False),
    Column("bssid", String(17), nullable=False),
    Column("ssid", String(32), nullable=False),
    Column("channel", SmallInteger, nullable=False),
    Column("rssi", SmallInteger, nullable=False),
    Column("auth", SmallInteger, nullable=False),
    Column("hidden", Boolean, nullable=False),
)
