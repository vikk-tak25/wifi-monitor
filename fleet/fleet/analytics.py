"""Dashboard queries: candles, room comparison and room summaries.

Everything is computed exactly from raw samples. TimescaleDB continuous aggregates cannot hold
medians without the Toolkit extension, which the deployed image does not have (#5).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text

# metric -> (value column, loss column or None). Only these names ever reach SQL.
METRICS = {
    "rssi": ("rssi", None),
    "gw_ms": ("gw_ms", "gw_lost"),
    "srv_ms": ("srv_ms", "srv_lost"),
}
BUCKETS = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15), "1h": timedelta(hours=1),
           "1d": timedelta(days=1)}
DEFAULT_RANGE = {"5m": timedelta(hours=6), "15m": timedelta(hours=24), "1h": timedelta(days=7),
                 "1d": timedelta(days=90)}
WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}
MAX_CANDLES = 2000


class QueryError(ValueError):
    pass


def _f(value: Any, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def signal_quality(rssi: float | None) -> str | None:
    if rssi is None:
        return None
    if rssi >= -50:
        return "excellent"
    if rssi >= -60:
        return "good"
    if rssi >= -70:
        return "fair"
    if rssi >= -80:
        return "weak"
    return "poor"


def stability_score(uptime: float | None, loss_pct: float | None, rssi_std: float | None) -> int | None:
    """0 to 100: half uptime, 30% gateway packet loss (10% loss or more scores 0), 20% signal
    steadiness (a standard deviation of 8 dB or more scores 0)."""
    if uptime is None:
        return None
    loss_part = 1 - min(loss_pct or 0.0, 10.0) / 10.0
    steady_part = max(0.0, 1 - (rssi_std or 0.0) / 8.0)
    return round(100 * (0.5 * uptime + 0.3 * loss_part + 0.2 * steady_part))


def candles(conn: Connection, unit_id: str, metric: str, bucket: str, start: datetime, end: datetime,
            timezone: str) -> list[dict[str, Any]]:
    if metric not in METRICS:
        raise QueryError(f"metric must be one of: {', '.join(METRICS)}")
    if bucket not in BUCKETS:
        raise QueryError(f"bucket must be one of: {', '.join(BUCKETS)}")
    if end <= start:
        raise QueryError("'to' must be after 'from'")
    if (end - start) / BUCKETS[bucket] > MAX_CANDLES:
        raise QueryError(f"range too long for {bucket} candles (at most {MAX_CANDLES})")
    column, loss = METRICS[metric]
    valid = f"{column} IS NOT NULL"
    loss_cols = (f", count(*) FILTER (WHERE {loss}) AS lost, count(*) FILTER (WHERE {loss} IS NOT NULL) AS tried"
                 if loss else ", 0 AS lost, 0 AS tried")
    scope = f"({loss} IS NOT NULL)" if loss else f"(connected AND {valid})"
    sql = text(f"""
        SELECT time_bucket(CAST(:bucket AS interval), time, CAST(:tz AS text)) AS t,
               first({column}, time) FILTER (WHERE {valid}) AS open,
               max({column}) AS high,
               min({column}) AS low,
               last({column}, time) FILTER (WHERE {valid}) AS close,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY {column}) AS median,
               count({column}) AS n
               {loss_cols}
        FROM link_samples
        WHERE unit_id = :unit AND time >= :start AND time < :end AND {scope}
        GROUP BY t ORDER BY t""")
    rows = conn.execute(sql, {"bucket": BUCKETS[bucket], "tz": timezone, "unit": unit_id,
                              "start": start, "end": end}).mappings().all()
    digits = 0 if metric == "rssi" else 2
    out = []
    for r in rows:
        item = {"t": r["t"].timestamp(), "n": r["n"], "open": _f(r["open"], digits), "high": _f(r["high"], digits),
                "low": _f(r["low"], digits), "close": _f(r["close"], digits), "median": _f(r["median"], 2)}
        if loss:
            item["loss_pct"] = round(100 * r["lost"] / r["tried"], 2) if r["tried"] else None
        out.append(item)
    return out


STATS_SQL = """
    SELECT unit_id,
           count(*) AS samples,
           avg(CASE WHEN connected THEN 1.0 ELSE 0.0 END) AS uptime,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi) FILTER (WHERE connected) AS rssi_median,
           avg(rssi) FILTER (WHERE connected) AS rssi_mean,
           stddev_pop(rssi) FILTER (WHERE connected) AS rssi_std,
           min(rssi) FILTER (WHERE connected) AS rssi_min,
           max(rssi) FILTER (WHERE connected) AS rssi_max,
           count(*) FILTER (WHERE gw_lost IS NOT NULL) AS gw_pings,
           count(*) FILTER (WHERE gw_lost) AS gw_lost,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY gw_ms) AS gw_median,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY gw_ms) AS gw_p95,
           count(*) FILTER (WHERE srv_lost IS NOT NULL) AS srv_pings,
           count(*) FILTER (WHERE srv_lost) AS srv_lost,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY srv_ms) AS srv_median,
           max(time) AS last_sample
    FROM link_samples
    WHERE time >= :start AND time < :end {unit_filter}
    GROUP BY unit_id
"""

EVENT_COUNTS_SQL = """
    SELECT unit_id,
           count(*) FILTER (WHERE kind = 'wifi_lost') AS wifi_drops,
           count(*) FILTER (WHERE kind = 'roam') AS roams,
           count(*) FILTER (WHERE kind = 'boot') AS boots
    FROM events
    WHERE time >= :start AND time < :end AND unit_id IS NOT NULL {unit_filter}
    GROUP BY unit_id
