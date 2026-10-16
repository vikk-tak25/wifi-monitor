# Wi-Fi monitor agent for MicroPython (ESP32-C5).
#
# Speaks a JSON-lines protocol over USB serial:
#   commands in  (stdin):  {"cmd": "hello" | "scan" | "connect" | "both" | "stop", ...}
#   messages out (stdout): {"t": "hello" | "status" | "scan" | "link" | "event" | "error", ...}
#
# The Agent class has no direct hardware access: the WLAN object, clock, ping
# function and output stream are injected, so the logic also runs under CPython
# in the test suite.

import json
import sys
import time

try:
    import gc
except ImportError:  # pragma: no cover
    gc = None

AGENT_VERSION = "1"

MODES = ("scan", "connect", "both")
CONNECT_TIMEOUT_MS = 20000
RECONNECT_BACKOFF_MS = 5000
STATUS_INTERVAL_MS = 5000
PING_TIMEOUT_MS = 1000

try:
    ticks_ms = time.ticks_ms
    ticks_diff = time.ticks_diff
    ticks_add = time.ticks_add
except AttributeError:  # CPython
    def ticks_ms():
        return int(time.monotonic() * 1000)

    def ticks_diff(a, b):
        return a - b

    def ticks_add(a, b):
        return a + b


def hex_bssid(raw):
    return ":".join("%02x" % b for b in raw)


def text(raw):
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode()
    except Exception:
        return "".join(chr(b) if 32 <= b < 127 else "?" for b in raw)


