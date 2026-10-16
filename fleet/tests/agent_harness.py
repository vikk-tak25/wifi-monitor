"""Runs the real unit agent (unit/app.py) under CPython against the real server (TestClient),
with a fake WLAN and a shared simulated clock."""

from __future__ import annotations

import importlib.util
import itertools
from datetime import timedelta
from pathlib import Path

UNIT_DIR = Path(__file__).resolve().parents[2] / "unit"
_counter = itertools.count()


def load_module(path: Path, name: str | None = None):
    spec = importlib.util.spec_from_file_location(name or f"unit_under_test_{next(_counter)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bssid(text: str) -> bytes:
    return bytes(int(p, 16) for p in text.split(":"))


AP_NEAR = ("02:00:00:aa:00:40", 9, -38)
AP_FAR = ("02:00:00:bb:00:00", 5, -70)
AP_OTHER = ("02:00:00:aa:01:70", 132, -55)


class FakeWLAN:
    def __init__(self, ssid="VIKK"):
        self.ssid = ssid
        self.aps = {AP_NEAR[0]: AP_NEAR, AP_FAR[0]: AP_FAR}
        self.other = [(b"Neighbour", bssid("02:00:00:00:00:01"), 1, -80, 3, False)]
        self.connect_calls = []
        self.connected = False
        self.auto_connect = True
        self.status_code = 1001
        self.rssi = -45
        self.scan_count = 0
        self.hidden = False  # broadcast our network without a name
        self.channel = None

    def scan(self):
        self.scan_count += 1
        name = b"" if self.hidden else self.ssid.encode()
        ours = [(name, bssid(b), ch, rssi, 3, False) for b, ch, rssi in self.aps.values()]
        return ours + self.other

    def connect(self, ssid, key, bssid=None):
        self.connect_calls.append((ssid, key, bssid))
        self.connected = self.auto_connect

    def config(self, key):
        if key == "channel":
            return self.channel
        raise ValueError("unknown config param")

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
        return "10.0.5.1"

    def ifconfig(self):
        return ("10.0.5.23", "255.255.255.0", "10.0.5.1", "10.0.0.2")


class Ticks:
    """Agent ticks_ms; advancing it also advances the server clock so both agree."""

    def __init__(self, server_clock):
        self.now = 1_000_000
        self.server_clock = server_clock

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms
        self.server_clock.advance(milliseconds=ms)


class Reset(Exception):
    pass


class FakeHardware:
    def __init__(self):
        self.feeds = 0
        self.resets = 0

    def feed(self):
        self.feeds += 1

    def reset(self):
        self.resets += 1
        raise Reset()


class ServerLink:
    """http() for the agent, backed by the FastAPI TestClient. Can simulate an outage."""

    def __init__(self, client, base="http://fleet.test"):
        self.client = client
        self.base = base
        self.down = False
        self.tamper = False  # simulate a man in the middle altering responses
        self.calls = []
        self.sent = []

    def __call__(self, method, url, body, headers, timeout_s):
        assert url.startswith(self.base), url
        path = url[len(self.base):]
        self.calls.append((method, path))
        self.sent.append((dict(headers), body))
        if self.down:
            raise OSError("ECONNREFUSED")
        res = self.client.request(method, path, content=body, headers=dict(headers))
        reply_headers = dict(res.headers)
        if self.tamper:
            reply_headers["x-signature"] = "0" * 64
        return res.status_code, res.content, reply_headers


class Rig:
    def __init__(self, app_module, client, clock, key, *, ssid="VIKK", ping=None):
        self.app = app_module
        self.wlan = FakeWLAN(ssid)
        self.ticks = Ticks(clock)
        self.link = ServerLink(client)
        self.hw = FakeHardware()
        self.pings = []

        def default_ping(host, timeout_ms):
            self.pings.append(host)
            return 3.5

        config = {"wifi_ssid": ssid, "wifi_password": "secret-pw", "server_url": self.link.base, "unit_key": key}
        self.agent = app_module.Agent(config, self.wlan, self.link, ping or default_ping, self.hw, ticks=self.ticks,
                                      info={"fw_version": "1.29.0", "chip": "ESP32-C5", "mac": "02:00:00:c5:00:01"})

    def run(self, seconds, step_ms=500):
        """Advance simulated time, calling agent.step() every step_ms."""
        end = self.ticks.now + int(seconds * 1000)
        while self.ticks.now < end:
            self.agent.step()
            self.ticks.advance(step_ms)

    def elapsed(self) -> timedelta:
        return timedelta(milliseconds=self.ticks.now - 1_000_000)
