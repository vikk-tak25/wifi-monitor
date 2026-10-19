"""Dashboard data (#5): exact candles and medians, room comparison, API and page."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, text

from fleet import analytics
from fleet.app import create_app
from fleet.demo import DemoRefused, seed_demo
from fleet.tables import events, link_samples

from .conftest import WRITE_HEADERS

T0 = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)  # 09:00 in Tallinn


def add_samples(engine, unit_id, rows):
    """rows: (seconds after T0, connected, rssi, gw_ms or 'lost' or None, srv_ms or 'lost' or None)"""
    data = []
    for sec, connected, rssi, gw, srv in rows:
        data.append({"time": T0 + timedelta(seconds=sec), "unit_id": unit_id, "connected": connected, "rssi": rssi,
                     "bssid": "02:00:00:aa:00:40" if connected else None, "channel": 6 if connected else None,
                     "gw_ms": None if gw in (None, "lost") else gw, "gw_lost": None if gw is None else gw == "lost",
                     "srv_ms": None if srv in (None, "lost") else srv, "srv_lost": None if srv is None else srv == "lost"})
    with engine.begin() as conn:
        conn.execute(insert(link_samples), data)


@pytest.fixture
def room(make_unit, engine):
    unit, _ = make_unit("B-204")
    # Bucket 1 (06:00 to 06:05 UTC): rssi -50, -40, -60, -45 -> open -50, high -40, low -60, close -45, median -47.5
    # Bucket 2 (06:05 to 06:10): rssi -55 then disconnected, then -65
    add_samples(engine, unit["id"], [
        (0, True, -50, 2.0, 40.0), (60, True, -40, 4.0, None), (120, True, -60, "lost", None), (240, True, -45, 8.0, "lost"),
        (300, True, -55, 3.0, None), (360, False, None, None, None), (420, True, -65, 5.0, 60.0),
    ])
    with engine.begin() as conn:
        conn.execute(insert(events), [
            {"time": T0 + timedelta(seconds=350), "unit_id": unit["id"], "source": "unit", "level": "warning",
             "kind": "wifi_lost", "message": "lost", "detail": None},
            {"time": T0 + timedelta(seconds=400), "unit_id": unit["id"], "source": "unit", "level": "info",
             "kind": "roam", "message": "roam", "detail": None}])
    return unit


def test_rssi_candles_are_exact(engine, room):
    with engine.connect() as conn:
        rows = analytics.candles(conn, room["id"], "rssi", "5m", T0, T0 + timedelta(minutes=10), "Europe/Tallinn")
    assert rows == [
        {"t": T0.timestamp(), "n": 4, "open": -50, "high": -40, "low": -60, "close": -45, "median": -47.5},
        {"t": (T0 + timedelta(minutes=5)).timestamp(), "n": 2, "open": -55, "high": -55, "low": -65, "close": -65,
         "median": -60.0},
    ]


def test_latency_candles_skip_lost_pings_and_report_loss(engine, room):
    with engine.connect() as conn:
        gw = analytics.candles(conn, room["id"], "gw_ms", "5m", T0, T0 + timedelta(minutes=10), "Europe/Tallinn")
        srv = analytics.candles(conn, room["id"], "srv_ms", "5m", T0, T0 + timedelta(minutes=10), "Europe/Tallinn")
    assert gw[0] == {"t": T0.timestamp(), "n": 3, "open": 2.0, "high": 8.0, "low": 2.0, "close": 8.0, "median": 4.0,
                     "loss_pct": 25.0}
    assert gw[1]["n"] == 2 and gw[1]["loss_pct"] == 0.0
    assert srv[0]["n"] == 1 and srv[0]["loss_pct"] == 50.0 and srv[0]["median"] == 40.0


def test_daily_candles_follow_the_configured_time_zone(engine, room):
    with engine.connect() as conn:
        tallinn = analytics.candles(conn, room["id"], "rssi", "1d", T0 - timedelta(days=1), T0 + timedelta(days=1),
                                    "Europe/Tallinn")
        utc = analytics.candles(conn, room["id"], "rssi", "1d", T0 - timedelta(days=1), T0 + timedelta(days=1), "UTC")
    assert tallinn[0]["t"] == datetime(2026, 10, 15, 21, 0, tzinfo=timezone.utc).timestamp()  # midnight EEST
    assert utc[0]["t"] == datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc).timestamp()
    assert tallinn[0]["n"] == 6 and tallinn[0]["median"] == -52.5


@pytest.mark.parametrize("kwargs,message", [
    ({"metric": "temperature"}, "metric"),
    ({"bucket": "1w"}, "bucket"),
    ({"start": T0, "end": T0}, "after"),
    ({"bucket": "5m", "start": T0 - timedelta(days=8)}, "too long"),
])
def test_candle_validation(engine, room, kwargs, message):
    args = {"metric": "rssi", "bucket": "1h", "start": T0, "end": T0 + timedelta(hours=1), **kwargs}
    with engine.connect() as conn, pytest.raises(analytics.QueryError, match=message):
        analytics.candles(conn, room["id"], args["metric"], args["bucket"], args["start"], args["end"], "UTC")


def test_room_statistics(engine, room):
    with engine.connect() as conn:
        stats = analytics.unit_summary(conn, room["id"], "24h", T0 + timedelta(hours=1))["stats"]
    assert stats["samples"] == 7
    assert stats["uptime_pct"] == pytest.approx(85.71, abs=0.01)
    assert stats["rssi_median"] == -52.5 and stats["rssi_min"] == -65 and stats["rssi_max"] == -40
    assert stats["gw_pings"] == 6 and stats["gw_loss_pct"] == pytest.approx(16.67, abs=0.01)
    assert stats["gw_median_ms"] == 4.0
    assert stats["srv_loss_pct"] == pytest.approx(33.33, abs=0.01) and stats["srv_median_ms"] == 50.0
    assert stats["wifi_drops"] == 1 and stats["roams"] == 1
    assert stats["quality"] == "good"
    assert stats["stability"] == analytics.stability_score(6 / 7, stats["gw_loss_pct"], stats["rssi_std"])


def test_stability_score():
    assert analytics.stability_score(1.0, 0.0, 0.0) == 100
    assert analytics.stability_score(1.0, 10.0, 8.0) == 50
    assert analytics.stability_score(0.0, 0.0, 0.0) == 50
    assert analytics.stability_score(1.0, 5.0, 4.0) == 75
    assert analytics.stability_score(None, None, None) is None


def test_rooms_api_lists_every_unit(admin, room, make_unit, clock):
    make_unit("C-301")  # no data yet
    clock.now = T0 + timedelta(hours=1)
    data = admin.get("/api/admin/rooms?window=24h").json()
    by_name = {r["name"]: r for r in data["rooms"]}
    assert by_name["B-204"]["samples"] == 7 and by_name["B-204"]["rssi_median"] == -52.5
    assert by_name["C-301"]["samples"] == 0 and by_name["C-301"]["stability"] is None
    assert admin.get("/api/admin/rooms?window=1y").status_code == 422


def test_summary_and_candles_api(admin, room, clock, engine):
    clock.now = T0 + timedelta(hours=1)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO scans (time, unit_id, duration_ms, ap_count) VALUES (:t, :u, 9700, 2)"),
                     {"t": T0, "u": room["id"]})
        conn.execute(text("""INSERT INTO scan_observations (time, unit_id, bssid, ssid, channel, rssi, auth, hidden)
            VALUES (:t, :u, '02:00:00:aa:00:40', 'VIKK', 6, -41, 3, false), (:t, :u, '02:00:00:bb:00:00', '', 44, -80, 7, true)"""),
                     {"t": T0, "u": room["id"]})
    summary = admin.get(f"/api/admin/units/{room['id']}/summary?window=24h").json()
    assert summary["unit"]["name"] == "B-204"
    assert summary["access_points"][0]["bssid"] == "02:00:00:aa:00:40" and summary["access_points"][0]["samples"] == 6
    assert [n["rssi"] for n in summary["last_scan"]["networks"]] == [-41, -80]
    res = admin.get(f"/api/admin/units/{room['id']}/candles",
                    params={"metric": "rssi", "bucket": "5m", "from": T0.timestamp(), "to": (T0 + timedelta(minutes=10)).timestamp()})
    assert res.status_code == 200 and len(res.json()["candles"]) == 2 and res.json()["timezone"] == "Europe/Tallinn"
    default = admin.get(f"/api/admin/units/{room['id']}/candles?metric=gw_ms&bucket=1h").json()
    assert default["to"] - default["from"] == 7 * 86400
    quarter = admin.get(f"/api/admin/units/{room['id']}/candles?metric=rssi&bucket=15m").json()
    assert quarter["to"] - quarter["from"] == 86400
    assert admin.get(f"/api/admin/units/{room['id']}/candles?metric=nope").status_code == 422
    assert admin.get(f"/api/admin/units/{room['id']}/candles?bucket=2h").status_code == 422
    assert admin.get("/api/admin/units/unope/candles").status_code == 404
    assert admin.get("/api/admin/units/unope/summary").status_code == 404


def test_dashboard_api_requires_login(settings, engine, clock, room):
    with TestClient(create_app(settings, engine=engine, clock=clock, background=False)) as anonymous:
        for path in ("/api/admin/rooms", f"/api/admin/units/{room['id']}/summary",
                     f"/api/admin/units/{room['id']}/candles"):
            assert anonymous.get(path).status_code == 401, path


def test_chart_logo_style_hash_matches_the_vendored_library():
    import base64
    import hashlib
    import re

    from fleet.app import CHART_LOGO_STYLE_HASH, STATIC_DIR

    js = (STATIC_DIR / "vendor" / "lightweight-charts.standalone.production.js").read_text()
    css = re.search(r'this\.Kd\.innerText="([^"]+)"', js).group(1)
    assert CHART_LOGO_STYLE_HASH == "sha256-" + base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()


def test_dashboard_page_and_assets(client):
    page = client.get("/")
    assert page.status_code == 200 and "<title>Wi-Fi Fleet</title>" in page.text
    csp = page.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    for path in ("/static/app.js", "/static/styles.css", "/static/favicon.svg", "/static/labels.js",
                 "/static/serial-repl.js", "/static/provision-flow.js", "/static/heatmap.js",
                 "/static/vendor/lightweight-charts.standalone.production.js",
                 "/static/vendor/esptool-js-0.7.0.bundle.js", "/static/vendor/qrcode-generator-1.4.4.js"):
        assert client.get(path).status_code == 200, path
    assert client.get("/static/../app.py").status_code == 404


def test_seed_demo_creates_rooms_and_refuses_production(engine, settings, admin, clock):
    with engine.begin() as conn:
        ids = seed_demo(conn, settings, count=3, days=0.05, now=clock.now)
    rooms = admin.get("/api/admin/rooms?window=24h").json()["rooms"]
    assert len(ids) == 3 and all(r["samples"] > 800 for r in rooms)
    assert all(r["srv_median_ms"] is not None for r in rooms)
    with engine.begin() as conn, pytest.raises(DemoRefused):
        seed_demo(conn, replace(settings, environment="production"), count=1, days=0.01, now=clock.now)


def test_room_statistics_are_cached_per_window(admin, room, clock, engine):
    clock.now = T0 + timedelta(hours=1)
    first = admin.get("/api/admin/rooms?window=24h").json()
    add_samples(engine, room["id"], [(3000, True, -30, 1.0, None)])
    cached = admin.get("/api/admin/rooms?window=24h").json()
    assert cached["rooms"][0]["samples"] == first["rooms"][0]["samples"] == 7
    clock.advance(seconds=61)
    fresh = admin.get("/api/admin/rooms?window=24h").json()
    assert fresh["rooms"][0]["samples"] == 8


def test_cached_statistics_still_show_live_status(admin, client, room, clock, engine):
    from .conftest import hello_payload, unit_headers

    clock.now = T0 + timedelta(hours=1)
    assert admin.get("/api/admin/rooms?window=1h").json()["rooms"][0]["status"] == "never_seen"
    key = admin.post(f"/api/admin/units/{room['id']}/rotate-key").json()["key"]
    client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key))
    assert admin.get("/api/admin/rooms?window=1h").json()["rooms"][0]["status"] == "online"


def test_housekeeping_refreshes_the_7_day_comparison(admin, client, room, clock, engine):
    clock.now = T0 + timedelta(hours=1)
    assert admin.get("/api/admin/rooms?window=7d").json()["rooms"][0]["samples"] == 7
    add_samples(engine, room["id"], [(3000, True, -30, 1.0, None)])
    clock.advance(seconds=241)
    client.app.state.housekeeping()
    clock.advance(seconds=10)
    assert admin.get("/api/admin/rooms?window=7d").json()["rooms"][0]["samples"] == 8
