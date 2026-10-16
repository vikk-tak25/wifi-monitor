"""HTTP server: JSON API, live Server-Sent Events, CSV export and the dashboard files."""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import mimetypes
import queue
import re
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import stats
from .monitor import Monitor, ValidationError
from .storage import Storage

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 16 * 1024
SSE_KEEPALIVE = 15.0
MAX_LINK_POINTS = 3000
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formulas in text cells (SSIDs are chosen by strangers)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def to_csv(header: list[str], rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_safe(v) for v in row])
    return buf.getvalue().encode("utf-8")


def downsample_link(samples: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    """Average link samples into at most max_points buckets so long sessions stay chartable."""
    if len(samples) <= max_points:
        return samples
    size = math.ceil(len(samples) / max_points)
    out = []
    for i in range(0, len(samples), size):
        bucket = samples[i:i + size]
        connected = [s for s in bucket if s["connected"]]
        lat = [s["latency_ms"] for s in connected if s["ok"] and s["latency_ms"] is not None]
        rssi = [s["rssi"] for s in connected if s["rssi"] is not None]
        out.append({
            "ts": bucket[0]["ts"],
            "connected": len(connected) == len(bucket),
            "rssi": round(sum(rssi) / len(rssi), 1) if rssi else None,
            "latency_ms": round(sum(lat) / len(lat), 2) if lat else None,
            "ok": bool(lat),
            "loss": round(1 - len(lat) / len(connected), 3) if connected else None,
            "bucket": len(bucket),
        })
    return out


class App:
    def __init__(self, monitor: Monitor, storage: Storage, *, allowed_hosts: set[str],
                 static_dir: Path = STATIC_DIR) -> None:
        self.monitor = monitor
        self.storage = storage
        self.allowed_hosts = allowed_hosts
        self.static_dir = static_dir.resolve()
        self.stopping = threading.Event()
        self.routes: list[tuple[str, re.Pattern, Callable[..., Any]]] = []
        route = self._route
        route("GET", r"/api/status", self.get_status)
        route("GET", r"/api/sessions", self.list_sessions)
        route("POST", r"/api/sessions", self.start_session)
        route("POST", r"/api/sessions/stop", self.stop_session)
        route("GET", r"/api/sessions/(\d+)", self.get_session)
        route("DELETE", r"/api/sessions/(\d+)", self.delete_session)
        route("GET", r"/api/sessions/(\d+)/rssi", self.get_rssi)
        route("GET", r"/api/sessions/(\d+)/link", self.get_link)
        route("GET", r"/api/sessions/(\d+)/events", self.get_events)
        route("GET", r"/api/sessions/(\d+)/export/(networks|observations|link|events)\.csv", self.export_csv)

    def _route(self, method: str, pattern: str, func: Callable[..., Any]) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), func))

    def match(self, method: str, path: str) -> tuple[Callable[..., Any], tuple[str, ...]]:
        allowed = False
        for m, pattern, func in self.routes:
            found = pattern.match(path)
            if found:
                if m == method:
                    return func, found.groups()
                allowed = True
        if allowed:
            raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        raise HttpError(HTTPStatus.NOT_FOUND, "not found")

    def _session(self, session_id: str) -> dict[str, Any]:
        session = self.storage.get_session(int(session_id))
        if not session:
            raise HttpError(HTTPStatus.NOT_FOUND, "session not found")
        return session

    # -- handlers (return JSON-serialisable data, or (content_type, bytes, filename)) --

    def get_status(self, query: dict, body: Any) -> Any:
        return self.monitor.status()

    def list_sessions(self, query: dict, body: Any) -> Any:
        return {"sessions": self.storage.list_sessions(), "active": self.monitor.current_session_id()}

    def start_session(self, query: dict, body: Any) -> Any:
        if not isinstance(body, dict):
            raise HttpError(HTTPStatus.BAD_REQUEST, "expected a JSON object")
        fields = {k: body.get(k) for k in ("scan_interval", "link_interval", "ssid", "password", "target", "note")}
        for key in ("ssid", "password", "target", "note"):
            if fields[key] is not None and not isinstance(fields[key], str):
                raise HttpError(HTTPStatus.BAD_REQUEST, f"{key} must be a string")
        try:
            session = self.monitor.start_session(str(body.get("mode", "")), **fields)
        except ValidationError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        return {"session": session}

    def stop_session(self, query: dict, body: Any) -> Any:
        return {"session": self.monitor.stop_session()}

    def get_session(self, query: dict, body: Any, session_id: str) -> Any:
        session = self._session(session_id)
        sid = session["id"]
        scans = self.storage.scans(sid)
        networks = stats.network_stats(scans, self.storage.observations(sid))
        link = None
        if session["mode"] in ("connect", "both"):
            link = stats.link_stats(self.storage.link_samples(sid), self.storage.count_events(sid, "disconnected"))
        durations = [s["duration_ms"] for s in scans if s["duration_ms"]]
        return {
            "session": session,
            "active": sid == self.monitor.current_session_id(),
            "scan_summary": {
                "scans": len(scans),
                "first": scans[0]["ts"] if scans else None,
                "last": scans[-1]["ts"] if scans else None,
                "visible_now": sum(1 for n in networks if n["visible_now"]),
                "ever_seen": len(networks),
                "mean_duration_ms": round(sum(durations) / len(durations)) if durations else None,
            },
            "networks": networks,
            "link": link,
        }

    def delete_session(self, query: dict, body: Any, session_id: str) -> Any:
        session = self._session(session_id)
        if session["id"] == self.monitor.current_session_id():
            raise HttpError(HTTPStatus.CONFLICT, "stop the session before deleting it")
        self.storage.delete_session(session["id"])
        return {"deleted": session["id"]}

    def get_rssi(self, query: dict, body: Any, session_id: str) -> Any:
        sid = self._session(session_id)["id"]
        bssids = [b.strip().lower() for b in query.get("bssids", [""])[0].split(",") if b.strip()][:20]
        scans = self.storage.scans(sid)
        return stats.rssi_series(scans, self.storage.observations(sid, bssids), bssids)

    def get_link(self, query: dict, body: Any, session_id: str) -> Any:
        sid = self._session(session_id)["id"]
        since = None
        if query.get("since"):
            try:
                since = float(query["since"][0])
            except ValueError as exc:
                raise HttpError(HTTPStatus.BAD_REQUEST, "since must be a number") from exc
        samples = self.storage.link_samples(sid, since)
        return {"samples": downsample_link(samples, MAX_LINK_POINTS), "total": len(samples)}

    def get_events(self, query: dict, body: Any, session_id: str) -> Any:
        sid = self._session(session_id)["id"]
        try:
            limit = max(1, min(5000, int(query.get("limit", ["500"])[0])))
        except ValueError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, "limit must be a number") from exc
        return {"events": self.storage.events(sid, limit)}

    def export_csv(self, query: dict, body: Any, session_id: str, kind: str) -> Any:
        sid = self._session(session_id)["id"]
        if kind == "networks":
            networks = stats.network_stats(self.storage.scans(sid), self.storage.observations(sid))
            header = ["ssid", "bssid", "band", "channel", "security", "samples", "seen_pct", "current_rssi",
                      "mean_rssi", "median_rssi", "min_rssi", "max_rssi", "std_rssi", "jitter_db", "dropouts",
                      "longest_gap_scans", "channel_changes", "quality", "stability", "first_seen", "last_seen"]
            rows = [[n[h] if h not in ("first_seen", "last_seen") else iso(n[h]) for h in header] for n in networks]
        elif kind == "observations":
            header = ["time", "ts", "scan_id", "ssid", "bssid", "channel", "band", "rssi", "security", "hidden"]
            rows = [[iso(o["ts"]), o["ts"], o["scan_id"], o["ssid"], o["bssid"], o["channel"],
                     stats.band_for_channel(o["channel"]), o["rssi"], stats.auth_name(o["auth"]), bool(o["hidden"])]
                    for o in self.storage.observations(sid)]
        elif kind == "link":
            header = ["time", "ts", "connected", "rssi", "latency_ms", "ok", "target"]
            rows = [[iso(s["ts"]), s["ts"], s["connected"], s["rssi"], s["latency_ms"], s["ok"], s["target"]]
                    for s in self.storage.link_samples(sid)]
        else:
            header = ["time", "ts", "level", "kind", "message"]
            rows = [[iso(e["ts"]), e["ts"], e["level"], e["kind"], e["message"]]
                    for e in reversed(self.storage.events(sid, 100000))]
        return ("text/csv; charset=utf-8", to_csv(header, rows), f"wifimon-session-{sid}-{kind}.csv")


