"""Unit registry: creation, keys, revocation, configuration and online status."""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Row, and_, insert, select, update

from . import security
from .config import Settings
from .db import add_event
from .schemas import UnitCreate, UnitUpdate
from .tables import units

PUBLIC_FIELDS = ("id", "name", "notes", "mac", "config", "created_at", "revoked_at", "last_seen_at",
                 "last_hello_at", "last_ip", "online", "agent_version", "fw_version", "chip",
                 "last_rssi", "last_bssid", "last_channel", "last_ssid")


def effective_config(settings: Settings, overrides: dict[str, Any] | None) -> dict[str, Any]:
    config = settings.unit_defaults.as_dict()
    config.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return config


def public_unit(row: Row | dict[str, Any], settings: Settings) -> dict[str, Any]:
    data = dict(row._mapping) if isinstance(row, Row) else dict(row)
    out = {key: data.get(key) for key in PUBLIC_FIELDS}
    out["status"] = "revoked" if data.get("revoked_at") else "online" if data.get("online") else (
        "never_seen" if data.get("last_seen_at") is None else "offline")
    out["effective_config"] = effective_config(settings, data.get("config"))
    return out


def create_unit(conn: Connection, settings: Settings, data: UnitCreate, now: datetime) -> tuple[Row, str]:
    """Register a unit. Returns the row and its key; the key is not stored and cannot be shown again."""
    for _ in range(5):
        unit_id = security.new_unit_id()
        if conn.execute(select(units.c.id).where(units.c.id == unit_id)).first() is None:
            break
    else:  # pragma: no cover - 50 bits of randomness, five collisions in a row is not happening
        raise RuntimeError("could not allocate a unit ID")
    key = security.unit_key_for(settings.secret_key, unit_id, 1)
    config = data.config.model_dump(exclude_none=True) if data.config else {}
    conn.execute(insert(units).values(
        id=unit_id, name=data.name, notes=data.notes, mac=data.mac, config=config, key_version=1,
        key_hash=security.hash_unit_key(settings.secret_key, key), created_at=now, online=False))
    add_event(conn, time=now, unit_id=unit_id, kind="unit_created", message=f"Unit registered for {data.name}")
    return get_unit(conn, unit_id), key


def get_unit(conn: Connection, unit_id: str) -> Row | None:
    return conn.execute(select(units).where(units.c.id == unit_id)).first()


def list_units(conn: Connection) -> list[Row]:
    return list(conn.execute(select(units).order_by(units.c.name, units.c.id)))


def update_unit(conn: Connection, unit_id: str, data: UnitUpdate, now: datetime) -> Row | None:
    unit = get_unit(conn, unit_id)
    if unit is None:
        return None
    values: dict[str, Any] = {}
    fields = data.model_fields_set
    if "name" in fields and data.name is not None:
        values["name"] = data.name
    if "notes" in fields:
        values["notes"] = data.notes
    if "mac" in fields:
        values["mac"] = data.mac
    if "config" in fields:
        values["config"] = data.config.model_dump(exclude_none=True) if data.config else {}
    if values:
        conn.execute(update(units).where(units.c.id == unit_id).values(**values))
        if "name" in values and values["name"] != unit.name:
            add_event(conn, time=now, unit_id=unit_id, kind="unit_renamed",
                      message=f"Unit renamed from {unit.name} to {values['name']}")
    return get_unit(conn, unit_id)


def rotate_key(conn: Connection, settings: Settings, unit_id: str, now: datetime) -> str | None:
    """Issue a new key (the old one stops working immediately). Also un-revokes the unit."""
    unit = get_unit(conn, unit_id)
    if unit is None:
        return None
    version = unit.key_version + 1
    key = security.unit_key_for(settings.secret_key, unit_id, version)
    conn.execute(update(units).where(units.c.id == unit_id).values(
        key_version=version, key_hash=security.hash_unit_key(settings.secret_key, key), revoked_at=None))
    add_event(conn, time=now, unit_id=unit_id, kind="key_rotated", level="warning",
              message="Unit key replaced; the unit must be reprogrammed")
    return key


def revoke_unit(conn: Connection, unit_id: str, now: datetime) -> Row | None:
    unit = get_unit(conn, unit_id)
    if unit is None:
        return None
    if unit.revoked_at is None:
        conn.execute(update(units).where(units.c.id == unit_id).values(revoked_at=now, online=False))
        add_event(conn, time=now, unit_id=unit_id, kind="unit_revoked", level="warning",
                  message="Unit key revoked; the unit can no longer send data")
    return get_unit(conn, unit_id)


