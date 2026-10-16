from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from fleet.tables import events, ingest_batches, link_samples, scan_observations, scans, units

from .conftest import hello_payload, unit_headers


def count(engine, table, **where):
    query = select(func.count()).select_from(table)
    for column, value in where.items():
        query = query.where(table.c[column] == value)
    with engine.connect() as conn:
        return conn.execute(query).scalar_one()


def sample(t, **extra):
    return {"t": t, "connected": True, "rssi": -55, "bssid": "02:00:00:aa:00:40", "channel": 9,
            "gw_ms": 3.2, "gw_lost": False, **extra}


def batch(clock, batch_id="b1", **parts):
    return {"batch_id": batch_id, "sent_at": clock.ts, **parts}


@pytest.fixture
def unit(make_unit):
    return make_unit("B-204")


# -- authentication ------------------------------------------------------------------------

@pytest.mark.parametrize("header", [None, "Basic abc", "Bearer ", "Bearer nonsense",
                                    "Bearer uaaaaaaaaaa." + "a" * 32])
def test_unit_api_rejects_bad_keys(client, unit, header):
    headers = {"Authorization": header} if header else {}
    for path, payload in (("/api/unit/v1/hello", hello_payload()), ("/api/unit/v1/batch", {})):
        res = client.post(path, json=payload, headers=headers)
        assert res.status_code == 401, (path, header)
        assert res.headers["WWW-Authenticate"] == "Bearer"
    assert client.get("/api/unit/v1/ping", headers=headers).status_code == 401


def test_key_of_one_unit_with_id_of_another_is_rejected(client, make_unit):
    a, key_a = make_unit("A")
    b, _ = make_unit("B")
    forged = b["id"] + "." + key_a.split(".", 1)[1]
    assert client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(forged)).status_code == 401


# -- handshake ---------------------------------------------------------------------------------

def test_hello_returns_time_and_config_and_marks_online(client, engine, unit, clock):
    info, key = unit
    res = client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key))
    assert res.status_code == 200
    body = res.json()
    assert body == {"unit_id": info["id"], "name": "B-204", "server_time": clock.ts, "update": None,
                    "server_time_ms": int(clock.ts * 1000),
                    "config": {"sample_interval_s": 5.0, "batch_interval_s": 60.0, "scan_interval_s": 600.0,
                               "server_ping_interval_s": 30.0, "ping_target": "gateway"}}
    with engine.connect() as conn:
        row = conn.execute(select(units).where(units.c.id == info["id"])).first()
        kinds = conn.execute(select(events.c.kind).order_by(events.c.id)).scalars().all()
    assert row.online and row.agent_version == "2.0.0" and row.fw_version == "1.29.0"
    assert row.mac == "02:00:00:c5:00:01" and row.last_rssi == -41 and row.last_bssid == "02:00:00:aa:00:40"
    assert row.last_ip == "testclient" and row.last_seen_at == clock.now
    assert kinds == ["unit_created", "unit_online", "hello"]


def test_hello_applies_unit_config_overrides(client, make_unit):
    _, key = make_unit("C-1", config={"scan_interval_s": 0, "ping_target": "10.0.0.1"})
    config = client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key)).json()["config"]
    assert config["scan_interval_s"] == 0 and config["ping_target"] == "10.0.0.1"


def test_hello_from_different_board_is_flagged(client, admin, unit):
    info, key = unit
    client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key))
    client.post("/api/unit/v1/hello", json=hello_payload(mac="aa:bb:cc:dd:ee:ff"), headers=unit_headers(key))
    flagged = admin.get(f"/api/admin/events?unit_id={info['id']}&kind=mac_mismatch").json()["events"]
    assert len(flagged) == 1 and "aa:bb:cc:dd:ee:ff" in flagged[0]["message"]


@pytest.mark.parametrize("change", [
    {"agent_version": ""}, {"agent_version": "1 0"}, {"rssi": 5}, {"channel": 0}, {"mac": "d0cf13e51254"},
    {"ssid": "x" * 33}, {"ssid": "ä" * 17}, {"uptime_s": -1}, {"surprise": True},
])
def test_hello_validation(client, unit, change):
    _, key = unit
    res = client.post("/api/unit/v1/hello", json=hello_payload(**change), headers=unit_headers(key))
    assert res.status_code == 422
    assert res.json()["error"] == "invalid request" and res.json()["details"]


def test_hello_body_size_limit(client, unit):
    _, key = unit
    res = client.post("/api/unit/v1/hello", content=b"{" + b" " * 9000 + b"}",
                      headers={**unit_headers(key), "Content-Type": "application/json"})
    assert res.status_code == 413


