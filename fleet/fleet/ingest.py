"""Unit handshake and measurement batches."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Row, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .agentbundle import AgentBundle
from .config import Settings
from .db import add_event, from_epoch
from .schemas import Batch, Hello
from .tables import ingest_batches, link_samples, scan_observations, scans
from .units import effective_config, mark_seen

# Unit clocks are set from the server at handshake; correct anything further off than this.
SKEW_TOLERANCE_S = 2.0
# Measurements outside this window (after skew correction) are dropped as implausible.
MAX_FUTURE = timedelta(minutes=2)
MAX_AGE = timedelta(days=30)


def server_ms(now: datetime) -> int:
    """Server time as integer milliseconds. Units use this: MicroPython floats are single precision."""
    return int(now.timestamp() * 1000)


def describe_unit_event(kind: str, detail: dict[str, Any]) -> tuple[str, str]:
    """(level, message) for an event reported by a unit."""
    d = detail
    if kind == "boot":
        error = f"; previous crash: {d['error']}" if d.get("error") else ""
        return "info", f"Unit booted (reset cause: {d.get('reset_cause', 'unknown')}){error}"
    if kind == "wifi_connected":
        parts = [f"Connected to {d.get('ssid', 'Wi-Fi')}"]
        if d.get("bssid"):
            parts.append(f"via {d['bssid']}")
        if d.get("channel"):
            parts.append(f"on channel {d['channel']}")
        if d.get("took_s") is not None:
            parts.append(f"after {d['took_s']} s")
        return "success", " ".join(parts)
    if kind == "wifi_lost":
        reason = f": {d['reason']}" if d.get("reason") else ""
        after = f" after {d['up_s']} s connected" if d.get("up_s") is not None else ""
        return "warning", f"Wi-Fi connection lost{reason}{after}"
    if kind == "wifi_connect_failed":
        return "warning", f"Could not connect to Wi-Fi: {d.get('reason', 'unknown reason')}"
    if kind == "auth_failed":
        return "error", "Server rejected the unit key; reprogram the unit"
    if kind == "roam":
        return "info", (f"Roamed from {d.get('from', '?')} to {d.get('to', '?')}"
                        + (f" (channel {d['channel']})" if d.get("channel") else ""))
    if kind == "server_unreachable":
        return "warning", f"Server unreachable: {d.get('error', 'no response')}"
    if kind == "buffer_overflow":
        return "warning", f"Buffer full, dropped {d.get('dropped', '?')} old samples"
    if kind == "bad_signature":
        return "error", f"Unit rejected a server response: {d.get('error', 'signature check failed')}"
    if kind == "update_started":
        return "info", f"Installing agent {d.get('version', '?')}"
    if kind == "update_skipped":
        return "warning", f"Skipped agent {d.get('version', '?')}: it failed on this unit before"
    if kind == "batch_rejected":
        return "error", f"Server rejected a batch: {d.get('error', '?')}"
    if kind == "rebooting":
        return "warning", f"Unit restarting: {d.get('reason', 'unknown reason')}"
    if kind == "update_ok":
        return "success", f"Agent updated to {d.get('version', '?')}"
    if kind == "update_failed":
        return "error", f"Agent update failed: {d.get('error', 'unknown error')}"
    if kind == "error":
        return "error", str(d.get("message", "unit error"))[:500]
    return "info", kind.replace("_", " ").capitalize()


def handle_hello(conn: Connection, settings: Settings, unit: Row, hello: Hello, client_ip: str | None,
                 now: datetime, agent: AgentBundle | None = None) -> dict[str, Any]:
    fields: dict[str, Any] = {"last_hello_at": now, "agent_version": hello.agent_version}
    for name in ("fw_version", "chip"):
        if getattr(hello, name) is not None:
            fields[name] = getattr(hello, name)
    if hello.mac and not unit.mac:
        fields["mac"] = hello.mac
    for src, dst in (("rssi", "last_rssi"), ("bssid", "last_bssid"), ("channel", "last_channel"),
                     ("ssid", "last_ssid")):
        if getattr(hello, src) is not None:
            fields[dst] = getattr(hello, src)
    if hello.mac and unit.mac and hello.mac != unit.mac:
        add_event(conn, time=now, unit_id=unit.id, kind="mac_mismatch", level="warning",
                  message=f"Unit key used by a different board ({hello.mac}, expected {unit.mac})",
                  detail={"mac": hello.mac})
    mark_seen(conn, unit, now, client_ip, **fields)
    reason = f", reset cause {hello.reset_cause}" if hello.reset_cause else ""
    add_event(conn, time=now, unit_id=unit.id, kind="hello",
              message=f"Handshake from agent {hello.agent_version}{reason}",
              detail=hello.model_dump(exclude_none=True))
    return {
        "unit_id": unit.id,
        "name": unit.name,
        "server_time": now.timestamp(),
        "server_time_ms": server_ms(now),
        "config": effective_config(settings, unit.config),
        "update": agent.offer() if agent and agent.version != hello.agent_version else None,
    }


def store_batch(conn: Connection, settings: Settings, unit: Row, batch: Batch, client_ip: str | None,
                now: datetime) -> dict[str, Any]:
    """Store one batch. Idempotent: a batch ID that was already stored is acknowledged, not stored again."""
    skew = now.timestamp() - batch.sent_at
    offset = skew if abs(skew) > SKEW_TOLERANCE_S else 0.0
    claimed = conn.execute(
        pg_insert(ingest_batches)
        .values(unit_id=unit.id, batch_id=batch.batch_id, received_at=now, clock_skew_s=round(skew, 3),
                samples=len(batch.samples), scans=len(batch.scans), events=len(batch.events))
        .on_conflict_do_nothing()
        .returning(ingest_batches.c.batch_id)).first()
    if claimed is None:
        mark_seen(conn, unit, now, client_ip)
        return {"duplicate": True, "server_time": now.timestamp(), "server_time_ms": server_ms(now), "stored": None}

    def when(t: float) -> datetime | None:
        moment = from_epoch(t + offset)
        return moment if now - MAX_AGE <= moment <= now + MAX_FUTURE else None

    dropped = 0
    sample_rows = []
    for s in batch.samples:
        t = when(s.t)
        if t is None:
            dropped += 1
            continue
        sample_rows.append({"time": t, "unit_id": unit.id, "connected": s.connected, "rssi": s.rssi,
                            "bssid": s.bssid, "channel": s.channel, "gw_ms": s.gw_ms, "gw_lost": s.gw_lost,
                            "srv_ms": s.srv_ms, "srv_lost": s.srv_lost})
    if sample_rows:
        conn.execute(insert(link_samples), sample_rows)

    scan_rows, obs_rows = [], []
    for scan in batch.scans:
        t = when(scan.t)
        if t is None:
            dropped += 1
            continue
        scan_rows.append({"time": t, "unit_id": unit.id, "duration_ms": scan.duration_ms, "ap_count": len(scan.aps)})
        obs_rows.extend({"time": t, "unit_id": unit.id, "bssid": ap.bssid, "ssid": ap.ssid, "channel": ap.channel,
                         "rssi": ap.rssi, "auth": ap.auth, "hidden": ap.hidden} for ap in scan.aps)
    if scan_rows:
        conn.execute(insert(scans), scan_rows)
    if obs_rows:
        conn.execute(insert(scan_observations), obs_rows)

    stored_events = 0
    for event in batch.events:
        t = when(event.t)
        if t is None:
            dropped += 1
            continue
        level, message = describe_unit_event(event.kind, event.detail or {})
        add_event(conn, time=t, unit_id=unit.id, source="unit", kind=event.kind, level=level,
                  message=message, detail=event.detail)
        stored_events += 1

    if abs(skew) > 60:
        add_event(conn, time=now, unit_id=unit.id, kind="clock_skew", level="warning",
                  message=f"Unit clock is off by {skew:+.0f} s; timestamps were corrected",
                  detail={"skew_s": round(skew, 1)})

    latest = max((s for s in batch.samples if s.connected and s.rssi is not None), key=lambda s: s.t, default=None)
    fields = {}
    if latest is not None:
        fields = {"last_rssi": latest.rssi, "last_bssid": latest.bssid, "last_channel": latest.channel}
    mark_seen(conn, unit, now, client_ip, **fields)

    return {
        "duplicate": False,
        "server_time": now.timestamp(),
        "server_time_ms": server_ms(now),
        "clock_skew_s": round(skew, 3),
        "stored": {"samples": len(sample_rows), "scans": len(scan_rows), "observations": len(obs_rows),
                   "events": stored_events, "dropped": dropped},
    }