def delete_unit(conn: Connection, unit_id: str, now: datetime) -> str | None:
    """Remove a unit and everything it measured (#14). Returns its name, or None if there was no such unit."""
    from .tables import link_samples, scan_observations, scans

    unit = get_unit(conn, unit_id)
    if unit is None:
        return None
    for table in (link_samples, scans, scan_observations):  # hypertables: no foreign keys to cascade
        conn.execute(table.delete().where(table.c.unit_id == unit_id))
    conn.execute(units.delete().where(units.c.id == unit_id))  # events, batches and placement cascade
    add_event(conn, time=now, unit_id=None, kind="unit_removed", level="warning",
              message=f"Unit removed: {unit.name}" + (f" (board {unit.mac})" if unit.mac else ""),
              detail={"unit_id": unit_id})
    return unit.name


def authenticate(conn: Connection, settings: Settings, authorization: str | None) -> Row | None:
    """Return the unit for a `Bearer <unit key>` header, or None if missing, wrong or revoked."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    key = authorization[len("Bearer "):].strip()
    parts = security.split_unit_key(key)
    if parts is None:
        return None
    unit = get_unit(conn, parts[0])
    if unit is None or unit.revoked_at is not None:
        return None
    if not security.unit_key_matches(settings.secret_key, key, unit.key_hash):
        return None
    return unit


class SignedRequestError(Exception):
    pass


# Accepted difference between a signed request's timestamp and server time.
SIGNATURE_WINDOW_S = 120


def authenticate_signed(conn: Connection, settings: Settings, nonces: security.NonceCache, *, method: str,
                        path: str, headers: Any, body: bytes, now: datetime) -> tuple[Row, bytes, str]:
    """Verify a signed unit request (#9). Returns (unit, signing key, nonce) or raises SignedRequestError."""
    unit_id = headers.get("x-unit-id") or ""
    timestamp = headers.get("x-timestamp") or ""
    nonce = headers.get("x-nonce") or ""
    signature = headers.get("x-signature") or ""
    if not security.UNIT_ID_RE.match(unit_id) or not timestamp.isdigit() or not security.NONCE_RE.match(nonce):
        raise SignedRequestError("malformed signature headers")
    if abs(now.timestamp() - int(timestamp)) > SIGNATURE_WINDOW_S:
        raise SignedRequestError("timestamp outside the accepted window; sync the clock with /api/unit/v1/time")
    unit = get_unit(conn, unit_id)
    if unit is None or unit.revoked_at is not None:
        raise SignedRequestError("unknown or revoked unit")
    key = security.signing_key(settings.secret_key, unit.id, unit.key_version)
    expected = security.request_signature(key, method, path, timestamp, nonce, body)
    if not hmac.compare_digest(expected, signature.lower()):
        raise SignedRequestError("bad signature")
    if not nonces.check_and_store(unit.id, nonce, now.timestamp()):
        raise SignedRequestError("nonce already used")
    return unit, key, nonce


def mark_seen(conn: Connection, unit: Row, now: datetime, client_ip: str | None, **fields: Any) -> None:
    """Record contact from a unit and log the transition to online."""
    values = {"last_seen_at": now, "online": True, **fields}
    if client_ip:
        values["last_ip"] = client_ip
    conn.execute(update(units).where(units.c.id == unit.id).values(**values))
    if not unit.online:
        if unit.last_seen_at is None:
            message = "Unit came online for the first time"
        else:
            message = f"Unit back online after {format_gap(now - unit.last_seen_at)}"
        add_event(conn, time=now, unit_id=unit.id, kind="unit_online", level="success", message=message)


def mark_offline_units(conn: Connection, settings: Settings, now: datetime) -> list[str]:
    """Flag units that have been silent longer than the offline threshold."""
    cutoff = now - timedelta(seconds=settings.offline_after_s)
    rows = conn.execute(
        update(units)
        .where(and_(units.c.online.is_(True), units.c.last_seen_at < cutoff))
        .values(online=False)
        .returning(units.c.id, units.c.last_seen_at)).all()
    for unit_id, last_seen in rows:
        add_event(conn, time=now, unit_id=unit_id, kind="unit_offline", level="warning",
                  message=f"Unit offline: nothing received for {format_gap(now - last_seen)}")
    return [r[0] for r in rows]


def format_gap(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} days"
