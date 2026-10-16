# Wi-Fi fleet unit agent (MicroPython on ESP32-C5).
#
# Joins the configured Wi-Fi network, performs the handshake with the fleet server,
# measures the connection continuously and uploads batches. Started by main.py, which
# also rolls back a failed remote update.
#
# Notes for MicroPython:
# - Floats are single precision on this port (a Unix timestamp is off by ~100 s), so
#   all times are integers: ticks in ms, epoch in ms or whole seconds.
# - The firmware cannot report the BSSID of the current connection, so the agent picks
#   the strongest access point from a scan and pins the connection to it.
# - Without PSRAM there is not enough contiguous memory for TLS once the agent runs, so the
#   unit talks plain HTTP and signs every request with HMAC-SHA256; the server signs its
#   responses the same way (#9). The unit key never crosses the network.
#
# The Agent class gets all hardware and network access injected, so the same code runs
# under CPython in the test suite.

AGENT_VERSION = "2.1.0"

import json
import os
import sys
import time

try:
    import gc
except ImportError:  # pragma: no cover
    gc = None

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

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
SPOOL_DIR = "spool"
PENDING_FILE = "update_pending.json"
FAILED_FILE = "update_failed.json"
ERROR_LOG = "last_error.txt"

DEFAULTS = {"sample_interval_s": 5, "batch_interval_s": 60, "scan_interval_s": 600,
            "server_ping_interval_s": 30, "ping_target": "gateway"}

HTTP_TIMEOUT_S = 10
PING_TIMEOUT_MS = 1000
CONNECT_TIMEOUT_MS = 20000
BACKOFF_MS = (5000, 10000, 20000, 40000, 60000)
AUTH_BACKOFF_MS = 600000
REHELLO_MS = 6 * 3600 * 1000
WIFI_GIVE_UP_MS = 10 * 60 * 1000       # reboot when Wi-Fi has been down this long
SERVER_GIVE_UP_MS = 60 * 60 * 1000     # reboot when the server has been unreachable this long
MAX_SAMPLES = 240                      # in RAM before the first handshake (20 min at 5 s); ~30 KB
MAX_EVENTS = 100
MAX_SCANS = 3
MAX_SPOOL = 300                        # batches kept on flash while the server is unreachable
SPOOL_DRAIN = 5                        # spooled batches sent per loop step

WLAN_STATUS = {200: "beacon timeout", 201: "network not found", 202: "wrong password",
               203: "association failed", 204: "handshake timeout",
               210: "no network with compatible security", 1000: "idle", 1001: "connecting",
               1010: "got IP"}


# -- small helpers -----------------------------------------------------------

def hex_bytes(raw, sep=":"):
    return sep.join("%02x" % b for b in raw)


def bssid_bytes(text):
    return bytes(int(part, 16) for part in text.split(":"))


def ssid_text(raw):
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode()
    except Exception:
        return "".join(chr(b) if 32 <= b < 127 else "?" for b in raw)


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    try:
        os.remove(path)
    except OSError:
        pass
    os.rename(tmp, path)


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def sha256_hex(data):
    import hashlib

    digest = hashlib.sha256(data).digest()
    return hex_bytes(digest, "")


def hmac_sha256(key, message):
    """HMAC-SHA256 (RFC 2104). MicroPython has hashlib but no hmac module."""
    import hashlib

    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + b"\x00" * (64 - len(key))
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key) + message).digest()
    return hashlib.sha256(bytes(b ^ 0x5C for b in key) + inner).digest()


def b64url_decode(text):
    import binascii

    text = text.replace("-", "+").replace("_", "/")
    return binascii.a2b_base64(text + "=" * (-len(text) % 4))


def header(headers, name):
    """Case-insensitive header lookup (servers and proxies differ in capitalisation)."""
    name = name.lower()
    for key in headers or {}:
        if key.lower() == name:
            value = headers[key]
            return value.decode() if isinstance(value, bytes) else value
    return None


class BadSignature(Exception):
    pass