def test_ping(client, unit, clock):
    _, key = unit
    assert client.get("/api/unit/v1/ping", headers=unit_headers(key)).json() == {
        "server_time": clock.ts, "server_time_ms": int(clock.ts * 1000)}


# -- batches -------------------------------------------------------------------------------

def test_batch_is_stored(client, engine, unit, clock):
    info, key = unit
    t0 = clock.ts - 60
    payload = batch(
        clock,
        samples=[sample(t0), sample(t0 + 5, gw_ms=None, gw_lost=True, rssi=-60),
                 {"t": t0 + 10, "connected": False}, sample(t0 + 15, rssi=-48, srv_ms=41.5, srv_lost=False)],
        scans=[{"t": t0 + 20, "duration_ms": 9700,
                "aps": [["VIKK", "02:00:00:AA:00:40", 9, -30, 3, False], ["", "02:00:00:bb:00:b0", 108, -73, 7, True]]}],
        events=[{"t": t0 + 12, "kind": "wifi_lost", "detail": {"reason": "beacon timeout"}},
                {"t": t0 + 14, "kind": "roam", "detail": {"from": "aa:aa:aa:aa:aa:01", "to": "aa:aa:aa:aa:aa:02",
                                                          "channel": 36}}])
    res = client.post("/api/unit/v1/batch", json=payload, headers=unit_headers(key))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["duplicate"] is False and body["clock_skew_s"] == 0
    assert body["stored"] == {"samples": 4, "scans": 1, "observations": 2, "events": 2, "dropped": 0}

    assert count(engine, link_samples, unit_id=info["id"]) == 4
    assert count(engine, scans) == 1 and count(engine, scan_observations) == 2
    with engine.connect() as conn:
        lost = conn.execute(select(link_samples).where(link_samples.c.gw_lost.is_(True))).first()
        obs = conn.execute(select(scan_observations).order_by(scan_observations.c.rssi.desc())).first()
        unit_row = conn.execute(select(units).where(units.c.id == info["id"])).first()
        unit_events = conn.execute(select(events).where(events.c.source == "unit").order_by(events.c.time)).all()
    assert lost.gw_ms is None and lost.rssi == -60
    assert obs.bssid == "02:00:00:aa:00:40" and obs.ssid == "VIKK"
    assert unit_row.last_rssi == -48 and unit_row.online
    assert [(e.kind, e.level, e.message) for e in unit_events] == [
        ("wifi_lost", "warning", "Wi-Fi connection lost: beacon timeout"),
        ("roam", "info", "Roamed from aa:aa:aa:aa:aa:01 to aa:aa:aa:aa:aa:02 (channel 36)"),
    ]


def test_duplicate_batch_is_acknowledged_but_not_stored_twice(client, engine, unit, clock):
    _, key = unit
    payload = batch(clock, samples=[sample(clock.ts - 1)])
    assert client.post("/api/unit/v1/batch", json=payload, headers=unit_headers(key)).json()["duplicate"] is False
    again = client.post("/api/unit/v1/batch", json=payload, headers=unit_headers(key))
    assert again.status_code == 200 and again.json()["duplicate"] is True and again.json()["stored"] is None
    assert count(engine, link_samples) == 1 and count(engine, ingest_batches) == 1


def test_same_batch_id_from_two_units_is_not_a_duplicate(client, engine, make_unit, clock):
    _, key_a = make_unit("A")
    _, key_b = make_unit("B")
    payload = batch(clock, samples=[sample(clock.ts - 1)])
    for key in (key_a, key_b):
        assert client.post("/api/unit/v1/batch", json=payload, headers=unit_headers(key)).json()["duplicate"] is False
    assert count(engine, link_samples) == 2


def test_clock_skew_is_corrected(client, engine, unit, clock):
    _, key = unit
    behind = clock.ts - 300  # unit clock is five minutes slow
    payload = {"batch_id": "skew", "sent_at": behind, "samples": [sample(behind - 10)]}
    body = client.post("/api/unit/v1/batch", json=payload, headers=unit_headers(key)).json()
    assert body["clock_skew_s"] == 300
    with engine.connect() as conn:
        stored = conn.execute(select(link_samples.c.time)).scalar_one()
        skew_event = conn.execute(select(events).where(events.c.kind == "clock_skew")).first()
    assert stored == clock.now - timedelta(seconds=10)
    assert skew_event.level == "warning"


def test_small_skew_is_left_alone(client, engine, unit, clock):
    _, key = unit
    payload = {"batch_id": "s", "sent_at": clock.ts - 1.5, "samples": [sample(clock.ts - 11.5)]}
    client.post("/api/unit/v1/batch", json=payload, headers=unit_headers(key))
    with engine.connect() as conn:
        assert conn.execute(select(link_samples.c.time)).scalar_one() == clock.now - timedelta(seconds=11.5)


