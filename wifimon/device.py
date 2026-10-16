"""Device backends: the real board over USB serial, and a simulator for development and tests."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any, Callable

from .protocol import Hello, Message, ProtocolError, encode_command, parse_line

log = logging.getLogger(__name__)

ESPRESSIF_VID = 0x303A

MessageHandler = Callable[[Message], None]
StateHandler = Callable[[bool, dict[str, Any]], None]


class Device:
    """Common interface. Callbacks run on the device's background thread."""

    kind = "base"

    def __init__(self) -> None:
        self.on_message: MessageHandler | None = None
        self.on_state: StateHandler | None = None
        self.connected = False
        self.state: dict[str, Any] = {"detail": "not started"}
        self.info: dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"{self.kind}-device", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def send(self, cmd: dict[str, Any]) -> bool:
        raise NotImplementedError

    def _run(self) -> None:
        raise NotImplementedError

    def _emit(self, msg: Message) -> None:
        if self.on_message:
            try:
                self.on_message(msg)
            except Exception:
                log.exception("message handler failed")

    def _set_state(self, connected: bool, **state: Any) -> None:
        changed = connected != self.connected or state != self.state
        self.connected = connected
        self.state = state
        if changed and self.on_state:
            try:
                self.on_state(connected, dict(state))
            except Exception:
                log.exception("state handler failed")


# -- serial ---------------------------------------------------------------

def find_espressif_port() -> str | None:
    from serial.tools import list_ports

    ports = [p for p in list_ports.comports() if p.vid == ESPRESSIF_VID]
    # On macOS prefer the call-out device (cu.*) over the dial-in one (tty.*).
    ports.sort(key=lambda p: (not p.device.startswith("/dev/cu."), p.device))
    return ports[0].device if ports else None


class SerialDevice(Device):
    """The ESP32 board running device/agent.py.

    Reconnects automatically when the board is unplugged. If the board does not answer
    (for example it sits in the MicroPython REPL), it interrupts it and starts the agent.
    """

    kind = "serial"

    def __init__(self, port: str | None = None, *, baudrate: int = 115200,
                 reconnect_delay: float = 1.0, handshake_timeout: float = 3.0,
                 scan_grace: float = 12.0,
                 serial_factory: Callable[[], Any] | None = None,
                 port_finder: Callable[[], str | None] = find_espressif_port) -> None:
        super().__init__()
        self.preferred_port = port
        self.baudrate = baudrate
        self.reconnect_delay = reconnect_delay
        self.handshake_timeout = handshake_timeout
        self.scan_grace = scan_grace
        self.serial_factory = serial_factory
        self.port_finder = port_finder
        self._ser: Any = None
        self._write_lock = threading.Lock()
        self._alive = False
        self._stage = 0
        self._deadline = 0.0

    def send(self, cmd: dict[str, Any]) -> bool:
        ser = self._ser
        if ser is None:
            return False
        try:
            with self._write_lock:
                ser.write(encode_command(cmd))
                ser.flush()
            return True
        except Exception as exc:
            log.warning("serial write failed: %s", exc)
            return False

    def _open(self, port: str) -> Any:
        if self.serial_factory:
            ser = self.serial_factory()
        else:
            import serial

            ser = serial.Serial()
        ser.port = port
        ser.baudrate = self.baudrate
        ser.timeout = 0.2
        ser.open()
        return ser

    def _run(self) -> None:
        while not self._stop.is_set():
            port = self.preferred_port or self.port_finder()
            if not port:
                self._set_state(False, detail="no Espressif board found")
                self._stop.wait(self.reconnect_delay)
                continue
            try:
                ser = self._open(port)
            except Exception as exc:
                self._set_state(False, port=port, detail=f"cannot open port: {exc}")
                self._stop.wait(self.reconnect_delay)
                continue
            self._ser = ser
            self._alive = False
            self._stage = 0
            self._deadline = time.monotonic() + self.handshake_timeout
            self._set_state(True, port=port, detail="connected")
            # Ask for the board's identity even if the agent is already running.
            self.send({"cmd": "hello"})
            try:
                self._read_loop(ser)
            except Exception as exc:
                log.info("serial connection lost: %s", exc)
            finally:
                self._ser = None
                try:
                    ser.close()
                except Exception:
                    pass
            self._set_state(False, port=port, detail="disconnected")
            self._stop.wait(self.reconnect_delay)

    def _read_loop(self, ser: Any) -> None:
        buf = b""
        while not self._stop.is_set():
            chunk = ser.read(max(1, getattr(ser, "in_waiting", 0) or 0))
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle_line(line)
                if len(buf) > 65536:
                    buf = b""
            self._handshake()

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = parse_line(line)
        except ProtocolError as exc:
            log.warning("bad message from board: %s", exc)
            return
        if msg is None:
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                log.debug("board: %s", text)
            return
        self._alive = True
        if isinstance(msg, Hello):
            self.info = msg.info
        self._emit(msg)

    def _handshake(self) -> None:
        """Make sure the agent is running: ask for hello, then restart it from the REPL."""
        if self._alive or time.monotonic() < self._deadline:
            return
        if self._stage == 0:
            self.send({"cmd": "hello"})
            self._stage = 1
            # A dual-band scan blocks the agent for about 10 s, so wait past one.
            self._deadline = time.monotonic() + self.scan_grace
        elif self._stage == 1:
            log.info("board not answering, starting the agent from the REPL")
            ser = self._ser
            if ser is not None:
                with self._write_lock:
                    ser.write(b"\x03\x03")
                    ser.flush()
                    time.sleep(0.2)
                    ser.write(b"\r\nimport agent; agent.run()\r\n")
                    ser.flush()
            self._stage = 2
            self._deadline = time.monotonic() + self.handshake_timeout * 2
        elif self._stage == 2:
            self._set_state(True, port=self.state.get("port"),
                            detail="board is not answering; is the agent installed? (wifimon install-agent)")
            self._stage = 0
            self._deadline = time.monotonic() + 30


