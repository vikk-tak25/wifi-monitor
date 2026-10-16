import csv
import io
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from wifimon.device import SimulatedDevice
from wifimon.monitor import Monitor
from wifimon.protocol import AccessPoint, Link, Scan
from wifimon.server import csv_safe, downsample_link, make_server

from .conftest import FakeDevice, make_scan, wait_for


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"X-Requested-With": "wifimon"}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, res.headers, res.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read()

    def json(self, method, path, body=None, headers=None):
        status, _, data = self.request(method, path, body, headers)
        return status, json.loads(data)


def run_server(monitor, storage):
    server = make_server(monitor, storage, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def device():
    return FakeDevice()


@pytest.fixture
def app(storage, device, settings, clock):
    monitor = Monitor(storage, device, settings, clock=clock)
    monitor.start()
    server = run_server(monitor, storage)
    yield Client(server.server_address[1]), monitor, device
    server.shutdown()
    server.server_close()


def test_dashboard_and_static_files_are_served(app):
    client, _, _ = app
    status, headers, body = client.request("GET", "/")
    assert status == 200 and b"<title>Wi-Fi Monitor</title>" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    for path in ("/app.js", "/styles.css", "/vendor/chart.umd.min.js", "/favicon.svg"):
        assert client.request("GET", path)[0] == 200, path
    assert client.request("GET", "/../wifimon/server.py")[0] == 404
    assert client.request("GET", "/%2e%2e/config.py")[0] == 404
    assert client.request("GET", "/nope.js")[0] == 404


def test_status_endpoint(app):
    client, _, _ = app
    status, data = client.json("GET", "/api/status")
    assert status == 200
    assert data["device"] == {"kind": "fake", "connected": True,
                              "state": {"port": "/dev/fake", "detail": "connected"}, "info": {}, "last_status": None}
    assert data["session"] is None and data["environment"] == "development"


def test_scan_session_flow_and_stats(app, clock):
    client, monitor, device = app
    status, data = client.json("POST", "/api/sessions", {"mode": "scan", "scan_interval": 5, "note": "desk"})
    assert status == 200
    sid = data["session"]["id"]
    for rssi in (-40, -44, -42):
        clock.advance(5)
        device.deliver(make_scan(("Home", "aa:aa", 6, rssi), ("Far", "bb:bb", 44, rssi - 40)))
    clock.advance(5)
    device.deliver(make_scan(("Home", "aa:aa", 6, -41)))

    status, detail = client.json("GET", f"/api/sessions/{sid}")
    assert status == 200 and detail["active"] is True
    assert detail["scan_summary"]["scans"] == 4
    assert detail["scan_summary"]["visible_now"] == 1 and detail["scan_summary"]["ever_seen"] == 2
    home, far = detail["networks"]
    assert home["bssid"] == "aa:aa" and home["seen_pct"] == 100 and home["current_rssi"] == -41
    assert far["band"] == "5 GHz" and far["seen_pct"] == 75 and far["visible_now"] is False
    assert detail["link"] is None

    status, series = client.json("GET", f"/api/sessions/{sid}/rssi?bssids=aa:aa,BB:BB")
    assert series["series"]["bb:bb"] == [-80, -84, -82, None]

    status, sessions = client.json("GET", "/api/sessions")
    assert sessions["active"] == sid and sessions["sessions"][0]["note"] == "desk"

    status, stopped = client.json("POST", "/api/sessions/stop")
    assert stopped["session"]["ended_at"] is not None
    assert device.sent[-1] == {"cmd": "stop"}


def test_connection_session_link_and_events(app, clock):
    client, _, device = app
    _, data = client.json("POST", "/api/sessions",
                          {"mode": "connect", "ssid": "Office", "password": "pw", "link_interval": 0.5})
    sid = data["session"]["id"]
    for i, lat in enumerate([2.0, 3.0, None, 5.0]):
        clock.advance(0.5)
        device.deliver(Link(True, -55, lat, lat is not None, "10.0.0.1"))
    _, detail = client.json("GET", f"/api/sessions/{sid}")
    assert detail["link"]["pings"] == 4 and detail["link"]["loss_pct"] == 25
    _, link = client.json("GET", f"/api/sessions/{sid}/link")
    assert link["total"] == 4 and len(link["samples"]) == 4
    _, events = client.json("GET", f"/api/sessions/{sid}/events?limit=10")
    assert events["events"][0]["kind"] == "session_started"


def test_validation_errors_return_400(app):
    client, _, _ = app
    assert client.json("POST", "/api/sessions", {"mode": "warp"}) == (400, {"error": "mode must be one of: scan, connect, both"})
    assert client.json("POST", "/api/sessions", {"mode": "connect", "ssid": 5})[0] == 400
    assert client.json("POST", "/api/sessions", ["scan"])[0] == 400
    status, _, _ = client.request("POST", "/api/sessions", None)
    assert status == 400
    status, _, _ = client.request("POST", "/api/sessions", None, {"Content-Type": "text/plain", "Content-Length": "0"})
    assert status == 400


def test_writes_require_dashboard_headers(app):
    client, _, _ = app
    req = urllib.request.Request(client.base + "/api/sessions", data=b'{"mode":"scan"}', method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 403
    status, data = client.json("POST", "/api/sessions", {"mode": "scan"}, {"Origin": "http://evil.example"})
    assert status == 403 and "cross-origin" in data["error"]
    status, _ = client.json("POST", "/api/sessions", {"mode": "scan"}, {"Origin": client.base})
    assert status == 200


def test_unexpected_host_header_is_rejected(app):
    client, _, _ = app
    port = client.base.rsplit(":", 1)[1]
    with socket.create_connection(("127.0.0.1", int(port)), timeout=5) as sock:
        sock.sendall(b"GET /api/status HTTP/1.1\r\nHost: attacker.example:80\r\nConnection: close\r\n\r\n")
        reply = sock.recv(4096)
    assert reply.startswith(b"HTTP/1.1 403")


def test_unknown_routes_and_methods(app):
    client, _, _ = app
    assert client.json("GET", "/api/nothing")[0] == 404
    assert client.json("GET", "/api/sessions/999")[0] == 404
    assert client.json("DELETE", "/api/sessions")[0] == 405


def test_delete_session(app):
    client, _, _ = app
    _, data = client.json("POST", "/api/sessions", {"mode": "scan"})
    sid = data["session"]["id"]
    assert client.json("DELETE", f"/api/sessions/{sid}")[0] == 409  # still running
    client.json("POST", "/api/sessions/stop")
    assert client.json("DELETE", f"/api/sessions/{sid}") == (200, {"deleted": sid})
    assert client.json("GET", f"/api/sessions/{sid}")[0] == 404


def test_csv_exports_are_spreadsheet_safe(app, clock):
    client, _, device = app
    _, data = client.json("POST", "/api/sessions", {"mode": "both", "ssid": "Home"})
    sid = data["session"]["id"]
    device.deliver(Scan(1, 9700, [AccessPoint("=HYPERLINK(\"http://x\")", "cc:cc", 1, -50, 0, False)]))
    device.deliver(Link(True, -50, 3.0, True))
    for kind in ("networks", "observations", "link", "events"):
        status, headers, body = client.request("GET", f"/api/sessions/{sid}/export/{kind}.csv")
        assert status == 200 and headers["Content-Type"].startswith("text/csv")
        assert f"wifimon-session-{sid}-{kind}.csv" in headers["Content-Disposition"]
        rows = list(csv.reader(io.StringIO(body.decode())))
        assert len(rows) >= 2, kind
    _, _, body = client.request("GET", f"/api/sessions/{sid}/export/networks.csv")
    assert "'=HYPERLINK" in body.decode()
    assert csv_safe(-50) == -50 and csv_safe("-x") == "'-x" and csv_safe("Home") == "Home"


def test_event_stream_pushes_live_updates(app):
    client, monitor, device = app
    req = urllib.request.Request(client.base + "/api/stream")
    res = urllib.request.urlopen(req, timeout=5)
    assert res.headers["Content-Type"] == "text/event-stream"

    def read_event():
        lines = []
        while True:
            line = res.readline().decode().rstrip("\n")
            if line == "" and lines:
                return lines
            if line and not line.startswith(":"):
                lines.append(line)

    assert read_event()[0] == "event: status"
    monitor.start_session("scan")
    device.deliver(make_scan(("A", "aa", 1, -40)))
    kinds = set()
    while "scan" not in kinds:
        kinds.add(read_event()[0].split(": ", 1)[1])
    assert {"session", "event", "scan"} <= kinds
    res.close()


def test_downsample_link_buckets_long_sessions():
    samples = [{"ts": float(i), "connected": i % 10 != 0, "rssi": -50, "latency_ms": 2.0, "ok": i % 5 != 1}
               for i in range(100)]
    out = downsample_link(samples, 10)
    assert len(out) == 10 and out[0]["bucket"] == 10
    assert out[0]["connected"] is False and out[0]["loss"] == pytest.approx(2 / 9, abs=1e-3)
    assert downsample_link(samples[:5], 10) == samples[:5]


def test_end_to_end_with_simulated_device(storage, settings):
    device = SimulatedDevice(seed=7, speed=200)
    monitor = Monitor(storage, device, settings)
    monitor.start()
    server = run_server(monitor, storage)
    client = Client(server.server_address[1])
    try:
        assert wait_for(lambda: device.connected)
        _, data = client.json("POST", "/api/sessions", {"mode": "both", "ssid": "HomeNet", "scan_interval": 15})
        sid = data["session"]["id"]
        assert wait_for(lambda: len(storage.scans(sid)) >= 3 and len(storage.link_samples(sid)) >= 30, timeout=10)
        _, detail = client.json("GET", f"/api/sessions/{sid}")
        assert detail["scan_summary"]["ever_seen"] >= 8
        assert detail["link"]["pings"] > 0 and detail["link"]["latency_median"] > 0
        assert any(n["ssid"] == "HomeNet" for n in detail["networks"])
        client.json("POST", "/api/sessions/stop")
    finally:
        server.shutdown()
        server.server_close()
        monitor.shutdown()