def test_implausible_timestamps_are_dropped(client, engine, unit, clock):
    _, key = unit
    payload = batch(clock, samples=[sample(clock.ts - 40 * 86400), sample(clock.ts + 3600), sample(clock.ts - 5)],
                    events=[{"t": clock.ts + 3600, "kind": "boot"}])
    stored = client.post("/api/unit/v1/batch", json=payload, headers=unit_headers(key)).json()["stored"]
    assert stored["samples"] == 1 and stored["events"] == 0 and stored["dropped"] == 3


@pytest.mark.parametrize("bad", [
    {"batch_id": "has space"},
    {"batch_id": "x" * 65},
    {"sent_at": 12},
    {"samples": [{"t": 1792135883.0, "connected": True, "rssi": 3}]},
    {"samples": [{"t": 1792135883.0, "connected": True, "bssid": "zz:zz:zz:zz:zz:zz"}]},
    {"samples": [{"t": 1792135883.0, "connected": True, "gw_ms": 3.0, "gw_lost": True}]},
    {"samples": [{"t": 1792135883.0, "connected": True, "gw_ms": -1}]},
    {"samples": [{"t": 1792135883.0}]},
    {"samples": [{"t": 1792135883.0, "connected": True}] * 2001},
    {"scans": [{"t": 1792135883.0, "aps": [["a", "aa:aa:aa:aa:aa:aa", 1, -50, 3]]}]},
    {"scans": [{"t": 1792135883.0, "aps": [["a", "aa:aa:aa:aa:aa:aa", 300, -50, 3, False]]}]},
    {"events": [{"t": 1792135883.0, "kind": "Bad Kind"}]},
    {"events": [{"t": 1792135883.0, "kind": "error", "detail": {"m": "x" * 3000}}]},
    {"extra": 1},
])
def test_batch_validation(client, engine, unit, clock, bad):
    _, key = unit
    payload = {**batch(clock), **bad}
    res = client.post("/api/unit/v1/batch", json=payload, headers=unit_headers(key))
    assert res.status_code == 422, bad
    assert count(engine, ingest_batches) == 0


def test_batch_size_limit(client, unit):
    _, key = unit
    res = client.post("/api/unit/v1/batch", content=b"x" * (512 * 1024 + 1),
                      headers={**unit_headers(key), "Content-Type": "application/json"})
    assert res.status_code == 413


def test_failed_batch_is_rolled_back_and_can_be_retried(client, engine, unit, clock, monkeypatch):
    from fleet import ingest

    _, key = unit
    original = ingest.add_event

    def boom(*args, **kwargs):
        if kwargs.get("kind") == "boot":
            raise RuntimeError("database hiccup")
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest, "add_event", boom)
    payload = batch(clock, samples=[sample(clock.ts - 2)], events=[{"t": clock.ts - 1, "kind": "boot"}])
    with pytest.raises(RuntimeError):
        client.post("/api/unit/v1/batch", json=payload, headers=unit_headers(key))
    assert count(engine, ingest_batches) == 0 and count(engine, link_samples) == 0
    monkeypatch.setattr(ingest, "add_event", original)
    assert client.post("/api/unit/v1/batch", json=payload, headers=unit_headers(key)).json()["duplicate"] is False


# -- online / offline ------------------------------------------------------------------------

def test_offline_and_back_online(client, admin, engine, unit, clock):
    info, key = unit
    client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key))
    housekeeping = client.app.state.housekeeping

    clock.advance(minutes=9)
    housekeeping()
    assert admin.get(f"/api/admin/units/{info['id']}").json()["unit"]["status"] == "online"

    clock.advance(minutes=2)
    housekeeping()
    housekeeping()  # idempotent: only one offline event
    assert admin.get(f"/api/admin/units/{info['id']}").json()["unit"]["status"] == "offline"
    offline = admin.get(f"/api/admin/events?unit_id={info['id']}&kind=unit_offline").json()["events"]
    assert len(offline) == 1 and offline[0]["message"] == "Unit offline: nothing received for 11 min"

    clock.advance(minutes=30)
    client.post("/api/unit/v1/batch", json=batch(clock, samples=[sample(clock.ts)]), headers=unit_headers(key))
    back = admin.get(f"/api/admin/events?unit_id={info['id']}&kind=unit_online").json()["events"]
    assert back[0]["message"] == "Unit back online after 41 min"


def test_hypertables_exist(engine):
    with engine.connect() as conn:
        names = conn.execute(text("SELECT hypertable_name FROM timescaledb_information.hypertables")).scalars().all()
    assert set(names) == {"link_samples", "scans", "scan_observations"}