class Handler(BaseHTTPRequestHandler):
    server_version = "wifimon"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- responses --------------------------------------------------------

    def _headers(self, status: HTTPStatus, content_type: str, length: int | None, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send(self, status: HTTPStatus, content_type: str, data: bytes, extra: dict | None = None) -> None:
        self._headers(status, content_type, len(data), extra)
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        self._send(status, "application/json", json.dumps(payload).encode("utf-8"))

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    # -- request guards ---------------------------------------------------

    def _check_host(self) -> bool:
        """Reject requests for unexpected Host names (protects against DNS rebinding)."""
        host = (self.headers.get("Host") or "").lower()
        if host not in self.app.allowed_hosts:
            self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
            return False
        return True

    def _check_write(self) -> bool:
        """Only accept state changes from the dashboard itself (blocks cross-site requests)."""
        origin = self.headers.get("Origin")
        if origin is not None:
            netloc = urlsplit(origin).netloc.lower()
            if netloc not in self.app.allowed_hosts:
                self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
                return False
        if self.headers.get("X-Requested-With") != "wifimon":
            self._error(HTTPStatus.FORBIDDEN, "missing X-Requested-With header")
            return False
        return True

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        if not length:
            return None
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/json")
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise HttpError(HTTPStatus.BAD_REQUEST, "invalid JSON") from exc

    # -- methods ----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        if not self._check_host():
            return
        url = urlsplit(self.path)
        path = url.path
        try:
            if method == "GET" and path == "/api/stream":
                self._stream()
                return
            if method == "GET" and not path.startswith("/api/"):
                self._static(path)
                return
            if method != "GET" and not self._check_write():
                return
            func, args = self.app.match(method, path)
            body = self._read_body() if method == "POST" else None
            result = func(parse_qs(url.query), body, *args)
            if isinstance(result, tuple):
                content_type, data, filename = result
                self._send(HTTPStatus.OK, content_type, data,
                           {"Content-Disposition": f'attachment; filename="{filename}"'})
            else:
                self._json(HTTPStatus.OK, result)
        except HttpError as exc:
            self._error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("request failed: %s %s", method, self.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (self.app.static_dir / name).resolve()
        if self.app.static_dir not in target.parents or not target.is_file():
            raise HttpError(HTTPStatus.NOT_FOUND, "not found")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "text/javascript"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, content_type, target.read_bytes())

    def _stream(self) -> None:
        q = self.app.monitor.subscribe()
        try:
            self._headers(HTTPStatus.OK, "text/event-stream", None, {"Connection": "keep-alive"})
            self._event("status", self.app.monitor.status())
            while not self.app.stopping.is_set():
                try:
                    kind, data = q.get(timeout=SSE_KEEPALIVE)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._event(kind, data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.monitor.unsubscribe(q)
            self.close_connection = True

    def _event(self, kind: str, data: Any) -> None:
        self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: App) -> None:
        self.app = app
        super().__init__(address, Handler)

    def shutdown(self) -> None:
        self.app.stopping.set()
        super().shutdown()


def allowed_hosts_for(host: str, port: int) -> set[str]:
    names = {host, "127.0.0.1", "localhost", "[::1]"}
    return {f"{name}:{port}".lower() for name in names} | ({h.lower() for h in names} if port == 80 else set())


def make_server(monitor: Monitor, storage: Storage, host: str, port: int) -> Server:
    app = App(monitor, storage, allowed_hosts=set())
    server = Server((host, port), app)
    app.allowed_hosts = allowed_hosts_for(host, server.server_address[1])
    return server
