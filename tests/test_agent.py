"""The MicroPython agent's logic, run under CPython with a fake WLAN.

These tests also check the contract: every line the agent emits must parse with wifimon.protocol.
"""

import importlib.util
import io
import json
from pathlib import Path

import pytest

from wifimon.protocol import Event, Hello, Link, Scan, Status, parse_line

AGENT_PATH = Path(__file__).resolve().parent.parent / "device" / "agent.py"


def load_agent():
    spec = importlib.util.spec_from_file_location("wifimon_agent_under_test", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent_mod = load_agent()


class FakeWLAN:
    def __init__(self):
        self.is_active = False
        self.connected = False
        self.connect_calls = []
        self.status_code = 1001
        self.rssi = -48
        self.results = [
            (b"VIKK", b"\x02\x00\x00\xaa\x00@", 13, -30, 3, False),
            (b"\xff\xfebad", b"\x01\x02\x03\x04\x05\x06", 36, -70, 7, True),
        ]

    def active(self, value=None):
        if value is not None:
            self.is_active = value
        return self.is_active

    def scan(self):
        return list(self.results)

    def connect(self, ssid, password):
        self.connect_calls.append((ssid, password))

    def disconnect(self):
        self.connected = False

    def isconnected(self):
        return self.connected

    def status(self, param=None):
        if param == "rssi":
            if not self.connected:
                raise OSError("Wifi Not Connected")
            return self.rssi
        return self.status_code

    def ipconfig(self, key):
        return "192.168.1.1"

    def ifconfig(self):
        return ("192.168.1.57", "255.255.255.0", "192.168.1.1", "8.8.8.8")

    def config(self, key):
        return {"channel": 6, "bssid": b"\xaa\xbb\xcc\xdd\xee\xff"}[key]


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


@pytest.fixture
def rig():
    out = io.StringIO()
    wlan = FakeWLAN()
    clock = FakeClock()
    pings = []

    def ping(host, timeout, seq):
        pings.append((host, seq))
        return None if len(pings) % 4 == 0 else 3.5

    agent = agent_mod.Agent(wlan, out, ping=ping, clock=clock, info={"fw": "1.29.0"})

    def messages():
        lines = out.getvalue().splitlines()
        out.seek(0)
        out.truncate()
        parsed = [parse_line(line) for line in lines]
        assert all(m is not None for m in parsed), lines
        return parsed

    return agent, wlan, clock, pings, messages


def test_hello_parses(rig):
    agent, _, _, _, messages = rig
    agent.handle_line('{"cmd": "hello"}')
    [msg] = messages()
    assert isinstance(msg, Hello) and msg.info["fw"] == "1.29.0" and msg.info["mode"] is None


def test_scan_mode_emits_parseable_scans(rig):
    agent, wlan, clock, _, messages = rig
    agent.handle_line(json.dumps({"cmd": "scan", "scan_interval": 10}))
    agent.tick()
    msgs = messages()
    started, scan, status = msgs
    assert isinstance(started, Event) and started.detail["mode"] == "scan"
    assert isinstance(scan, Scan) and scan.number == 1
    assert scan.aps[0].ssid == "VIKK" and scan.aps[0].bssid == "02:00:00:aa:00:40"
    assert scan.aps[1].ssid == "??bad" and scan.aps[1].hidden and scan.aps[1].band == "5 GHz"
    assert isinstance(status, Status) and status.mode == "scan"
    assert wlan.connect_calls == []

    clock.now = 5000
    agent.tick()
    assert not any(isinstance(m, Scan) for m in messages())  # not yet due
    clock.now = 10000
    agent.tick()
    assert any(isinstance(m, Scan) for m in messages())


def test_connect_mode_reports_link_and_events(rig):
    agent, wlan, clock, pings, messages = rig
    agent.handle_line(json.dumps({"cmd": "connect", "ssid": "Home", "password": "pw", "link_interval": 1}))
    assert wlan.connect_calls == [("Home", "pw")]
    agent.tick()
    msgs = messages()
    link = next(m for m in msgs if isinstance(m, Link))
    assert link.connected is False and link.latency_ms is None

    wlan.connected = True
    for t in range(1000, 5000, 1000):
        clock.now = t
        agent.tick()
    msgs = messages()
    connected = next(m for m in msgs if isinstance(m, Event) and m.kind == "connected")
    assert connected.detail == {"ip": "192.168.1.57", "channel": 6, "bssid": "aa:bb:cc:dd:ee:ff",
                                "gateway": "192.168.1.1"}
    links = [m for m in msgs if isinstance(m, Link)]
    assert len(links) == 4
    assert [l.ok for l in links] == [True, True, True, False]
    assert links[0].rssi == -48 and links[0].target == "192.168.1.1"
    assert [p[1] for p in pings] == [1, 2, 3, 4]


def test_disconnect_triggers_event_and_reconnect(rig):
    agent, wlan, clock, _, messages = rig
    agent.handle_line(json.dumps({"cmd": "connect", "ssid": "Home", "password": "pw", "target": "1.1.1.1"}))
    wlan.connected = True
    agent.tick()
    wlan.connected = False
    wlan.status_code = 200
    clock.now = 1000
    agent.tick()
    msgs = messages()
    lost = next(m for m in msgs if isinstance(m, Event) and m.kind == "disconnected")
    assert lost.detail["status"] == 200
    clock.now = 1000 + agent_mod.RECONNECT_BACKOFF_MS
    agent.tick()
    assert len(wlan.connect_calls) == 2


def test_connect_timeout_retries(rig):
    agent, wlan, clock, _, messages = rig
    agent.handle_line(json.dumps({"cmd": "connect", "ssid": "Home", "password": "bad"}))
    wlan.status_code = 202
    clock.now = agent_mod.CONNECT_TIMEOUT_MS + 1000
    agent.tick()
    msgs = messages()
    timeout = next(m for m in msgs if isinstance(m, Event) and m.kind == "connect_timeout")
    assert timeout.detail["status"] == 202
    clock.now += agent_mod.RECONNECT_BACKOFF_MS
    agent.tick()
    assert len(wlan.connect_calls) == 2


def test_both_mode_delays_first_scan(rig):
    agent, wlan, clock, _, messages = rig
    agent.handle_line(json.dumps({"cmd": "both", "ssid": "Home", "scan_interval": 30}))
    agent.tick()
    assert not any(isinstance(m, Scan) for m in messages())
    clock.now = 30000
    agent.tick()
    assert any(isinstance(m, Scan) for m in messages())


def test_stop_clears_password_and_disconnects(rig):
    agent, wlan, _, _, messages = rig
    agent.handle_line(json.dumps({"cmd": "connect", "ssid": "Home", "password": "pw"}))
    wlan.connected = True
    agent.handle_line('{"cmd":"stop"}')
    assert agent.password is None and agent.mode is None and wlan.connected is False
    assert any(isinstance(m, Event) and m.kind == "stopped" for m in messages())


@pytest.mark.parametrize("line,error", [
    ("not json", "invalid JSON"),
    ("[1,2]", "must be an object"),
    ('{"cmd":"dance"}', "unknown command"),
    ('{"cmd":"connect"}', "ssid is required"),
])
def test_bad_commands_report_errors(rig, line, error):
    agent, _, _, _, messages = rig
    agent.handle_line(line)
    [msg] = messages()
    assert error in msg.message


def test_intervals_are_clamped(rig):
    agent, _, _, _, _ = rig
    agent.handle_line(json.dumps({"cmd": "scan", "scan_interval": 0.01}))
    assert agent.scan_interval_ms == 1000
    agent.handle_line(json.dumps({"cmd": "connect", "ssid": "x", "link_interval": "fast"}))
    assert agent.link_interval_ms == 1000


def test_icmp_checksum():
    # Echo request, id 1, seq 1, no payload: checksum 0xf7fd.
    assert agent_mod._checksum(b"\x08\x00\x00\x00\x00\x01\x00\x01") == 0xF7FD
    assert agent_mod._checksum(b"\x08\x00\xf7\xfd\x00\x01\x00\x01") == 0
