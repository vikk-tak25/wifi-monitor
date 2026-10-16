"""JSON-lines protocol between the Mac and the board agent (device/agent.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ESP-IDF wifi_auth_mode_t values, as returned by MicroPython's WLAN.scan().
AUTH_MODES = {
    0: "Open",
    1: "WEP",
    2: "WPA",
    3: "WPA2",
    4: "WPA/WPA2",
    5: "WPA2-Enterprise",
    6: "WPA3",
    7: "WPA2/WPA3",
    8: "WAPI",
    9: "OWE",
    10: "WPA3-Enterprise-192",
    11: "WPA3-Ext",
    12: "WPA3-Ext mixed",
    13: "DPP",
    14: "WPA3-Enterprise",
    15: "WPA2/WPA3-Enterprise",
    16: "WPA-Enterprise",
}

MODES = ("scan", "connect", "both")


class ProtocolError(ValueError):
    pass


def auth_name(code: int | None) -> str:
    if code is None:
        return "?"
    return AUTH_MODES.get(code, f"auth {code}")


def band_for_channel(channel: int | None) -> str:
    if channel is None:
        return "?"
    if 1 <= channel <= 14:
        return "2.4 GHz"
    if 32 <= channel <= 177:
        return "5 GHz"
    return "?"


@dataclass(frozen=True)
class AccessPoint:
    ssid: str
    bssid: str
    channel: int
    rssi: int
    auth: int
    hidden: bool

    @property
    def band(self) -> str:
        return band_for_channel(self.channel)


@dataclass(frozen=True)
class Hello:
    info: dict[str, Any]


@dataclass(frozen=True)
class Status:
    mode: str | None
    connected: bool
    mem: int | None = None


@dataclass(frozen=True)
class Scan:
    number: int | None
    duration_ms: int | None
    aps: list[AccessPoint] = field(default_factory=list)


@dataclass(frozen=True)
class Link:
    connected: bool
    rssi: int | None
    latency_ms: float | None
    ok: bool
    target: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Event:
    kind: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class DeviceError:
    message: str


Message = Hello | Status | Scan | Link | Event | DeviceError


def _int(value: Any, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{name} must be a number, got {value!r}")
    return int(value)


def _float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{name} must be a number, got {value!r}")
    return float(value)


def _ap(raw: Any) -> AccessPoint:
    if not isinstance(raw, list) or len(raw) != 6:
        raise ProtocolError(f"access point must be a 6-item list, got {raw!r}")
    ssid, bssid, channel, rssi, auth, hidden = raw
    if not isinstance(ssid, str) or not isinstance(bssid, str):
        raise ProtocolError("ssid and bssid must be strings")
    return AccessPoint(
        ssid=ssid,
        bssid=bssid.lower(),
        channel=_int(channel, "channel"),
        rssi=_int(rssi, "rssi"),
        auth=_int(auth, "auth"),
        hidden=bool(hidden),
    )


def parse_message(obj: dict[str, Any]) -> Message:
    kind = obj.get("t")
    if kind == "hello":
        return Hello(info={k: v for k, v in obj.items() if k != "t"})
    if kind == "status":
        return Status(mode=obj.get("mode"), connected=bool(obj.get("connected")),
                      mem=_int(obj.get("mem"), "mem", optional=True))
    if kind == "scan":
        aps = obj.get("aps")
        if not isinstance(aps, list):
            raise ProtocolError("scan.aps must be a list")
        return Scan(number=_int(obj.get("n"), "n", optional=True),
                    duration_ms=_int(obj.get("dur"), "dur", optional=True),
                    aps=[_ap(a) for a in aps])
    if kind == "link":
        connected = bool(obj.get("connected"))
        latency = _float(obj.get("lat"), "lat")
        return Link(connected=connected,
                    rssi=_int(obj.get("rssi"), "rssi", optional=True),
                    latency_ms=latency,
                    ok=bool(obj.get("ok")) and latency is not None,
                    target=obj.get("target"),
                    error=obj.get("err"))
    if kind == "event":
        name = obj.get("kind")
        if not isinstance(name, str) or not name:
            raise ProtocolError("event.kind must be a non-empty string")
        return Event(kind=name, detail={k: v for k, v in obj.items() if k not in ("t", "kind")})
    if kind == "error":
        return DeviceError(message=str(obj.get("message", "unknown error")))
    raise ProtocolError(f"unknown message type {kind!r}")


def parse_line(line: str | bytes) -> Message | None:
    """Parse one line from the board.

    Returns None for lines that are not protocol messages (boot logs, REPL echo).
    Raises ProtocolError for JSON objects that look like messages but are malformed.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "t" not in obj:
        return None
    return parse_message(obj)


# -- commands -------------------------------------------------------------

def encode_command(cmd: dict[str, Any]) -> bytes:
    return (json.dumps(cmd, separators=(",", ":")) + "\n").encode("utf-8")


def hello_command() -> dict[str, Any]:
    return {"cmd": "hello"}


def stop_command() -> dict[str, Any]:
    return {"cmd": "stop"}


def start_command(mode: str, *, scan_interval: float, link_interval: float,
                  ssid: str | None = None, password: str | None = None,
                  target: str | None = None) -> dict[str, Any]:
    if mode not in MODES:
        raise ProtocolError(f"mode must be one of {MODES}")
    cmd: dict[str, Any] = {"cmd": mode, "scan_interval": scan_interval, "link_interval": link_interval}
    if mode in ("connect", "both"):
        if not ssid:
            raise ProtocolError("ssid is required for connection modes")
        cmd["ssid"] = ssid
        cmd["password"] = password or ""
        if target:
            cmd["target"] = target
    return cmd
