"""Session control: connects the device to storage and pushes live updates to subscribers."""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Any, Callable

from .config import Settings
from .device import Device
from .protocol import (MODES, DeviceError, Event, Hello, Link, Scan, Status, start_command,
                       stop_command)
from .storage import Storage

log = logging.getLogger(__name__)

# MicroPython network.STAT_* values (read from the board, firmware v1.29.0).
WLAN_STATUS = {
    200: "beacon timeout",
    201: "network not found",
    202: "wrong password",
    203: "association failed",
    204: "handshake timeout",
    210: "no network with compatible security",
    211: "no network in auth-mode threshold",
    212: "no network in RSSI threshold",
    1000: "idle",
    1001: "connecting",
    1010: "got IP",
}

DEFAULT_SCAN_INTERVAL = {"scan": 15.0, "connect": 15.0, "both": 60.0}
DEFAULT_LINK_INTERVAL = 1.0
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:]{1,253}$")
# A dual-band scan blocks the board for ~10 s, so a status line sent just before a
# command was processed can still say "idle". Wait this long before trusting it.
RESEND_COOLDOWN = 15.0


class ValidationError(ValueError):
    pass


def _number(value: Any, name: str, low: float, high: float, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a number") from exc
    if not low <= number <= high:
        raise ValidationError(f"{name} must be between {low:g} and {high:g}")
    return number


def describe_event(kind: str, detail: dict[str, Any]) -> tuple[str, str]:
    """Human-readable (level, message) for a board event."""
    status = detail.get("status")
    reason = WLAN_STATUS.get(status, f"status {status}") if status is not None else None
    if kind == "connecting":
        return "info", f"Connecting to {detail.get('ssid')}"
    if kind == "connected":
        parts = [f"IP {detail['ip']}" if detail.get("ip") else None,
                 f"channel {detail['channel']}" if detail.get("channel") else None,
                 f"AP {detail['bssid']}" if detail.get("bssid") else None,
                 f"gateway {detail['gateway']}" if detail.get("gateway") else None]
        return "success", "Connected (" + ", ".join(p for p in parts if p) + ")"
    if kind == "disconnected":
        return "warning", "Connection lost" + (f": {reason}" if reason else "")
    if kind == "connect_timeout":
        return "warning", "Connection attempt timed out" + (f": {reason}" if reason else "")
    if kind == "connect_error":
        return "error", f"Connection error: {detail.get('message')}"
    if kind == "started":
        return "info", f"Board started {detail.get('mode')} mode"
    if kind == "stopped":
        return "info", "Board stopped measuring"
    return "info", kind.replace("_", " ")


class Monitor:
    def __init__(self, storage: Storage, device: Device, settings: Settings,
                 clock: Callable[[], float] = time.time) -> None:
        self.storage = storage
        self.device = device
        self.settings = settings
        self.clock = clock
        self.session: dict[str, Any] | None = None
        self.last_status: dict[str, Any] | None = None
        self.last_scan: dict[str, Any] | None = None
        self._command: dict[str, Any] | None = None  # holds the password in memory only
        self._last_resend = 0.0
        self._lock = threading.RLock()
        self._subscribers: list[queue.Queue] = []
        device.on_message = self._on_message
        device.on_state = self._on_state

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        closed = self.storage.close_open_sessions(self.clock())
        if closed:
            log.info("closed %d session(s) left open by a previous run", closed)
        self.device.start()

    def shutdown(self) -> None:
        self.stop_session()
        self.device.stop()

    # -- sessions ---------------------------------------------------------

    def start_session(self, mode: str, *, scan_interval: Any = None, link_interval: Any = None,
                      ssid: str | None = None, password: str | None = None,
                      target: str | None = None, note: str | None = None) -> dict[str, Any]:
        if mode not in MODES:
            raise ValidationError(f"mode must be one of: {', '.join(MODES)}")
        scan_iv = _number(scan_interval, "scan_interval", 1, 3600, DEFAULT_SCAN_INTERVAL[mode])
        link_iv = _number(link_interval, "link_interval", 0.2, 60, DEFAULT_LINK_INTERVAL)
        note = (note or "").strip()[:200] or None

        if mode in ("connect", "both"):
            ssid = (ssid or "").strip() or self.settings.wifi_ssid
            if not ssid:
                raise ValidationError("ssid is required for connection modes (or set WIFI_SSID in .env)")
            if len(ssid.encode("utf-8")) > 32:
                raise ValidationError("ssid is longer than 32 bytes")
            if password is None or password == "":
                password = self.settings.wifi_password if ssid == self.settings.wifi_ssid else (password or "")
            if len(password) > 64:
                raise ValidationError("password is longer than 64 characters")
            target = (target or "").strip() or self.settings.ping_target or None
            if target and not HOST_RE.match(target):
                raise ValidationError("target must be a hostname or IP address")
        else:
            ssid = password = target = None

        command = start_command(mode, scan_interval=scan_iv, link_interval=link_iv,
                                ssid=ssid, password=password, target=target)
        now = self.clock()
        with self._lock:
            if self.session:
                self._end_session_locked(now)
            session_id = self.storage.create_session(
                mode=mode, started_at=now, ssid=ssid, ping_target=target,
                scan_interval=scan_iv, link_interval=link_iv, note=note)
            self.session = self.storage.get_session(session_id)
            self._command = command
            self._last_resend = now
            self.last_scan = None
            session = dict(self.session)
        what = f"{mode} mode" + (f" on {ssid}" if ssid else "")
        self._log(session_id, "session_started", f"Session started: {what}")
        if not self.device.send(command):
            self._log(session_id, "device_offline", "Board is offline; measuring starts when it connects",
                      level="warning")
        self._publish("session", session)
        return session

    def stop_session(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.session:
                return None
            session = self._end_session_locked(self.clock())
        self.device.send(stop_command())
        self._publish("session", session)
        return session

    def _end_session_locked(self, now: float) -> dict[str, Any]:
        session_id = self.session["id"]
        self.storage.end_session(session_id, now)
        self._log(session_id, "session_stopped", "Session stopped")
        self.session = None
        self._command = None
        return self.storage.get_session(session_id)

    def current_session_id(self) -> int | None:
        with self._lock:
            return self.session["id"] if self.session else None

    # -- device callbacks -------------------------------------------------

    def _resend(self, reason: str, *, force: bool = False) -> None:
        with self._lock:
            command = self._command
            session_id = self.session["id"] if self.session else None
            now = self.clock()
            if not command or (not force and now - self._last_resend < RESEND_COOLDOWN):
                return
            self._last_resend = now
        log.info("resending session command: %s", reason)
        self._log(session_id, "session_resumed", f"Resuming measurement on the board ({reason})", level="warning")
        self.device.send(command)

    def _on_state(self, connected: bool, state: dict[str, Any]) -> None:
        session_id = self.current_session_id()
        if connected:
            self._log(session_id, "device_connected", f"Board connected on {state.get('port')}", level="success")
            with self._lock:
                command = self._command
                self._last_resend = self.clock()
            self.device.send(command or stop_command())
        else:
            self._log(session_id, "device_disconnected", f"Board disconnected ({state.get('detail')})",
                      level="warning")
        self._publish("status", self.status())

    def _on_message(self, msg: Any) -> None:
        now = self.clock()
        with self._lock:
            session = dict(self.session) if self.session else None
        session_id = session["id"] if session else None

        if isinstance(msg, Hello):
            if session and msg.info.get("mode") != session["mode"]:
                # A hello comes from a freshly started agent, so resend right away.
                self._resend("board restarted", force=True)
            self._publish("status", self.status())
        elif isinstance(msg, Status):
            self.last_status = {"mode": msg.mode, "connected": msg.connected, "mem": msg.mem, "ts": now}
            if session and msg.mode is None:
                self._resend("board was idle")
            self._publish("status", self.status())
        elif isinstance(msg, Scan):
            if not session or session["mode"] not in ("scan", "both"):
                return
            scan_id = self.storage.add_scan(session_id, now, msg)
            self.last_scan = {"id": scan_id, "ts": now, "count": len(msg.aps), "duration_ms": msg.duration_ms}
            self._publish("scan", {"session_id": session_id, **self.last_scan})
        elif isinstance(msg, Link):
            if not session or session["mode"] not in ("connect", "both"):
                return
            self.storage.add_link(session_id, now, msg)
            self._publish("link", {"session_id": session_id, "ts": now, "connected": msg.connected,
                                   "rssi": msg.rssi, "latency_ms": msg.latency_ms, "ok": msg.ok})
        elif isinstance(msg, Event):
            level, message = describe_event(msg.kind, msg.detail)
            self._log(session_id, msg.kind, message, level=level, detail=msg.detail)
        elif isinstance(msg, DeviceError):
            self._log(session_id, "board_error", msg.message, level="error")

    # -- status and events ------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            session = dict(self.session) if self.session else None
        return {
            "device": {"kind": self.device.kind, "connected": self.device.connected,
                       "state": self.device.state, "info": self.device.info,
                       "last_status": self.last_status},
            "session": session,
            "last_scan": self.last_scan,
            "environment": self.settings.environment,
            "defaults": {"ssid": self.settings.wifi_ssid,
                         "has_password": bool(self.settings.wifi_password),
                         "ping_target": self.settings.ping_target,
                         "scan_interval": DEFAULT_SCAN_INTERVAL,
                         "link_interval": DEFAULT_LINK_INTERVAL},
            "server_time": self.clock(),
        }

    def _log(self, session_id: int | None, kind: str, message: str, *, level: str = "info",
             detail: dict[str, Any] | None = None) -> None:
        event = self.storage.add_event(session_id, self.clock(), kind, message, level=level, detail=detail)
        self._publish("event", event)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _publish(self, kind: str, data: Any) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait((kind, data))
            except queue.Full:
                self.unsubscribe(q)
