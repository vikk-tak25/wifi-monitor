"""The unit agent (unit/app.py) and boot loader (unit/main.py), run against the real server."""

from dataclasses import replace
import json
import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from fleet.app import create_app
from fleet.tables import events, link_samples, scan_observations, scans, units

from .agent_harness import AP_FAR, AP_NEAR, AP_OTHER, UNIT_DIR, Reset, Rig, load_module
from .conftest import hello_payload, unit_headers

APP_SOURCE = (UNIT_DIR / "app.py").read_text()
CURRENT = re.search(r'^AGENT_VERSION = "([^"]+)"$', APP_SOURCE, re.MULTILINE).group(1)
NEXT = CURRENT + "-next"


@pytest.fixture
def unit_fs(tmp_path, monkeypatch):
    """The agent works in its current directory, like on the board."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.py").write_text(APP_SOURCE)
    return tmp_path


@pytest.fixture
def app_mod():
    return load_module(UNIT_DIR / "app.py")


@pytest.fixture
def rig(unit_fs, app_mod, client, clock, make_unit):
    unit, key = make_unit("B-204")
    r = Rig(app_mod, client, clock, key)
    r.unit = unit
    r.agent.boot("power_on")
    return r


def rows(engine, table, *order):
    with engine.connect() as conn:
        return conn.execute(select(table).order_by(*order)).all()


def kinds(engine, source="unit"):
    with engine.connect() as conn:
        return conn.execute(select(events.c.kind).where(events.c.source == source)
                            .order_by(events.c.time, events.c.id)).scalars().all()


def flush_now(r):
    r.agent.next_batch = r.ticks.now
    r.agent.step()


# -- normal operation ----------------------------------------------------------------------

def test_full_cycle_against_server(rig, engine, clock):
    rig.run(61)
    a = rig.agent
    assert rig.wlan.connect_calls == [("VIKK", "secret-pw", bytes.fromhex(AP_NEAR[0].replace(":", "")))]
    assert a.hello_ok and a.clock.synced()

    with engine.connect() as conn:
        unit = conn.execute(select(units).where(units.c.id == rig.unit["id"])).first()
    assert unit.online and unit.agent_version == CURRENT and unit.last_bssid == AP_NEAR[0]
    assert unit.last_ssid == "VIKK" and unit.mac == "02:00:00:c5:00:01"

    samples = rows(engine, link_samples, link_samples.c.time)
    assert len(samples) == 12  # every 5 s from the first connection (at 5 s) to the batch at 60 s
    assert all(s.connected and s.rssi == -45 and s.bssid == AP_NEAR[0] and s.channel == 9 for s in samples)
    assert all(s.gw_ms == 3.5 and s.gw_lost is False for s in samples)
    assert set(rig.pings) == {"10.0.5.1"}  # the gateway
    with_srv = [s for s in samples if s.srv_ms is not None]
    assert len(with_srv) == 2 and all(s.srv_lost is False for s in with_srv)  # every 30 s
    # Sample times match the (simulated) moment they were taken, to the second.
    start = clock.now - rig.elapsed()
    assert abs((samples[0].time - start).total_seconds() - 5) <= 1
    assert all(5 - 1 <= (b.time - a_.time).total_seconds() <= 5 + 1 for a_, b in zip(samples, samples[1:]))

    # The connect scan doubles as the first scan measurement.
    assert len(rows(engine, scans)) == 1
    observed = {o.bssid for o in rows(engine, scan_observations)}
    assert {AP_NEAR[0], AP_FAR[0], "02:00:00:00:00:01"} == observed
    assert kinds(engine) == ["boot", "wifi_connected"]


def test_periodic_scans_follow_server_config(rig, engine, admin):
    admin.patch(f"/api/admin/units/{rig.unit['id']}", json={"config": {"scan_interval_s": 30,
                                                                      "sample_interval_s": 10}})
    rig.run(125)
    assert rig.agent.cfg["scan_interval_s"] == 30
    assert len(rows(engine, scans)) == 4  # connect scan + every 30 s up to the batch at 120 s
    assert len(rig.agent.scans) == 1      # the scan at ~122 s waits for the next batch
    assert 12 <= len(rows(engine, link_samples)) <= 14  # every 10 s


def test_batch_ids_are_unique_per_boot(rig, engine):
    rig.run(181)
    with engine.connect() as conn:
        ids = conn.execute(select(func.count()).select_from(link_samples)).scalar_one()
    assert ids == 36
    assert rig.agent.batch_seq == 3


# -- outages ----------------------------------------------------------------------------------

def test_server_outage_spools_to_flash_and_recovers(rig, engine, unit_fs):
    rig.run(61)
    rig.link.down = True
    rig.run(240)
    spooled = sorted(os.listdir(unit_fs / "spool"))
    assert len(spooled) == 4
    assert len(rows(engine, link_samples)) == 12
    samples_during = sum(len(json.loads((unit_fs / "spool" / n).read_text())["samples"]) for n in spooled)
    assert samples_during == 48

    rig.link.down = False
    rig.run(61)
    assert os.listdir(unit_fs / "spool") == []
    assert len(rows(engine, link_samples)) == 12 + 48 + 12
    kinds_seen = kinds(engine)
    assert "server_unreachable" in kinds_seen and "server_reachable" in kinds_seen
    # During the outage the server pings were lost, but the gateway pings were not.
    lost_srv = [s for s in rows(engine, link_samples) if s.srv_lost]
    assert len(lost_srv) >= 7 and all(s.gw_lost is False for s in lost_srv)


def test_spool_survives_reboot(rig, engine, unit_fs, app_mod, client, clock):
    rig.run(61)
    rig.link.down = True
    rig.run(120)
    assert len(os.listdir(unit_fs / "spool")) == 2
    # Reboot: a new Agent on the same filesystem, server reachable again.
    from .agent_harness import Rig as NewRig

    again = NewRig(app_mod, client, clock, rig.agent.key)
    again.ticks.now = rig.ticks.now
    again.agent.boot("wdt")
    again.run(61)
    assert os.listdir(unit_fs / "spool") == []
    assert len(rows(engine, link_samples)) == 12 + 24 + 12
    with engine.connect() as conn:
        boots = conn.execute(select(events.c.detail).where(events.c.kind == "boot")).scalars().all()
    assert sorted(b["reset_cause"] for b in boots) == ["power_on", "wdt"]


def test_measures_before_first_handshake_and_backdates(unit_fs, app_mod, client, clock, make_unit, engine):
    _, key = make_unit()
    r = Rig(app_mod, client, clock, key)
    r.agent.boot("power_on")
    r.link.down = True
    r.run(120)
    assert not r.agent.clock.synced() and len(r.agent.samples) == 23
    assert os.listdir(unit_fs / "spool") == []  # no time yet, nothing can be spooled
    r.link.down = False
    r.run(65)
    samples = rows(engine, link_samples, link_samples.c.time)
    assert len(samples) >= 36
    start = clock.now - r.elapsed()
    assert abs((samples[0].time - start).total_seconds() - 5) <= 1


def test_ram_buffer_is_bounded_before_handshake(unit_fs, app_mod, client, clock, make_unit, engine):
    _, key = make_unit()
    r = Rig(app_mod, client, clock, key)
    r.agent.boot("power_on")
    r.link.down = True
    r.run(25 * 60, step_ms=5000)
    assert len(r.agent.samples) == app_mod.MAX_SAMPLES
    r.link.down = False
    r.run(65, step_ms=1000)
    overflow = [e for e in rows(engine, events) if e.kind == "buffer_overflow"]
    assert overflow and overflow[0].detail["dropped"] > 0


def test_wifi_loss_reconnect_and_roam(rig, engine):
    rig.run(20)
    rig.wlan.connected = False
    rig.wlan.status_code = 200
    rig.wlan.aps = {AP_OTHER[0]: AP_OTHER, AP_FAR[0]: AP_FAR}  # the near AP is gone
    rig.run(2)
    rig.run(60)
    flush_now(rig)
    assert rig.wlan.connect_calls[-1][2] == bytes.fromhex(AP_OTHER[0].replace(":", ""))
    seen = kinds(engine)
    assert seen[:2] == ["boot", "wifi_connected"]
    assert "wifi_lost" in seen and seen.count("wifi_connected") == 2 and "roam" in seen
    with engine.connect() as conn:
        roam = conn.execute(select(events).where(events.c.kind == "roam")).first()
        lost = conn.execute(select(events).where(events.c.kind == "wifi_lost")).first()
    assert roam.detail == {"from": AP_NEAR[0], "to": AP_OTHER[0], "channel": AP_OTHER[1]}
    assert roam.message == f"Roamed from {AP_NEAR[0]} to {AP_OTHER[0]} (channel 132)"
    assert lost.message == "Wi-Fi connection lost: beacon timeout after 19 s connected"
    assert any(not s.connected for s in rows(engine, link_samples))
    assert rows(engine, link_samples, link_samples.c.time)[-1].bssid == AP_OTHER[0]


def test_hidden_network_connects_unpinned_and_identifies_ap(rig, engine):
    rig.wlan.hidden = True
    rig.wlan.channel = AP_FAR[1]  # the firmware ended up on the far AP's channel
    rig.run(61)
    assert rig.wlan.connect_calls == [("VIKK", "secret-pw", None)]
    samples = rows(engine, link_samples)
    assert samples and all(s.bssid == AP_FAR[0] and s.channel == AP_FAR[1] for s in samples)
    with engine.connect() as conn:
        connected = conn.execute(select(events).where(events.c.kind == "wifi_connected")).first()
    assert connected.detail["bssid"] == AP_FAR[0]


def test_hidden_network_on_unknown_channel_still_measures(rig, engine):
    rig.wlan.hidden = True
    rig.wlan.channel = 157
    rig.run(61)
    samples = rows(engine, link_samples)
    assert samples and all(s.connected and s.bssid is None and s.channel == 157 for s in samples)


def test_network_not_found_backs_off_then_reboots(rig):
    rig.wlan.aps = {}
    with pytest.raises(Reset):
        rig.run(11 * 60)
    scans_done = rig.wlan.scan_count
    assert 10 <= scans_done <= 16  # 5, 10, 20, 40, 60, 60 ... seconds apart, not every step
    assert rig.hw.resets == 1
    assert [e[1] for e in rig.agent.events if e[1] == "rebooting"] == ["rebooting"]


def test_wrong_password_is_reported(rig, engine):
    rig.wlan.auto_connect = False
    rig.wlan.status_code = 202
    rig.run(30)
    failures = [e for e in rig.agent.events if e[1] == "wifi_connect_failed"]
    assert failures and failures[0][2] == {"reason": "wrong password"}


def test_revoked_key_backs_off(rig, admin, engine):
    rig.run(61)
    admin.post(f"/api/admin/units/{rig.unit['id']}/revoke")
    rig.agent.next_hello = rig.ticks.now
    calls_before = len(rig.link.calls)
    rig.run(300)
    hellos = [c for c in rig.link.calls[calls_before:] if c[1] == "/api/unit/v1/hello"]
    assert len(hellos) == 1  # then waits 10 minutes
    assert any(e[1] == "auth_failed" for e in rig.agent.events) or os.listdir("spool")


def test_rejected_batch_is_dropped_not_retried(rig, engine, unit_fs):
    rig.run(61)
    (unit_fs / "spool" / "00000001.json").write_text('{"batch_id":"bad id!","samples":[]}')
    rig.agent.spool.seq = 2
    rig.run(122)  # dropped at the next batch, reported in the one after
    assert os.listdir(unit_fs / "spool") == []
    rejected = [e for e in rows(engine, events) if e.kind == "batch_rejected"]
    assert len(rejected) == 1 and "HTTP 422" in rejected[0].message


# -- signed transport (#9) ---------------------------------------------------------------

def test_hmac_matches_python(app_mod):
    import hashlib
    import hmac

    for key in (b"", b"k" * 32, b"x" * 64, b"y" * 100):
        for message in (b"", b"hello", b"z" * 1000):
            assert app_mod.hmac_sha256(key, message) == hmac.new(key, message, hashlib.sha256).digest()


def test_requests_are_signed_and_never_carry_the_key(rig):
    rig.run(61)
    assert rig.link.calls[0] == ("GET", "/api/unit/v1/time")
    secret = rig.agent.key.split(".", 1)[1]
    for headers, body in rig.link.sent[1:]:
        assert "Authorization" not in headers and {"X-Unit-Id", "X-Timestamp", "X-Nonce", "X-Signature"} <= set(headers)
        assert secret not in json.dumps(headers) and secret.encode() not in (body or b"")
    nonces = [h["X-Nonce"] for h, _ in rig.link.sent[1:]]
    assert len(nonces) == len(set(nonces))


def test_tampered_responses_are_rejected(rig, engine):
    rig.link.tamper = True
    rig.run(30)
    assert not rig.agent.hello_ok
    assert [e for e in rig.agent.events if e[1] == "bad_signature"]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(link_samples)).scalar_one() == 0
    rig.link.tamper = False
    rig.run(65)
    assert rig.agent.hello_ok
    assert "bad_signature" in kinds(engine)  # reported once the server can be trusted again


# -- remote update -----------------------------------------------------------------------------

@pytest.fixture
def server_with_update(settings, engine, clock, tmp_path_factory):
    agent_dir = tmp_path_factory.mktemp("agent")
    new_source = APP_SOURCE.replace(f'AGENT_VERSION = "{CURRENT}"', f'AGENT_VERSION = "{NEXT}"')
    (agent_dir / "app.py").write_text(new_source)
    app = create_app(replace(settings, agent_path=agent_dir / "app.py"), engine=engine, clock=clock,
                     background=False)
    with TestClient(app) as c:
        yield c, new_source


def test_remote_update_installs_and_confirms(unit_fs, app_mod, server_with_update, clock, make_unit, engine):
    client, new_source = server_with_update
    _, key = make_unit()
    r = Rig(app_mod, client, clock, key)
    r.agent.boot("power_on")
    with pytest.raises(Reset):
        r.run(10)
    assert (unit_fs / "app.py").read_text() == new_source
    assert (unit_fs / "app_prev.py").read_text() == APP_SOURCE
    pending = json.loads((unit_fs / "update_pending.json").read_text())
    assert pending == {"version": NEXT, "previous": CURRENT, "tries": 0}
    assert len(os.listdir(unit_fs / "spool")) == 1  # events saved before the reboot

    # Boot into the new version: it confirms the update after its first handshake.
    new_mod = load_module(unit_fs / "app.py")
    assert new_mod.AGENT_VERSION == NEXT
    r2 = Rig(new_mod, client, clock, key)
    r2.ticks.now = r.ticks.now
    r2.agent.boot("soft")
    r2.run(61)
    assert not (unit_fs / "update_pending.json").exists()
    seen = kinds(engine)
    assert "update_started" in seen and "update_ok" in seen and "rebooting" in seen
    with engine.connect() as conn:
        assert conn.execute(select(units.c.agent_version)).scalar_one() == NEXT
        ok = conn.execute(select(events).where(events.c.kind == "update_ok")).first()
    assert ok.message == f"Agent updated to {NEXT}"


def test_update_with_bad_hash_is_refused(rig, unit_fs):
    rig.agent.apply_update({"version": "9.9.9", "sha256": "0" * 64, "size": 10, "url": "/api/unit/v1/agent"})
    assert (unit_fs / "app.py").read_text() == APP_SOURCE
    assert not (unit_fs / "update_pending.json").exists()
    assert rig.agent.events[-1][1] == "update_failed"


def test_failed_version_is_skipped_and_reported(rig, unit_fs):
    (unit_fs / "update_failed.json").write_text(json.dumps({"version": NEXT, "error": "crash", "reported": False}))
    rig.agent.confirm_update()
    rig.agent.apply_update({"version": NEXT, "sha256": "x", "size": 1})
    assert [e[1] for e in rig.agent.events[-2:]] == ["update_failed", "update_skipped"]
    assert json.loads((unit_fs / "update_failed.json").read_text())["reported"] is True


# -- boot loader (unit/main.py) ----------------------------------------------------------------

@pytest.fixture
def loader(unit_fs):
    return load_module(UNIT_DIR / "main.py")


class FakeApp:
    def __init__(self, error=None):
        self.error = error
        self.ran = 0

    def run(self):
        self.ran += 1
        if self.error:
            raise self.error


def test_loader_runs_app(loader):
    app, resets = FakeApp(), []
    loader.start(lambda: app, lambda: resets.append(1), lambda s: None)
    assert app.ran == 1 and resets == []


def test_loader_counts_tries_and_rolls_back_unconfirmed_update(loader, unit_fs):
    (unit_fs / "app.py").write_text("new")
    (unit_fs / "app_prev.py").write_text("old")
    (unit_fs / "update_pending.json").write_text('{"version": "2.0.1", "previous": "2.0.0", "tries": 0}')
    resets = []
    for _ in range(2):
        loader.start(lambda: FakeApp(), lambda: resets.append(1), lambda s: None)
    assert json.loads((unit_fs / "update_pending.json").read_text())["tries"] == 2
    loader.start(lambda: FakeApp(), lambda: resets.append(1), lambda s: None)
    assert (unit_fs / "app.py").read_text() == "old" and not (unit_fs / "app_prev.py").exists()
    assert not (unit_fs / "update_pending.json").exists()
    failed = json.loads((unit_fs / "update_failed.json").read_text())
    assert failed["version"] == "2.0.1" and failed["reported"] is False and "not confirmed" in failed["error"]
    assert resets == [1]


def test_loader_rolls_back_when_new_version_crashes(loader, unit_fs):
    (unit_fs / "app.py").write_text("new")
    (unit_fs / "app_prev.py").write_text("old")
    (unit_fs / "update_pending.json").write_text('{"version": "2.0.1", "tries": 0}')
    resets = []
    loader.start(lambda: FakeApp(ValueError("boom")), lambda: resets.append(1), lambda s: None)
    assert (unit_fs / "app.py").read_text() == "old"
    assert "boom" in json.loads((unit_fs / "update_failed.json").read_text())["error"]
    assert "boom" in (unit_fs / "last_error.txt").read_text()
    assert resets == [1]


def test_loader_crash_without_update_logs_and_restarts(loader, unit_fs, rig):
    resets, sleeps = [], []
    loader.start(lambda: FakeApp(RuntimeError("socket died")), lambda: resets.append(1), sleeps.append)
    assert resets == [1] and sleeps == [10]
    assert (unit_fs / "app.py").read_text() == APP_SOURCE
    assert not (unit_fs / "update_failed.json").exists()
    # The next boot reports the crash.
    rig.agent.boot("soft")
    assert "socket died" in rig.agent.events[-1][2]["error"]
    assert not (unit_fs / "last_error.txt").exists()


def test_loader_lets_ctrl_c_through(loader):
    with pytest.raises(KeyboardInterrupt):
        loader.start(lambda: FakeApp(KeyboardInterrupt()), lambda: None, lambda s: None)


# -- pieces ------------------------------------------------------------------------------------

def test_clock_uses_integer_math(app_mod):
    now = [5000]
    clock = app_mod.Clock(lambda: now[0])
    assert not clock.synced()
    now[0] = 5040  # 40 ms round trip
    clock.sync(1_792_138_472_967, 5000)
    assert clock.base_ms == 1_792_138_472_987
    assert clock.epoch_s() == 1_792_138_472
    now[0] += 60_500
    assert clock.epoch_s() == 1_792_138_533
    assert clock.epoch_s(5040 - 3000) == 1_792_138_469


def test_spool_limit_drops_oldest(app_mod, unit_fs):
    spool = app_mod.Spool(limit=3)
    for i in range(5):
        spool.put('{"n":%d}' % i)
    assert spool.count() == 3 and spool.read(spool.oldest()) == '{"n":2}'
    assert app_mod.Spool(limit=3).seq == 6  # continues numbering after a reboot


# -- server side of updates ---------------------------------------------------------------------

def test_hello_offers_update_only_for_other_versions(server_with_update, make_unit):
    client, new_source = server_with_update
    _, key = make_unit()
    offer = client.post("/api/unit/v1/hello", json=hello_payload(agent_version=CURRENT),
                        headers=unit_headers(key)).json()["update"]
    import hashlib

    assert offer == {"version": NEXT, "sha256": hashlib.sha256(new_source.encode()).hexdigest(),
                     "size": len(new_source.encode()), "url": "/api/unit/v1/agent"}
    current = client.post("/api/unit/v1/hello", json=hello_payload(agent_version=NEXT), headers=unit_headers(key))
    assert current.json()["update"] is None
    res = client.get("/api/unit/v1/agent", headers=unit_headers(key))
    assert res.status_code == 200 and res.content == new_source.encode()
    assert res.headers["X-Agent-Version"] == NEXT
    assert client.get("/api/unit/v1/agent").status_code == 401


def test_no_agent_bundle_means_no_updates(client, make_unit):
    _, key = make_unit()
    assert client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key)).json()["update"] is None
    assert client.get("/api/unit/v1/agent", headers=unit_headers(key)).status_code == 404


def test_agent_bundle_validation(tmp_path):
    from fleet.agentbundle import AgentSource, read_bundle

    path = tmp_path / "app.py"
    path.write_text("print('no version')\n")
    assert read_bundle(path) is None
    path.write_text('AGENT_VERSION = "3.1.4"\n')
    source = AgentSource(path)
    assert source.get().version == "3.1.4"
    path.write_text('AGENT_VERSION = "3.1.5"\n' + "#" * 10)
    os.utime(path, (1, 1))
    assert source.get().version == "3.1.5"
    path.write_bytes(b'AGENT_VERSION = "1"\n' + b"#" * 300 * 1024)
    assert read_bundle(path) is None
    assert AgentSource(tmp_path / "missing.py").get() is None
    assert AgentSource(None).get() is None


def test_repository_agent_is_a_valid_bundle():
    from fleet.agentbundle import read_bundle

    bundle = read_bundle(UNIT_DIR / "app.py")
    assert bundle is not None and bundle.version == CURRENT
