"""Database engine and small shared helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, insert

from .tables import events


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def add_event(conn: Connection, *, time: datetime, unit_id: str | None, kind: str, message: str,
              level: str = "info", source: str = "server", detail: dict[str, Any] | None = None) -> None:
    conn.execute(insert(events).values(time=time, unit_id=unit_id, source=source, level=level,
                                       kind=kind, message=message, detail=detail))
