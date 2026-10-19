"""Synthetic rooms for development, demos and the dashboard benchmark (#5). Never for production."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import Connection, text, update

from .config import Settings
from .schemas import UnitCreate
from .tables import units
from .units import create_unit

# name prefix, base RSSI, noise, Wi-Fi drop rate per 5-minute block, packet loss, latency scale (ms)
PROFILES = [
    (-44, 1.5, 0.000, 0.002, 2.0),   # next to the access point
    (-52, 2.5, 0.002, 0.005, 3.0),
    (-61, 3.5, 0.004, 0.010, 5.0),
    (-68, 4.5, 0.010, 0.020, 8.0),
    (-74, 5.5, 0.025, 0.040, 14.0),  # far corridor end
    (-81, 6.0, 0.060, 0.080, 25.0),  # basement
]


class DemoRefused(RuntimeError):
    pass


SAMPLES_SQL = """
INSERT INTO link_samples (time, unit_id, connected, rssi, bssid, channel, gw_ms, gw_lost, srv_ms, srv_lost)
SELECT t, :uid, c.connected,
       CASE WHEN c.connected THEN greatest(-95, least(-25, round(:base + 4 * sin(c.phase)
            + (random() + random() + random() - 1.5) * :noise * 2))) END,
       CASE WHEN c.connected THEN :bssid END,
       CASE WHEN c.connected THEN :channel END,
       CASE WHEN c.connected AND c.gw_ok THEN round((1 + :lat * -ln(random()))::numeric, 2) END,
       CASE WHEN c.connected THEN NOT c.gw_ok END,
       CASE WHEN c.connected AND c.srv AND c.srv_ok THEN round((40 + 30 * -ln(random()))::numeric, 1) END,
       CASE WHEN c.connected AND c.srv THEN NOT c.srv_ok END
FROM generate_series(CAST(:start AS timestamptz), CAST(:end AS timestamptz), interval '5 seconds') AS t,
LATERAL (SELECT
    (abs(hashtext(:uid || floor(extract(epoch FROM t) / 300)::text)) % 100000) >= :drop * 100000 AS connected,
    extract(epoch FROM t) / 86400 * 2 * pi() AS phase,
    floor(extract(epoch FROM t))::bigint % 30 = 0 AS srv,
    random() >= :loss AS gw_ok,
    random() >= :loss AS srv_ok) AS c
"""

EVENTS_SQL = """
INSERT INTO events (time, unit_id, source, level, kind, message, detail)
SELECT time, :uid, 'unit',
       CASE WHEN connected THEN 'success' ELSE 'warning' END,
       CASE WHEN connected THEN 'wifi_connected' ELSE 'wifi_lost' END,
       CASE WHEN connected THEN 'Connected to ' || :ssid || ' via ' || :bssid
            ELSE 'Wi-Fi connection lost: beacon timeout' END,
       NULL
FROM (SELECT time, connected, lag(connected) OVER (ORDER BY time) AS before
      FROM link_samples WHERE unit_id = :uid) s
WHERE before IS NOT NULL AND before <> connected
"""


def seed_demo(conn: Connection, settings: Settings, *, count: int, days: float, now: datetime,
              seed: int = 7) -> list[str]:
    if settings.is_production:
        raise DemoRefused("seed-demo writes synthetic data and is refused when ENVIRONMENT=production")
    rng = random.Random(seed)
    # Whole 5-second steps, so "every 30 s" (server pings) lines up with the sample times.
    now = now.replace(microsecond=0) - timedelta(seconds=now.second % 5)
    start = now - timedelta(days=days)
    created = []
    for i in range(count):
        base, noise, drop, loss, lat = PROFILES[i % len(PROFILES)]
        name = f"Demo {chr(65 + (i // 10) % 26)}-{101 + i % 10 + 100 * (i // 60)}"
        unit, _ = create_unit(conn, settings, UnitCreate(name=name, notes="Synthetic demo data"), now)
        bssid = "02:00:5e:%02x:%02x:%02x" % (i // 256, i % 256, rng.randrange(256))
        channel = rng.choice([1, 6, 11, 36, 44, 52, 100, 149])
        params = {"uid": unit.id, "base": base, "noise": noise, "drop": drop, "loss": loss, "lat": lat,
                  "bssid": bssid, "channel": channel, "start": start, "end": now, "ssid": "SchoolNet"}
        conn.execute(text(SAMPLES_SQL), params)
        conn.execute(text(EVENTS_SQL), params)
        scan_time = now - timedelta(minutes=rng.randrange(1, 10))
        aps = [("SchoolNet", bssid, channel, base + 2)] + [
            (rng.choice(["SchoolNet", "SchoolNet-Guest", "Printer-Direct", ""]),
             "02:00:5e:%02x:%02x:%02x" % (200 + i % 50, j, rng.randrange(256)),
             rng.choice([1, 6, 11, 36, 44, 149]), rng.randrange(-92, -50)) for j in range(rng.randrange(8, 20))]
        conn.execute(text("INSERT INTO scans (time, unit_id, duration_ms, ap_count) VALUES (:t, :u, 9700, :n)"),
                     {"t": scan_time, "u": unit.id, "n": len(aps)})
        conn.execute(text("""INSERT INTO scan_observations (time, unit_id, bssid, ssid, channel, rssi, auth, hidden)
                             VALUES (:t, :u, :b, :s, :c, :r, 3, :h)"""),
                     [{"t": scan_time, "u": unit.id, "b": b, "s": s, "c": c, "r": r, "h": s == ""} for s, b, c, r in aps])
        conn.execute(update(units).where(units.c.id == unit.id).values(
            online=drop < 0.05, last_seen_at=now if drop < 0.05 else now - timedelta(hours=1), agent_version="demo",
            last_rssi=base, last_bssid=bssid, last_channel=channel, last_ssid="SchoolNet"))
        created.append(unit.id)
    return created