class Clock:
    """Epoch time from the server, kept with ticks_ms (no floats)."""

    def __init__(self, ticks):
        self.ticks = ticks
        self.base_ms = None
        self.base_ticks = 0

    def synced(self):
        return self.base_ms is not None

    def sync(self, server_ms, sent_ticks):
        now = self.ticks()
        rtt = max(0, ticks_diff(now, sent_ticks))
        self.base_ms = int(server_ms) + rtt // 2
        self.base_ticks = now

    def epoch_s(self, at_ticks=None):
        if at_ticks is None:
            at_ticks = self.ticks()
        return (self.base_ms + ticks_diff(at_ticks, self.base_ticks)) // 1000


class Spool:
    """Batches waiting for the server, one file each, on flash. Survives reboots."""

    def __init__(self, path=SPOOL_DIR, limit=MAX_SPOOL):
        self.path = path
        self.limit = limit
        if not exists(path):
            os.mkdir(path)
        names = self.names()
        self.seq = int(names[-1].split(".")[0]) + 1 if names else 1

    def names(self):
        return sorted(n for n in os.listdir(self.path) if n.endswith(".json"))

    def count(self):
        return len(self.names())

    def put(self, body):
        """Store a batch; returns how many old batches were dropped to stay under the limit."""
        name = "%08d.json" % self.seq
        self.seq += 1
        with open(self.path + "/" + name, "w") as f:
            f.write(body)
        names = self.names()
        dropped = 0
        while len(names) > self.limit:
            remove(self.path + "/" + names.pop(0))
            dropped += 1
        return dropped

    def oldest(self):
        names = self.names()
        return names[0] if names else None

    def read(self, name):
        with open(self.path + "/" + name) as f:
            return f.read()

    def remove(self, name):
        remove(self.path + "/" + name)


