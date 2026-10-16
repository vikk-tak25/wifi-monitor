from __future__ import annotations

import time
from typing import Any

import pytest

from wifimon.config import Settings
from wifimon.device import Device
from wifimon.protocol import AccessPoint, Scan
from wifimon.storage import Storage


class FakeDevice(Device):
    """Records commands and lets tests inject board messages synchronously."""

    kind = "fake"

    def __init__(self, online: bool = True) -> None:
        super().__init__()
        self.online = online
        self.sent: list[dict[str, Any]] = []

    def start(self) -> None:
        if self.online:
            self._set_state(True, port="/dev/fake", detail="connected")

    def stop(self) -> None:
        self._set_state(False, port="/dev/fake", detail="stopped")

    def send(self, cmd: dict[str, Any]) -> bool:
        if not self.connected:
            return False
        self.sent.append(cmd)
        return True

    def deliver(self, msg: Any) -> None:
        self._emit(msg)

    def plug(self) -> None:
        self._set_state(True, port="/dev/fake", detail="connected")

    def unplug(self) -> None:
        self._set_state(False, port="/dev/fake", detail="disconnected")


class Clock:
    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def storage(tmp_path):
    db = Storage(tmp_path / "test.db")
    yield db
    db.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "test.db", wifi_ssid="HomeNet", wifi_password="env-secret")


@pytest.fixture
def clock():
    return Clock()


def make_scan(*aps: tuple[str, str, int, int], number: int | None = None) -> Scan:
    return Scan(number=number, duration_ms=9700,
                aps=[AccessPoint(ssid=s, bssid=b, channel=c, rssi=r, auth=3, hidden=False) for s, b, c, r in aps])


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