# -- simulator ------------------------------------------------------------

SIM_NETWORKS = [
    # ssid, bssid, channel, base rssi, auth, presence probability, noise (dB)
    ("HomeNet", "02:00:00:00:00:01", 6, -42, 3, 1.0, 1.5),
    ("HomeNet", "02:00:00:00:00:02", 36, -51, 3, 1.0, 2.0),
    ("HomeNet-Guest", "02:00:00:00:00:03", 6, -43, 7, 1.0, 1.5),
    ("Neighbour_2G", "02:00:00:00:01:01", 1, -67, 3, 0.95, 3.0),
    ("Neighbour_5G", "02:00:00:00:01:02", 44, -74, 3, 0.85, 3.5),
    ("CoffeeShop", "02:00:00:00:02:01", 11, -79, 0, 0.6, 4.0),
    ("Office-WiFi", "02:00:00:00:03:01", 149, -70, 7, 0.9, 2.5),
    ("Office-WiFi", "02:00:00:00:03:02", 1, -76, 7, 0.8, 3.0),
    ("DIRECT-Printer", "02:00:00:00:04:01", 6, -82, 3, 0.5, 4.5),
    ("", "02:00:00:00:05:01", 100, -85, 3, 0.4, 3.0),
    ("IoT-Hub", "02:00:00:00:06:01", 13, -60, 6, 0.98, 6.0),
    ("Mesh-Node", "02:00:00:00:07:01", 52, -64, 7, 0.97, 2.0),
]