def _clamp(value, low, high, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if value < low:
        return low
    if value > high:
        return high
    return value


class Agent:
    def __init__(self, wlan, out, ping=None, clock=ticks_ms, info=None):
        self.wlan = wlan
        self.out = out
        self.ping = ping
        self.clock = clock
        self.info = info or {}
        self.mode = None
        self.scan_interval_ms = 15000
        self.link_interval_ms = 1000
        self.ssid = None
        self.password = None
        self.target = None
        self.next_scan = 0
        self.next_link = 0
        self.next_status = 0
        self.connecting_since = None
        self.next_connect_attempt = None
        self.was_connected = False
        self.ping_seq = 0
        self.scan_count = 0

    # -- output -----------------------------------------------------------

    def emit(self, obj):
        self.out.write(json.dumps(obj) + "\n")

    def event(self, kind, **detail):
        msg = {"t": "event", "kind": kind}
        msg.update(detail)
        self.emit(msg)

    def hello(self):
        msg = {"t": "hello", "agent": AGENT_VERSION, "mode": self.mode}
        msg.update(self.info)
        self.emit(msg)

    # -- commands ---------------------------------------------------------

    def handle_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            cmd = json.loads(line)
        except ValueError:
            self.emit({"t": "error", "message": "invalid JSON command"})
            return
        if not isinstance(cmd, dict):
            self.emit({"t": "error", "message": "command must be an object"})
            return
        self.handle(cmd)

    def handle(self, cmd):
        name = cmd.get("cmd")
        if name == "hello":
            self.hello()
        elif name == "stop":
            self.stop()
            self.event("stopped")
        elif name in MODES:
            self.start(name, cmd)
        else:
            self.emit({"t": "error", "message": "unknown command: %s" % name})

    def start(self, mode, cmd):
        if mode in ("connect", "both") and not cmd.get("ssid"):
            self.emit({"t": "error", "message": "ssid is required for mode %s" % mode})
            return
        self.stop()
        self.mode = mode
        self.scan_interval_ms = int(_clamp(cmd.get("scan_interval"), 1, 3600, 15) * 1000)
        self.link_interval_ms = int(_clamp(cmd.get("link_interval"), 0.2, 60, 1) * 1000)
        self.ssid = cmd.get("ssid")
        self.password = cmd.get("password") or ""
        self.target = cmd.get("target") or None
        now = self.clock()
        self.next_scan = now
        self.next_link = now
        if mode == "both":
            # Let the link settle before the first scan interrupts it.
            self.next_scan = ticks_add(now, self.scan_interval_ms)
        self.wlan.active(True)
        if mode in ("connect", "both"):
            self.begin_connect(now)
        self.event("started", mode=mode, ssid=self.ssid,
                   scan_interval=self.scan_interval_ms / 1000,
                   link_interval=self.link_interval_ms / 1000)

    def stop(self):
        if self.mode in ("connect", "both"):
            try:
                self.wlan.disconnect()
            except Exception:
                pass
        self.mode = None
        self.password = None
        self.connecting_since = None
        self.next_connect_attempt = None
        self.was_connected = False

    # -- connection -------------------------------------------------------

    def begin_connect(self, now):
        try:
            self.wlan.disconnect()
        except Exception:
            pass
        try:
            self.wlan.connect(self.ssid, self.password)
        except Exception as exc:
            self.event("connect_error", message=str(exc))
            self.connecting_since = None
            self.next_connect_attempt = ticks_add(now, RECONNECT_BACKOFF_MS)
            return
        self.connecting_since = now
        self.next_connect_attempt = None
        self.event("connecting", ssid=self.ssid)

    def gateway(self):
        try:
            return self.wlan.ipconfig("gw4")
        except Exception:
            try:
                return self.wlan.ifconfig()[2]
            except Exception:
                return None

    def link_info(self):
        info = {}
        try:
            info["ip"] = self.wlan.ifconfig()[0]
        except Exception:
            pass
        for key in ("channel", "bssid"):
            try:
                value = self.wlan.config(key)
                info[key] = hex_bssid(value) if key == "bssid" else value
            except Exception:
                pass
        return info

    def check_connection(self, now):
        connected = self.wlan.isconnected()
        if connected and not self.was_connected:
            self.was_connected = True
            self.connecting_since = None
            detail = self.link_info()
            detail["gateway"] = self.gateway()
            self.event("connected", **detail)
        elif not connected and self.was_connected:
            self.was_connected = False
            try:
                status = self.wlan.status()
            except Exception:
                status = None
            self.event("disconnected", status=status)
            self.next_connect_attempt = ticks_add(now, RECONNECT_BACKOFF_MS)
        elif not connected and self.connecting_since is not None:
            if ticks_diff(now, self.connecting_since) > CONNECT_TIMEOUT_MS:
                try:
                    status = self.wlan.status()
                except Exception:
                    status = None
                self.event("connect_timeout", status=status)
                self.connecting_since = None
                self.next_connect_attempt = ticks_add(now, RECONNECT_BACKOFF_MS)
        if (not connected and self.connecting_since is None
                and self.next_connect_attempt is not None
                and ticks_diff(now, self.next_connect_attempt) >= 0):
            self.begin_connect(now)
        return connected

    def link_sample(self, now):
        connected = self.check_connection(now)
        msg = {"t": "link", "ms": now, "connected": connected,
               "rssi": None, "lat": None, "ok": False}
        if connected:
            try:
                msg["rssi"] = self.wlan.status("rssi")
            except Exception:
                pass
            target = self.target or self.gateway()
            if target and self.ping:
                self.ping_seq = (self.ping_seq + 1) & 0xFFFF
                try:
                    latency = self.ping(target, PING_TIMEOUT_MS, self.ping_seq)
                except Exception as exc:
                    latency = None
                    msg["err"] = str(exc)
                msg["lat"] = latency
                msg["ok"] = latency is not None
            msg["target"] = target
        self.emit(msg)

    # -- scanning ---------------------------------------------------------

    def scan(self, now):
        start = self.clock()
        try:
            results = self.wlan.scan()
        except Exception as exc:
            self.emit({"t": "error", "message": "scan failed: %s" % exc})
            return
        duration = ticks_diff(self.clock(), start)
        aps = []
        for ssid, bssid, channel, rssi, auth, hidden in results:
            aps.append([text(ssid), hex_bssid(bssid), channel, rssi, auth, bool(hidden)])
        self.scan_count += 1
        self.emit({"t": "scan", "ms": now, "n": self.scan_count, "dur": duration, "aps": aps})
        if gc:
            gc.collect()

    # -- main loop step ---------------------------------------------------

    def tick(self):
        now = self.clock()
        if self.mode in ("connect", "both") and ticks_diff(now, self.next_link) >= 0:
            self.next_link = ticks_add(now, self.link_interval_ms)
            self.link_sample(now)
        now = self.clock()
        if self.mode in ("scan", "both") and ticks_diff(now, self.next_scan) >= 0:
            self.next_scan = ticks_add(now, self.scan_interval_ms)
            self.scan(now)
        now = self.clock()
        if ticks_diff(now, self.next_status) >= 0:
            self.next_status = ticks_add(now, STATUS_INTERVAL_MS)
            status = {"t": "status", "ms": now, "mode": self.mode,
                      "connected": bool(self.mode in ("connect", "both") and self.wlan.isconnected())}
            if hasattr(gc, "mem_free"):
                status["mem"] = gc.mem_free()
            self.emit(status)


# -- hardware glue (MicroPython only) -------------------------------------

def _checksum(data):
    if len(data) & 1:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def icmp_ping(host, timeout_ms, seq):
    """Send one ICMP echo request; return round-trip time in ms or None on timeout."""
    import socket
    import struct

    ident = 0x574D
    payload = b"wifimon-probe-0123456789abcdef"
    packet = struct.pack("!BBHHH", 8, 0, 0, ident, seq) + payload
    packet = packet[:2] + struct.pack("!H", _checksum(packet)) + packet[4:]
    addr = socket.getaddrinfo(host, 1)[0][-1]
    sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, 1)
    try:
        sock.settimeout(timeout_ms / 1000)
        sock.connect(addr)
        start = time.ticks_us()
        sock.send(packet)
        while True:
            remaining = timeout_ms * 1000 - time.ticks_diff(time.ticks_us(), start)
            if remaining <= 0:
                return None
            sock.settimeout(remaining / 1000000)
            try:
                reply = sock.recv(256)
            except OSError:
                return None
            header_len = (reply[0] & 0x0F) * 4
            icmp = reply[header_len:]
            if len(icmp) >= 8 and icmp[0] == 0:
                r_ident, r_seq = struct.unpack("!HH", icmp[4:8])
                if r_ident == ident and r_seq == seq:
                    return round(time.ticks_diff(time.ticks_us(), start) / 1000, 2)
    finally:
        sock.close()


def run():
    import machine
    import network
    import select

    wlan = network.WLAN(network.WLAN.IF_STA)
    wlan.active(True)
    info = {"fw": ".".join(str(v) for v in sys.implementation.version[:3]),
            "machine": sys.implementation._machine,
            "mac": hex_bssid(wlan.config("mac")),
            "freq": machine.freq()}
    agent = Agent(wlan, sys.stdout, ping=icmp_ping, info=info)
    agent.hello()

    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    buf = ""
    while True:
        # Drain any pending command bytes without blocking the measurement loop.
        while poller.poll(0):
            ch = sys.stdin.read(1)
            if ch in ("\n", "\r"):
                if buf:
                    agent.handle_line(buf)
                buf = ""
            elif len(buf) < 1024:
                buf += ch
        agent.tick()
        time.sleep_ms(20)
