"""Stability statistics for scanned networks and the connection link."""

from __future__ import annotations

import math
from statistics import fmean, median, pstdev
from typing import Any, Iterable

from .protocol import auth_name, band_for_channel

# RSSI standard deviation (dB) at which a network counts as completely unsteady.
UNSTEADY_STD_DB = 8.0


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile (pct in 0..100)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * pct / 100
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[low])
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def mean_abs_delta(values: list[float]) -> float | None:
    """Mean absolute difference between consecutive values (a simple jitter measure)."""
    if len(values) < 2:
        return None
    return fmean(abs(b - a) for a, b in zip(values, values[1:]))


def signal_quality(rssi: float | None) -> str:
    if rssi is None:
        return "none"
    if rssi >= -50:
        return "excellent"
    if rssi >= -60:
        return "good"
    if rssi >= -70:
        return "fair"
    if rssi >= -80:
        return "weak"
    return "poor"


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _gaps(seen: list[int]) -> list[int]:
    """Lengths of runs of missed scan indexes between the first and last sighting."""
    return [b - a - 1 for a, b in zip(seen, seen[1:]) if b - a > 1]


def network_stats(scans: list[dict[str, Any]], observations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-BSSID statistics over a session.

    `scans` are the session's scans (id, ts) in time order; `observations` are rows
    with scan_id, ts, bssid, ssid, channel, rssi, auth and hidden.
    """
    index = {scan["id"]: i for i, scan in enumerate(scans)}
    total = len(scans)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        if obs["scan_id"] in index:
            grouped.setdefault(obs["bssid"], []).append(obs)

    results = []
    for bssid, rows in grouped.items():
        rows.sort(key=lambda r: index[r["scan_id"]])
        seen = sorted({index[r["scan_id"]] for r in rows})
        rssis = [r["rssi"] for r in rows]
        last = rows[-1]
        gaps = _gaps(seen)
        std = pstdev(rssis) if len(rssis) > 1 else 0.0
        presence = len(seen) / total if total else 0.0
        steadiness = max(0.0, 1.0 - std / UNSTEADY_STD_DB)
        visible_now = seen[-1] == total - 1
        mean_rssi = fmean(rssis)
        results.append({
            "bssid": bssid,
            "ssid": last["ssid"],
            "hidden": bool(last["hidden"]),
            "channel": last["channel"],
            "band": band_for_channel(last["channel"]),
            "auth": last["auth"],
            "security": auth_name(last["auth"]),
            "samples": len(rssis),
            "seen_pct": _round(presence * 100),
            "visible_now": visible_now,
            "current_rssi": last["rssi"] if visible_now else None,
            "last_seen": last["ts"],
            "first_seen": rows[0]["ts"],
            "mean_rssi": _round(mean_rssi),
            "median_rssi": _round(median(rssis)),
            "min_rssi": min(rssis),
            "max_rssi": max(rssis),
            "std_rssi": _round(std, 2),
            "jitter_db": _round(mean_abs_delta(rssis), 2),
            "dropouts": len(gaps),
            "longest_gap_scans": max(gaps, default=0),
            "channel_changes": sum(1 for a, b in zip(rows, rows[1:]) if a["channel"] != b["channel"]),
            "quality": signal_quality(mean_rssi),
            "stability": round(100 * (0.6 * presence + 0.4 * steadiness)),
        })
    results.sort(key=lambda r: (-(r["mean_rssi"] or -999), r["ssid"], r["bssid"]))
    return results


def rssi_series(scans: list[dict[str, Any]], observations: Iterable[dict[str, Any]],
                bssids: list[str]) -> dict[str, Any]:
    """RSSI per scan for the given BSSIDs, with None where a network was not seen."""
    index = {scan["id"]: i for i, scan in enumerate(scans)}
    series: dict[str, list[int | None]] = {b: [None] * len(scans) for b in bssids}
    for obs in observations:
        i = index.get(obs["scan_id"])
        if i is not None and obs["bssid"] in series:
            series[obs["bssid"]][i] = obs["rssi"]
    return {"ts": [scan["ts"] for scan in scans], "series": series}


def link_stats(samples: list[dict[str, Any]], disconnects: int = 0) -> dict[str, Any]:
    """Connection quality over a session's link samples (ts, connected, rssi, latency_ms, ok)."""
    total = len(samples)
    connected = [s for s in samples if s["connected"]]
    latencies = [s["latency_ms"] for s in connected if s["ok"] and s["latency_ms"] is not None]
    rssis = [s["rssi"] for s in connected if s["rssi"] is not None]
    attempts = len(connected)

    longest_outage = 0.0
    outage_start = None
    for s in samples:
        if not s["connected"]:
            if outage_start is None:
                outage_start = s["ts"]
        elif outage_start is not None:
            longest_outage = max(longest_outage, s["ts"] - outage_start)
            outage_start = None
    if outage_start is not None and samples:
        longest_outage = max(longest_outage, samples[-1]["ts"] - outage_start)

    last = samples[-1] if samples else None
    return {
        "samples": total,
        "duration_s": _round(samples[-1]["ts"] - samples[0]["ts"]) if total > 1 else 0.0,
        "uptime_pct": _round(100 * len(connected) / total) if total else None,
        "pings": attempts,
        "pings_ok": len(latencies),
        "loss_pct": _round(100 * (attempts - len(latencies)) / attempts) if attempts else None,
        "latency_mean": _round(fmean(latencies), 2) if latencies else None,
        "latency_median": _round(median(latencies), 2) if latencies else None,
        "latency_p95": _round(percentile(latencies, 95), 2),
        "latency_min": _round(min(latencies), 2) if latencies else None,
        "latency_max": _round(max(latencies), 2) if latencies else None,
        "latency_jitter": _round(mean_abs_delta(latencies), 2),
        "rssi_current": last["rssi"] if last and last["connected"] else None,
        "rssi_mean": _round(fmean(rssis)) if rssis else None,
        "rssi_min": min(rssis) if rssis else None,
        "rssi_max": max(rssis) if rssis else None,
        "rssi_std": _round(pstdev(rssis), 2) if len(rssis) > 1 else None,
        "connected_now": bool(last and last["connected"]),
        "disconnects": disconnects,
        "longest_outage_s": _round(longest_outage),
        "quality": signal_quality(fmean(rssis) if rssis else None),
    }