class SimulatedDevice(Device):
    """Generates realistic-looking measurements without hardware.

    `speed` divides all intervals, so tests can run a session in a fraction of a second.
    Messages go through the same JSON parsing as real board output.
    """

    kind = "simulated"

    def __init__(self, *, seed: int | None = None, speed: float = 1.0, scan_duration: float = 0.0) -> None:
        super().__init__()
        self.rng = random.Random(seed)
        self.speed = speed
        self.scan_duration = scan_duration
        self._lock = threading.Lock()
        self._commands: list[dict[str, Any]] = []
        self.mode: str | None = None
        self.scan_interval = 15.0
        self.link_interval = 1.0
        self.ssid: str | None = None
        self.target: str | None = None
        self.drift = {n[1]: 0.0 for n in SIM_NETWORKS}
        self.scan_count = 0
        self.link_up = False
        self.outage_until = 0.0
        self.info = {"agent": "1", "fw": "simulated", "machine": "Simulated ESP32-C5", "mac": "02:00:00:00:00:00"}

    def send(self, cmd: dict[str, Any]) -> bool:
        if not self.connected:
            return False
        with self._lock:
            self._commands.append(dict(cmd))
        return True

    def _line(self, obj: dict[str, Any]) -> None:
        msg = parse_line(json.dumps(obj))
        if msg is not None:
            self._emit(msg)

    def _event(self, kind: str, **detail: Any) -> None:
        self._line({"t": "event", "kind": kind, **detail})

    def _handle(self, cmd: dict[str, Any], now: float) -> None:
        name = cmd.get("cmd")
        if name == "hello":
            self._line({"t": "hello", "mode": self.mode, **self.info})
        elif name == "stop":
            self.mode = None
            self.link_up = False
            self._event("stopped")
        elif name in ("scan", "connect", "both"):
            if name != "scan" and not cmd.get("ssid"):
                self._line({"t": "error", "message": f"ssid is required for mode {name}"})
                return
            self.mode = name
            self.scan_interval = float(cmd.get("scan_interval") or 15)
            self.link_interval = float(cmd.get("link_interval") or 1)
            self.ssid = cmd.get("ssid")
            self.target = cmd.get("target")
            self.link_up = False
            self.outage_until = 0.0
            self._next_scan = now if name == "scan" else now + self.scan_interval / self.speed
            self._next_link = now
            self._event("started", mode=name, ssid=self.ssid,
                        scan_interval=self.scan_interval, link_interval=self.link_interval)
            if name != "scan":
                self._event("connecting", ssid=self.ssid)
        else:
            self._line({"t": "error", "message": f"unknown command: {name}"})

    def _scan(self) -> None:
        aps = []
        for ssid, bssid, channel, base, auth, presence, noise in SIM_NETWORKS:
            self.drift[bssid] = max(-6.0, min(6.0, self.drift[bssid] + self.rng.gauss(0, 0.6)))
            if self.rng.random() > presence:
                continue
            rssi = round(base + self.drift[bssid] + self.rng.gauss(0, noise))
            aps.append([ssid, bssid, channel, max(-100, min(-20, rssi)), auth, ssid == ""])
        if self.scan_duration:
            time.sleep(self.scan_duration / self.speed)
        self.scan_count += 1
        self._line({"t": "scan", "n": self.scan_count, "dur": 9700, "aps": aps})

    def _link(self, now: float) -> None:
        if not self.link_up and now >= self.outage_until:
            self.link_up = True
            self._event("connected", ip="192.168.1.57", channel=6, bssid="02:00:00:00:00:01",
                        gateway="192.168.1.1")
        elif self.link_up and self.rng.random() < 0.004:
            self.link_up = False
            self.outage_until = now + self.rng.uniform(3, 8) / self.speed
            self._event("disconnected", status=200)
        if not self.link_up:
            self._line({"t": "link", "connected": False, "rssi": None, "lat": None, "ok": False})
            return
        rssi = round(-48 + self.drift["02:00:00:00:00:01"] + self.rng.gauss(0, 2))
        lost = self.rng.random() < 0.015
        latency = None if lost else round(self.rng.lognormvariate(1.2, 0.45) + (30 if self.rng.random() < 0.02 else 0), 2)
        self._line({"t": "link", "connected": True, "rssi": rssi, "lat": latency, "ok": not lost,
                    "target": self.target or "192.168.1.1"})

    def _run(self) -> None:
        self._set_state(True, port="simulated", detail="simulated device")
        self._line({"t": "hello", "mode": None, **self.info})
        self._next_scan = self._next_link = time.monotonic()
        next_status = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                commands, self._commands = self._commands, []
            for cmd in commands:
                self._handle(cmd, now)
            if self.mode in ("connect", "both") and now >= self._next_link:
                self._next_link = now + self.link_interval / self.speed
                self._link(now)
            if self.mode in ("scan", "both") and now >= self._next_scan:
                self._next_scan = now + self.scan_interval / self.speed
                self._scan()
            if now >= next_status:
                next_status = now + 5 / self.speed
                self._line({"t": "status", "mode": self.mode,
                            "connected": self.link_up and self.mode in ("connect", "both")})
            self._stop.wait(min(0.02, 0.2 / self.speed))
        self._set_state(False, port="simulated", detail="stopped")