"""


def _stats(conn: Connection, start: datetime, end: datetime, unit_id: str | None) -> dict[str, dict]:
    params: dict[str, Any] = {"start": start, "end": end}
    unit_filter = ""
    if unit_id:
        unit_filter = "AND unit_id = :unit"
        params["unit"] = unit_id
    stats = {r["unit_id"]: dict(r) for r in conn.execute(text(STATS_SQL.format(unit_filter=unit_filter)),
                                                          params).mappings()}
    counts = {r["unit_id"]: dict(r) for r in conn.execute(text(EVENT_COUNTS_SQL.format(unit_filter=unit_filter)),
                                                           params).mappings()}
    out = {}
    for uid in set(stats) | set(counts):
        s, c = stats.get(uid, {}), counts.get(uid, {})
        loss = round(100 * s["gw_lost"] / s["gw_pings"], 2) if s.get("gw_pings") else None
        srv_loss = round(100 * s["srv_lost"] / s["srv_pings"], 2) if s.get("srv_pings") else None
        uptime = float(s["uptime"]) if s.get("uptime") is not None else None
        rssi_std = _f(s.get("rssi_std"), 2)
        out[uid] = {
            "samples": s.get("samples", 0),
            "uptime_pct": _f(uptime * 100 if uptime is not None else None, 2),
            "rssi_median": _f(s.get("rssi_median")),
            "rssi_mean": _f(s.get("rssi_mean")),
            "rssi_std": rssi_std,
            "rssi_min": s.get("rssi_min"),
            "rssi_max": s.get("rssi_max"),
            "gw_pings": s.get("gw_pings", 0),
            "gw_loss_pct": loss,
            "gw_median_ms": _f(s.get("gw_median"), 2),
            "gw_p95_ms": _f(s.get("gw_p95"), 2),
            "srv_loss_pct": srv_loss,
            "srv_median_ms": _f(s.get("srv_median")),
            "wifi_drops": c.get("wifi_drops", 0),
            "roams": c.get("roams", 0),
            "boots": c.get("boots", 0),
            "quality": signal_quality(s.get("rssi_median")),
            "stability": stability_score(uptime, loss, rssi_std),
            "last_sample": s["last_sample"].timestamp() if s.get("last_sample") else None,
        }
    return out


def room_stats(conn: Connection, window: str, now: datetime) -> dict[str, Any]:
    """Statistics for every unit over a window. The expensive part of the comparison, so it is cached."""
    if window not in WINDOWS:
        raise QueryError(f"window must be one of: {', '.join(WINDOWS)}")
    start = now - WINDOWS[window]
    return {"window": window, "from": start, "to": now, "stats": _stats(conn, start, now, None)}


def rooms(conn: Connection, units: list[dict[str, Any]], window: str, now: datetime,
          computed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Room comparison: live unit status combined with (possibly cached) statistics."""
    computed = computed or room_stats(conn, window, now)
    start, end, stats = computed["from"], computed["to"], computed["stats"]
    rows = []
    for unit in units:
        rows.append({"unit_id": unit["id"], "name": unit["name"], "status": unit["status"],
                     "last_seen_at": unit["last_seen_at"], "last_bssid": unit["last_bssid"],
                     "last_channel": unit["last_channel"], "last_rssi": unit["last_rssi"],
                     **(stats.get(unit["id"]) or _stats_empty())})
    return {"window": window, "from": start.timestamp(), "to": end.timestamp(), "rooms": rows}


def _stats_empty() -> dict[str, Any]:
    return {"samples": 0, "uptime_pct": None, "rssi_median": None, "rssi_mean": None, "rssi_std": None,
            "rssi_min": None, "rssi_max": None, "gw_pings": 0, "gw_loss_pct": None, "gw_median_ms": None,
            "gw_p95_ms": None, "srv_loss_pct": None, "srv_median_ms": None, "wifi_drops": 0, "roams": 0,
            "boots": 0, "quality": None, "stability": None, "last_sample": None}


def unit_summary(conn: Connection, unit_id: str, window: str, now: datetime) -> dict[str, Any]:
    if window not in WINDOWS:
        raise QueryError(f"window must be one of: {', '.join(WINDOWS)}")
    start = now - WINDOWS[window]
    stats = _stats(conn, start, now, unit_id).get(unit_id) or _stats_empty()
    aps = conn.execute(text("""
        SELECT bssid, channel, count(*) AS samples, avg(rssi) AS rssi_mean, max(time) AS last_used
        FROM link_samples
        WHERE unit_id = :unit AND time >= :start AND time < :end AND connected
        GROUP BY bssid, channel ORDER BY samples DESC"""), {"unit": unit_id, "start": start, "end": now}).mappings()
    scan = conn.execute(text("""
        SELECT time, duration_ms, ap_count FROM scans WHERE unit_id = :unit ORDER BY time DESC LIMIT 1"""),
                        {"unit": unit_id}).mappings().first()
    networks = []
    if scan:
        networks = [dict(r) for r in conn.execute(text("""
            SELECT ssid, bssid, channel, rssi, auth, hidden FROM scan_observations
            WHERE unit_id = :unit AND time = :time ORDER BY rssi DESC LIMIT 100"""),
            {"unit": unit_id, "time": scan["time"]}).mappings()]
    return {
        "window": window, "from": start.timestamp(), "to": now.timestamp(), "stats": stats,
        "access_points": [{"bssid": a["bssid"], "channel": a["channel"], "samples": a["samples"],
                           "rssi_mean": _f(a["rssi_mean"]), "last_used": a["last_used"].timestamp()} for a in aps],
        "last_scan": ({"time": scan["time"].timestamp(), "duration_ms": scan["duration_ms"],
                       "ap_count": scan["ap_count"], "networks": networks} if scan else None),
    }