class Agent:
    def __init__(self, config, wlan, http, ping=None, hw=None, ticks=ticks_ms, info=None, urandom=os.urandom):
        self.config = config
        self.wlan = wlan
        self.http = http
        self.ping = ping
        self.hw = hw
        self.ticks = ticks
        self.info = info or {}
        self.server = config["server_url"].rstrip("/")
        self.key = config["unit_key"]
        self.unit_id, _, secret = self.key.partition(".")
        self.sign_key = b64url_decode(secret)
        self.urandom = urandom
        self.signature_reported = False
        state = read_json(STATE_FILE, {}) or {}
        self.cfg = dict(DEFAULTS)
        self.cfg.update(state.get("config") or {})
        self.last_bssid = state.get("bssid")
        self.clock = Clock(ticks)
        self.spool = Spool()
        self.boot_id = hex_bytes(urandom(4), "")
        self.batch_seq = 0
        self.samples = []
        self.scans = []
        self.events = []
        self.dropped = 0
        now = ticks()
        self.started = now
        # Wi-Fi state: "down" (connect at next_connect), "connecting" or "up"
        self.wifi = "down"
        self.next_connect = now
        self.connect_started = now
        self.connect_failures = 0
        self.wifi_up_since = None
        self.wifi_down_since = now
        self.target = None  # (bssid, channel, rssi) the connection is pinned to
        self.hidden = []    # hidden access points from the last connect scan
        # Server state
        self.hello_ok = False
        self.next_hello = now
        self.server_failures = 0
        self.server_down_since = None
        self.server_down_reported = False
        self.update_checked = False
        # Measurement timers
        self.next_sample = now
        self.next_batch = ticks_add(now, self.ms("batch_interval_s"))
        self.next_scan = ticks_add(now, self.ms("scan_interval_s"))
        self.next_srv_ping = now
        self.loops = 0

    def ms(self, key):
        return int(float(self.cfg[key]) * 1000)

    # -- events -------------------------------------------------------------

    def event(self, kind, **detail):
        self.events.append((self.ticks(), kind, detail or None))
        if len(self.events) > MAX_EVENTS:
            self.events.pop(0)

    def boot(self, reset_cause=None):
        detail = {"reset_cause": reset_cause or "unknown", "agent": AGENT_VERSION}
        try:
            with open(ERROR_LOG) as f:
                detail["error"] = f.read()[:300]
            remove(ERROR_LOG)
        except OSError:
            pass
        self.reset_cause = detail["reset_cause"]
        self.event("boot", **detail)

    # -- Wi-Fi --------------------------------------------------------------

    def status_text(self):
        try:
            code = self.wlan.status()
        except Exception:
            return None
        return WLAN_STATUS.get(code, "status %s" % code)

    def scan(self, now):
        start = self.ticks()
        self.feed()
        try:
            results = self.wlan.scan()
        except Exception as exc:
            self.event("error", message="scan failed: %s" % exc)
            return None
        self.feed()
        aps = [[ssid_text(s), hex_bytes(b), ch, rssi, auth, bool(hidden)]
               for s, b, ch, rssi, auth, hidden in results]
        self.scans.append((now, ticks_diff(self.ticks(), start), aps))
        if len(self.scans) > MAX_SCANS:
            self.scans.pop(0)
        self.next_scan = ticks_add(now, self.ms("scan_interval_s"))
        return aps

    def best_ap(self, aps):
        ssid = self.config["wifi_ssid"]
        best = None
        for ap in aps:
            if ap[0] == ssid and (best is None or ap[3] > best[3]):
                best = ap
        return best

    def wifi_step(self, now):
        connected = self.wlan.isconnected()
        if self.wifi == "up":
            if connected:
                return
            up_s = ticks_diff(now, self.wifi_up_since) // 1000
            self.event("wifi_lost", reason=self.status_text(), up_s=up_s)
            self.wifi = "down"
            self.wifi_down_since = now
            self.next_connect = now
            return
        if self.wifi == "connecting":
            if connected:
                if self.target is None:
                    self.target = self.hidden_target()
                bssid, channel, _ = self.target
                took_s = ticks_diff(now, self.connect_started) // 1000
                self.event("wifi_connected", ssid=self.config["wifi_ssid"], bssid=bssid, channel=channel,
                           took_s=took_s)
                if bssid and self.last_bssid and self.last_bssid != bssid:
                    self.event("roam", **{"from": self.last_bssid, "to": bssid, "channel": channel})
                self.last_bssid = bssid or self.last_bssid
                self.save_state()
                self.wifi = "up"
                self.wifi_up_since = now
                self.wifi_down_since = None
                self.connect_failures = 0
                return
            if ticks_diff(now, self.connect_started) > CONNECT_TIMEOUT_MS:
                self.connect_failed(now, self.status_text() or "timeout")
            return
        # down
        if ticks_diff(now, self.next_connect) < 0:
            return
        aps = self.scan(now) or []
        ap = self.best_ap(aps)
        # Hidden networks broadcast no name; then connect by name without pinning and work
        # out the access point afterwards from the channel.
        self.hidden = [a for a in aps if a[0] == "" or a[5]]
        if ap is None and not self.hidden:
            self.connect_failed(now, "network %s not found" % self.config["wifi_ssid"])
            return
        try:
            self.wlan.disconnect()
        except Exception:
            pass
        try:
            if ap is not None:
                self.wlan.connect(self.config["wifi_ssid"], self.config.get("wifi_password", ""),
                                  bssid=bssid_bytes(ap[1]))
            else:
                self.wlan.connect(self.config["wifi_ssid"], self.config.get("wifi_password", ""))
        except Exception as exc:
            self.connect_failed(now, str(exc))
            return
        self.target = (ap[1], ap[2], ap[3]) if ap is not None else None
        self.wifi = "connecting"
        self.connect_started = self.ticks()

    def hidden_target(self):
        """(bssid, channel, rssi) for an unpinned connection to a hidden network."""
        try:
            channel = self.wlan.config("channel")
        except Exception:
            channel = None
        best = None
        for ap in self.hidden:
            if ap[2] == channel and (best is None or ap[3] > best[3]):
                best = ap
        if best is None:
            return (None, channel, None)
        return (best[1], best[2], best[3])

    def connect_failed(self, now, reason):
        self.event("wifi_connect_failed", reason=reason)
        try:
            self.wlan.disconnect()
        except Exception:
            pass
        self.wifi = "down"
        delay = BACKOFF_MS[min(self.connect_failures, len(BACKOFF_MS) - 1)]
        self.connect_failures += 1
        self.next_connect = ticks_add(now, delay)

    def gateway(self):
        try:
            return self.wlan.ipconfig("gw4")
        except Exception:
            try:
                return self.wlan.ifconfig()[2]
            except Exception:
                return None

    def ip(self):
        try:
            return self.wlan.ifconfig()[0]
        except Exception:
            return None

    # -- server ---------------------------------------------------------------

    def request(self, method, path, body=None, timeout_s=HTTP_TIMEOUT_S, signed=True):
        """HTTP request to the server. Signed requests need a synced clock and get verified replies."""
        data = body.encode() if isinstance(body, str) else body
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        nonce = None
        if signed:
            timestamp = str(self.clock.epoch_s())
            nonce = hex_bytes(self.urandom(12), "")
            message = "\n".join((method, path, timestamp, nonce, sha256_hex(data or b""))).encode()
            headers["X-Unit-Id"] = self.unit_id
            headers["X-Timestamp"] = timestamp
            headers["X-Nonce"] = nonce
            headers["X-Signature"] = hex_bytes(hmac_sha256(self.sign_key, message), "")
        self.feed()
        try:
            status, content, reply_headers = self.http(method, self.server + path, data, headers, timeout_s)
        finally:
            self.feed()
        if isinstance(content, str):
            content = content.encode()
        if signed and status == 200:
            expected = hex_bytes(hmac_sha256(
                self.sign_key, ("%s\n200\n%s" % (nonce, sha256_hex(content))).encode()), "")
            if (header(reply_headers, "x-signature") or "").lower() != expected:
                if not self.signature_reported:
                    self.event("bad_signature", error="%s %s: missing or wrong response signature" % (method, path))
                    self.signature_reported = True
                raise BadSignature("response signature check failed")
        return status, content

    def sync_time(self, now):
        """Set the clock from the server before the first signed request."""
        sent = self.ticks()
        try:
            status, body = self.request("GET", "/api/unit/v1/time", timeout_s=5, signed=False)
            if status != 200:
                raise OSError("HTTP %s" % status)
            self.clock.sync(json.loads(body)["server_time_ms"], sent)
        except Exception as exc:
            self.server_failed(now, exc)
            return False
        return True

    def server_ok(self):
        if self.server_down_since is not None and self.server_down_reported:
            self.event("server_reachable", down_s=ticks_diff(self.ticks(), self.server_down_since) // 1000)
        self.server_failures = 0
        self.server_down_since = None
        self.server_down_reported = False

    def server_failed(self, now, error):
        if self.server_down_since is None:
            self.server_down_since = now
        if not self.server_down_reported:
            self.event("server_unreachable", error=str(error)[:120])
            self.server_down_reported = True
        delay = BACKOFF_MS[min(self.server_failures, len(BACKOFF_MS) - 1)]
        self.server_failures += 1
        self.next_hello = ticks_add(now, delay)

    def auth_failed(self, now):
        if not self.server_down_reported:
            self.event("auth_failed")
            self.server_down_reported = True
        if self.server_down_since is None:
            self.server_down_since = now
        self.hello_ok = False
        self.next_hello = ticks_add(now, AUTH_BACKOFF_MS)

    def hello(self, now):
        payload = {"agent_version": AGENT_VERSION, "reset_cause": self.reset_cause_name(),
                   "uptime_s": ticks_diff(now, self.started) // 1000, "buffered": self.spool.count()}
        for key in ("fw_version", "chip", "mac"):
            if self.info.get(key):
                payload[key] = self.info[key]
        payload["ssid"] = self.config["wifi_ssid"]
        if self.target:
            payload["bssid"], payload["channel"] = self.target[0], self.target[1]
        try:
            payload["rssi"] = self.wlan.status("rssi")
        except Exception:
            pass
        ip = self.ip()
        if ip:
            payload["ip"] = ip
        if not self.clock.synced() and not self.sync_time(now):
            return
        sent = self.ticks()
        try:
            status, body = self.request("POST", "/api/unit/v1/hello", json.dumps(payload))
        except Exception as exc:
            self.server_failed(now, exc)
            return
        if status == 401:
            self.auth_failed(now)
            return
        if status != 200:
            self.server_failed(now, "HTTP %s" % status)
            return
        try:
            reply = json.loads(body)
            self.clock.sync(reply["server_time_ms"], sent)
        except (ValueError, KeyError, TypeError):
            self.server_failed(now, "bad handshake reply")
            return
        self.cfg = dict(DEFAULTS)
        self.cfg.update(reply.get("config") or {})
        self.save_state()
        self.reschedule(now)
        self.hello_ok = True
        self.next_hello = ticks_add(now, REHELLO_MS)
        self.server_ok()
        self.confirm_update()
        if reply.get("update"):
            self.apply_update(reply["update"])

    def reschedule(self, now):
        """Pull timers forward when the server shortened an interval."""
        for attr, key in (("next_sample", "sample_interval_s"), ("next_batch", "batch_interval_s"),
                          ("next_scan", "scan_interval_s")):
            due = ticks_add(now, self.ms(key))
            if ticks_diff(getattr(self, attr), due) > 0:
                setattr(self, attr, due)

    def reset_cause_name(self):
        return getattr(self, "reset_cause", None) or "unknown"

    def save_state(self):
        try:
            write_json(STATE_FILE, {"config": self.cfg, "bssid": self.last_bssid})
        except OSError:
            pass

    def server_ping(self):
        sent = self.ticks()
        try:
            status, body = self.request("GET", "/api/unit/v1/ping", timeout_s=3)
        except Exception:
            return None
        if status != 200:
            return None
        rtt = ticks_diff(self.ticks(), sent)
        try:
            self.clock.sync(json.loads(body)["server_time_ms"], sent)
        except (ValueError, KeyError):
            pass
        return rtt

    # -- measurements -----------------------------------------------------------

    def sample(self, now):
        connected = self.wifi == "up" and self.wlan.isconnected()
        if not connected and self.wifi_up_since is None:
            return  # still joining after boot: not network downtime, failures are reported as events
        rssi = bssid = channel = gw_ms = gw_lost = srv_ms = srv_lost = None
        if connected:
            try:
                rssi = self.wlan.status("rssi")
            except Exception:
                pass
            bssid, channel = self.target[0], self.target[1]
            target = self.cfg.get("ping_target") or "gateway"
            host = self.gateway() if target == "gateway" else target
            if host and self.ping:
                try:
                    gw_ms = self.ping(host, PING_TIMEOUT_MS)
                except Exception:
                    gw_ms = None
                gw_lost = gw_ms is None
            interval = self.ms("server_ping_interval_s")
            if self.hello_ok and interval and ticks_diff(now, self.next_srv_ping) >= 0:
                self.next_srv_ping = ticks_add(now, interval)
                srv_ms = self.server_ping()
                srv_lost = srv_ms is None
        self.samples.append((now, connected, rssi, bssid, channel, gw_ms, gw_lost, srv_ms, srv_lost))
        if len(self.samples) > MAX_SAMPLES:
            self.samples.pop(0)
            self.dropped += 1

    def build_batch(self):
        """Turn buffered measurements into a batch body without sent_at (added when sending)."""
        epoch = self.clock.epoch_s
        samples = []
        for t, c, rssi, bssid, ch, gw, gwl, srv, srvl in self.samples:
            s = {"t": epoch(t), "connected": c}
            for key, value in (("rssi", rssi), ("bssid", bssid), ("channel", ch), ("gw_ms", gw),
                               ("gw_lost", gwl), ("srv_ms", srv), ("srv_lost", srvl)):
                if value is not None:
                    s[key] = value
            samples.append(s)
        scans = [{"t": epoch(t), "duration_ms": dur, "aps": aps} for t, dur, aps in self.scans]
        events = []
        for t, kind, detail in self.events:
            e = {"t": epoch(t), "kind": kind}
            if detail:
                e["detail"] = detail
            events.append(e)
        self.batch_seq += 1
        body = {"batch_id": "%s-%s-%d" % (self.unit_id, self.boot_id, self.batch_seq),
                "samples": samples, "scans": scans, "events": events}
        self.samples, self.scans, self.events = [], [], []
        return json.dumps(body)

    def send(self, body):
        """POST a batch body; True when the server has it (stored or duplicate), False to retry later."""
        sent = self.ticks()
        full = '{"sent_at":%d,%s' % (self.clock.epoch_s(sent), body[1:])
        try:
            status, reply = self.request("POST", "/api/unit/v1/batch", full)
        except Exception as exc:
            self.server_failed(sent, exc)
            return False
        if status == 200:
            try:
                self.clock.sync(json.loads(reply)["server_time_ms"], sent)
            except (ValueError, KeyError):
                pass
            self.server_ok()
            return True
        if status == 401:
            self.auth_failed(sent)
            return False
        if 400 <= status < 500 and status not in (408, 429):
            # The server will never accept this batch; keeping it would block the spool forever.
            self.event("batch_rejected", error="HTTP %s: %s" % (status, ssid_text((reply or b"")[:100])))
            return True
        self.server_failed(sent, "HTTP %s" % status)
        return False

    def flush(self, now, online):
        if not self.clock.synced():
            return  # keep measuring in RAM until the first handshake gives us the time
        if self.dropped:
            self.event("buffer_overflow", dropped=self.dropped)
            self.dropped = 0
        if not (self.samples or self.scans or self.events):
            return
        body = self.build_batch()
        if online and self.send(body):
            return
        dropped = self.spool.put(body)
        if dropped:
            self.event("buffer_overflow", dropped=dropped, unit="batches")

    def drain_spool(self):
        for _ in range(SPOOL_DRAIN):
            name = self.spool.oldest()
            if name is None:
                return
            if not self.send(self.spool.read(name)):
                return
            self.spool.remove(name)

    # -- remote update ------------------------------------------------------------------

    def confirm_update(self):
        """After the first good handshake: confirm a pending update, report a rolled-back one."""
        pending = read_json(PENDING_FILE)
        if pending and pending.get("version") == AGENT_VERSION:
            remove(PENDING_FILE)
            self.event("update_ok", version=AGENT_VERSION, previous=pending.get("previous"))
        failed = read_json(FAILED_FILE)
        if failed and not failed.get("reported"):
            self.event("update_failed", version=failed.get("version"), error=failed.get("error"))
            failed["reported"] = True
            write_json(FAILED_FILE, failed)

    def apply_update(self, offer):
        version = offer.get("version")
        if not version or version == AGENT_VERSION or self.update_checked:
            return
        self.update_checked = True
        failed = read_json(FAILED_FILE) or {}
        if failed.get("version") == version:
            self.event("update_skipped", version=version)
            return
        try:
            status, data = self.request("GET", offer.get("url", "/api/unit/v1/agent"), timeout_s=30)
        except Exception as exc:
            self.event("update_failed", version=version, error=str(exc)[:120])
            return
        if isinstance(data, str):
            data = data.encode()
        if status != 200 or len(data) != offer.get("size") or sha256_hex(data) != offer.get("sha256"):
            self.event("update_failed", version=version, error="download did not match (HTTP %s)" % status)
            return
        with open("app_new.py", "wb") as f:
            f.write(data)
        self.event("update_started", version=version)
        write_json(PENDING_FILE, {"version": version, "previous": AGENT_VERSION, "tries": 0})
        remove("app_prev.py")
        os.rename("app.py", "app_prev.py")
        os.rename("app_new.py", "app.py")
        self.restart("installing agent %s" % version)

    # -- main loop --------------------------------------------------------------------

    def feed(self):
        if self.hw:
            self.hw.feed()

    def restart(self, reason):
        self.event("rebooting", reason=reason)
        self.flush(self.ticks(), online=False)
        if self.hw:
            self.hw.reset()

    def step(self):
        self.feed()
        now = self.ticks()
        self.wifi_step(now)
        online = self.wifi == "up"
        now = self.ticks()

        if online and ticks_diff(now, self.next_hello) >= 0:
            self.hello(now)

        if ticks_diff(now, self.next_sample) >= 0:
            self.next_sample = ticks_add(now, self.ms("sample_interval_s"))
            self.sample(now)

        now = self.ticks()
        scan_ms = self.ms("scan_interval_s")
        if online and scan_ms and ticks_diff(now, self.next_scan) >= 0:
            self.scan(now)

        now = self.ticks()
        if ticks_diff(now, self.next_batch) >= 0:
            self.next_batch = ticks_add(now, self.ms("batch_interval_s"))
            self.flush(now, online and self.hello_ok)
            if online and self.hello_ok and self.server_failures == 0:
                self.drain_spool()

        now = self.ticks()
        if self.wifi_down_since is not None and ticks_diff(now, self.wifi_down_since) > WIFI_GIVE_UP_MS:
            self.restart("no Wi-Fi for %d min" % (WIFI_GIVE_UP_MS // 60000))
        elif self.server_down_since is not None and ticks_diff(now, self.server_down_since) > SERVER_GIVE_UP_MS:
            self.restart("server unreachable for %d min" % (SERVER_GIVE_UP_MS // 60000))

        self.loops += 1
        if gc and self.loops % 20 == 0:
            gc.collect()


# -- hardware glue (MicroPython only) -------------------------------------------------

def _checksum(data):
    if len(data) & 1:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


_ping_seq = [0]


def icmp_ping(host, timeout_ms):
    """One ICMP echo; returns the round trip in ms (int on MicroPython floats is fine) or None."""
    import socket
    import struct

    _ping_seq[0] = (_ping_seq[0] + 1) & 0xFFFF
    seq = _ping_seq[0]
    ident = 0x574D
    packet = struct.pack("!BBHHH", 8, 0, 0, ident, seq) + b"wifimon-fleet-probe-0123456789"
    packet = packet[:2] + struct.pack("!H", _checksum(packet)) + packet[4:]
    addr = socket.getaddrinfo(host, 1)[0][-1]
    sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, 1)
    try:
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
            icmp = reply[(reply[0] & 0x0F) * 4:]
            if len(icmp) >= 8 and icmp[0] == 0 and struct.unpack("!HH", icmp[4:8]) == (ident, seq):
                return round(time.ticks_diff(time.ticks_us(), start) / 1000, 2)
    finally:
        sock.close()


def http_request(method, url, body, headers, timeout_s):
    import requests

    response = requests.request(method, url, data=body, headers=headers, timeout=timeout_s)
    try:
        return response.status_code, response.content, response.headers
    finally:
        response.close()


class Hardware:
    def __init__(self, machine, watchdog_s):
        self.machine = machine
        self.wdt = machine.WDT(timeout=watchdog_s * 1000) if watchdog_s else None

    def feed(self):
        if self.wdt:
            self.wdt.feed()

    def reset(self):
        time.sleep_ms(200)
        self.machine.reset()


def reset_cause_name(machine):
    names = {getattr(machine, n): n[:-6].lower() for n in dir(machine) if n.endswith("_RESET")}
    return names.get(machine.reset_cause(), "unknown")


def run():
    import machine
    import network

    config = read_json(CONFIG_FILE)
    if not config:
        raise RuntimeError("config.json is missing; provision the unit first")
    unit_id = config["unit_key"].split(".")[0]
    try:
        network.hostname("wifimon-" + unit_id)
    except Exception:
        pass
    wlan = network.WLAN(network.WLAN.IF_STA)
    wlan.active(True)
    try:
        wlan.config(pm=wlan.PM_NONE)  # power save would add latency to every measurement
    except Exception:
        pass
    info = {"fw_version": ".".join(str(v) for v in sys.implementation.version[:3]),
            "chip": sys.implementation._machine.split(" with ")[-1][:64],
            "mac": hex_bytes(wlan.config("mac"))}
    hw = Hardware(machine, int(config.get("watchdog_s", 120)))
    agent = Agent(config, wlan, http_request, icmp_ping, hw, info=info)
    agent.boot(reset_cause_name(machine))
    while True:
        agent.step()
        time.sleep_ms(50)
