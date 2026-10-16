"""SQLite storage for sessions, scans, link samples and events."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .protocol import Link, Scan

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    ssid TEXT,
    ping_target TEXT,
    scan_interval REAL,
    link_interval REAL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    duration_ms INTEGER,
    ap_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS scans_session ON scans(session_id, ts);
CREATE TABLE IF NOT EXISTS observations (
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    bssid TEXT NOT NULL,
    ssid TEXT NOT NULL,
    channel INTEGER NOT NULL,
    rssi INTEGER NOT NULL,
    auth INTEGER NOT NULL,
    hidden INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_session ON observations(session_id, bssid, ts);
CREATE TABLE IF NOT EXISTS link_samples (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    ts REAL NOT NULL,
    connected INTEGER NOT NULL,
    rssi INTEGER,
    latency_ms REAL,
    ok INTEGER NOT NULL,
    target TEXT
);
CREATE INDEX IF NOT EXISTS link_session ON link_samples(session_id, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id),
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, ts);
"""

SESSION_FIELDS = ("id", "mode", "started_at", "ended_at", "ssid", "ping_target",
                  "scan_interval", "link_interval", "note")


class Storage:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # -- sessions ---------------------------------------------------------

    def create_session(self, *, mode: str, started_at: float, ssid: str | None = None,
                       ping_target: str | None = None, scan_interval: float | None = None,
                       link_interval: float | None = None, note: str | None = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO sessions (mode, started_at, ssid, ping_target, scan_interval, link_interval, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mode, started_at, ssid, ping_target, scan_interval, link_interval, note))
            self._db.commit()
            return int(cur.lastrowid)

    def end_session(self, session_id: int, ended_at: float) -> None:
        with self._lock:
            self._db.execute("UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                             (ended_at, session_id))
            self._db.commit()

    def close_open_sessions(self, ended_at: float) -> int:
        """Mark sessions left open by a previous run (crash, Ctrl-C) as ended."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE sessions SET ended_at = COALESCE("
                " (SELECT MAX(ts) FROM ("
                "   SELECT ts FROM scans WHERE session_id = sessions.id"
                "   UNION ALL SELECT ts FROM link_samples WHERE session_id = sessions.id)),"
                " started_at) WHERE ended_at IS NULL")
            self._db.commit()
            return cur.rowcount

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        rows = self._query(f"SELECT {', '.join(SESSION_FIELDS)} FROM sessions WHERE id = ?", (session_id,))
        return dict(rows[0]) if rows else None

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self._query(
            f"SELECT {', '.join('s.' + f for f in SESSION_FIELDS)},"
            " (SELECT COUNT(*) FROM scans WHERE session_id = s.id) AS scan_count,"
            " (SELECT COUNT(*) FROM link_samples WHERE session_id = s.id) AS link_count"
            " FROM sessions s ORDER BY s.started_at DESC")
        return [dict(r) for r in rows]

    def delete_session(self, session_id: int) -> bool:
        with self._lock:
            for table in ("observations", "scans", "link_samples", "events"):
                self._db.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            cur = self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._db.commit()
            return cur.rowcount > 0

    # -- measurements -----------------------------------------------------

    def add_scan(self, session_id: int, ts: float, scan: Scan) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO scans (session_id, ts, duration_ms, ap_count) VALUES (?, ?, ?, ?)",
                (session_id, ts, scan.duration_ms, len(scan.aps)))
            scan_id = int(cur.lastrowid)
            self._db.executemany(
                "INSERT INTO observations (scan_id, session_id, ts, bssid, ssid, channel, rssi, auth, hidden)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(scan_id, session_id, ts, ap.bssid, ap.ssid, ap.channel, ap.rssi, ap.auth, int(ap.hidden))
                 for ap in scan.aps])
            self._db.commit()
            return scan_id

    def add_link(self, session_id: int, ts: float, link: Link) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO link_samples (session_id, ts, connected, rssi, latency_ms, ok, target)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, ts, int(link.connected), link.rssi, link.latency_ms, int(link.ok), link.target))
            self._db.commit()
            return int(cur.lastrowid)

    def add_event(self, session_id: int | None, ts: float, kind: str, message: str,
                  level: str = "info", detail: dict[str, Any] | None = None) -> dict[str, Any]:
        detail_json = json.dumps(detail) if detail else None
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (session_id, ts, level, kind, message, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, ts, level, kind, message, detail_json))
            self._db.commit()
            event_id = int(cur.lastrowid)
        return {"id": event_id, "session_id": session_id, "ts": ts, "level": level,
                "kind": kind, "message": message, "detail": detail or None}

    # -- reads ------------------------------------------------------------

    def scans(self, session_id: int) -> list[dict[str, Any]]:
        rows = self._query("SELECT id, ts, duration_ms, ap_count FROM scans WHERE session_id = ? ORDER BY ts, id",
                           (session_id,))
        return [dict(r) for r in rows]

    def observations(self, session_id: int, bssids: list[str] | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT scan_id, ts, bssid, ssid, channel, rssi, auth, hidden FROM observations"
               " WHERE session_id = ?")
        params: list[Any] = [session_id]
        if bssids:
            sql += f" AND bssid IN ({', '.join('?' for _ in bssids)})"
            params.extend(bssids)
        rows = self._query(sql + " ORDER BY ts, scan_id", tuple(params))
        return [dict(r) for r in rows]

    def link_samples(self, session_id: int, since: float | None = None) -> list[dict[str, Any]]:
        sql = "SELECT ts, connected, rssi, latency_ms, ok, target FROM link_samples WHERE session_id = ?"
        params: list[Any] = [session_id]
        if since is not None:
            sql += " AND ts > ?"
            params.append(since)
        rows = self._query(sql + " ORDER BY ts, id", tuple(params))
        return [{**dict(r), "connected": bool(r["connected"]), "ok": bool(r["ok"])} for r in rows]

    def events(self, session_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if session_id is None:
            rows = self._query("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        else:
            rows = self._query("SELECT * FROM events WHERE session_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
                               (session_id, limit))
        out = []
        for r in rows:
            item = dict(r)
            item["detail"] = json.loads(item["detail"]) if item["detail"] else None
            out.append(item)
        return out

    def count_events(self, session_id: int, kind: str) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM events WHERE session_id = ? AND kind = ?", (session_id, kind))
        return int(rows[0]["n"])
