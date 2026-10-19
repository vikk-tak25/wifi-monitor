"""FastAPI application: unit API (handshake, batches), admin API and health check."""


import asyncio
import hashlib
import logging
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Callable, TypeVar

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, and_, select, text

from . import analytics, floors, ingest, provisioning, sessions, units
from .agentbundle import AgentSource
from .config import Settings
from .db import from_epoch, make_engine, utcnow
from .schemas import (Batch, FloorUpdate, Hello, LabelTemplate, Login, Placement, ProvisionRequest, UnitCreate,
                      UnitUpdate, WifiUpdate)
from .security import LoginThrottle, NonceCache, response_signature
from .tables import events

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
# The dashboard is static files from this server only; nothing inline, nothing third-party. The one
# exception is the <style> element lightweight-charts injects for its TradingView attribution logo,
# allowed by its exact hash (tests recompute it from the vendored file).
CHART_LOGO_STYLE_HASH = "sha256-3pRED1tOXas1FXFoPb9TGCjmYe9XQsmO9OV23khV2nY="
PAGE_CSP = ("default-src 'self'; script-src 'self'; "
            f"style-src 'self' '{CHART_LOGO_STYLE_HASH}'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
HELLO_MAX_BYTES = 8 * 1024
# How long room statistics may be reused. The 7-day comparison takes about 2 s over 50 units, so it
# is also refreshed in the background (see housekeeping) and requests never wait for it (#5).
ROOM_STATS_TTL_S = {"1h": 30, "24h": 60, "7d": 300}
ROOM_STATS_REFRESH_7D_S = 240
BATCH_MAX_BYTES = 512 * 1024
HOUSEKEEPING_INTERVAL_S = 30.0

M = TypeVar("M", bound=BaseModel)


class ApiError(HTTPException):
    def __init__(self, status_code: int, detail: Any = None, headers: dict | None = None,
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(status_code, detail, headers)
        self.extra = extra or {}


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    return [{"loc": [str(p) for p in err.get("loc", ())], "msg": err.get("msg", "")} for err in exc.errors()[:20]]


def asset_version(static_dir: Path) -> str:
    """Hash of every dashboard file: changes whenever a deploy changes any of them."""
    digest = hashlib.sha256()
    for path in sorted(p for p in static_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(static_dir).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def versioned_page(static_dir: Path) -> str:
    """index.html with ?v=<version> on every /static/ reference, so a deploy replaces cached copies (#12)."""
    version = asset_version(static_dir)
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    return re.sub(r'((?:src|href)="/static/[^"?]+)"', rf'\1?v={version}"', html)


def create_app(settings: Settings, *, engine: Engine | None = None,
               clock: Callable[[], datetime] = utcnow, background: bool = True) -> FastAPI:
    engine = engine or make_engine(settings.database_url)
    throttle = LoginThrottle()
    room_stats_cache: dict[str, tuple[datetime, dict]] = {}
    room_stats_lock = threading.Lock()

    def room_stats(window: str, max_age_s: float | None = None) -> dict:
        if window not in analytics.WINDOWS:
            raise ApiError(422, f"window must be one of: {', '.join(analytics.WINDOWS)}")
        limit = ROOM_STATS_TTL_S[window] if max_age_s is None else max_age_s
        with room_stats_lock:  # one computation at a time; others reuse its result
            cached = room_stats_cache.get(window)
            now = clock()
            if cached and (now - cached[0]).total_seconds() < limit:
                return cached[1]
            with engine.connect() as conn:
                result = analytics.room_stats(conn, window, now)
            room_stats_cache[window] = (now, result)
            return result
    nonces = NonceCache()
    agent_source = AgentSource(settings.agent_path)

    def housekeeping() -> None:
        with engine.begin() as conn:
            units.mark_offline_units(conn, settings, clock())
            sessions.purge_expired(conn, clock())
        room_stats("7d", max_age_s=ROOM_STATS_REFRESH_7D_S)

    async def housekeeping_loop() -> None:
        while True:
            try:
                await run_in_threadpool(housekeeping)
            except Exception:
                log.exception("housekeeping failed")
            await asyncio.sleep(HOUSEKEEPING_INTERVAL_S)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.admin_password_hash:
            log.warning("FLEET_ADMIN_PASSWORD_HASH is not set: development admin password is %r",
                        sessions.DEV_ADMIN_PASSWORD)
        task = asyncio.create_task(housekeeping_loop()) if background else None
        yield
        if task:
            task.cancel()
        engine.dispose()

    production = settings.is_production
    app = FastAPI(title="Wi-Fi fleet server", lifespan=lifespan,
                  docs_url=None if production else "/api/docs", redoc_url=None,
                  openapi_url=None if production else "/api/openapi.json")
    app.state.settings = settings
    app.state.engine = engine
    app.state.housekeeping = housekeeping

    # -- errors and headers -------------------------------------------------

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail, **getattr(exc, "extra", {})}, status_code=exc.status_code,
                            headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "invalid request", "details": _validation_details(exc)}, status_code=422)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith("/static/"):
            # Browsers must ask before reusing a file (a cheap 304 when unchanged), or they keep
            # running old dashboard code after a deploy (#12).
            response.headers["Cache-Control"] = "no-cache"
        else:
            response.headers.setdefault("Content-Security-Policy", PAGE_CSP)
        if settings.secure_cookies:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    # -- helpers ------------------------------------------------------------

    def client_ip(request: Request) -> str | None:
        return request.client.host if request.client else None

    async def read_body(request: Request, limit: int) -> bytes:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ApiError(413, f"request body larger than {limit} bytes")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise ApiError(413, f"request body larger than {limit} bytes")
        return bytes(body)

    def parse(model: type[M], body: bytes) -> M:
        try:
            return model.model_validate_json(body)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc

    def check_write(request: Request) -> None:
        """Block cross-site writes: require our custom header and a same-origin Origin if present."""
        if request.headers.get("x-requested-with") != "fleet":
            raise ApiError(403, "missing X-Requested-With header")
        origin = request.headers.get("origin")
        if origin is not None:
            own = f"{request.url.scheme}://{request.headers.get('host', '')}".lower()
            if origin.lower() not in (settings.public_origin, own):
                raise ApiError(403, "cross-origin request refused")

    def require_admin(request: Request) -> None:
        token = request.cookies.get(sessions.COOKIE_NAME)
        with engine.connect() as conn:
            if not sessions.session_valid(conn, token, clock()):
                raise ApiError(401, "login required")
        if request.method not in ("GET", "HEAD"):
            check_write(request)

    Admin = Annotated[None, Depends(require_admin)]

    class UnitAuth:
        """An authenticated unit; `key` and `nonce` are set for signed requests (#9)."""

        def __init__(self, unit, key: bytes | None = None, nonce: str | None = None) -> None:
            self.unit, self.key, self.nonce = unit, key, nonce

    def unit_or_401(conn, request: Request, body: bytes = b"") -> "UnitAuth":
        if "x-signature" in request.headers:
            path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            try:
                unit, key, nonce = units.authenticate_signed(
                    conn, settings, nonces, method=request.method, path=path, headers=request.headers,
                    body=body, now=clock())
            except units.SignedRequestError as exc:
                raise ApiError(401, f"signed request rejected: {exc}") from exc
            return UnitAuth(unit, key, nonce)
        if settings.is_production and request.url.scheme != "https":
            # A bearer key over plain HTTP would cross the network readable; units sign instead.
            raise ApiError(401, "use signed requests over plain HTTP", headers={"WWW-Authenticate": "Signature"})
        unit = units.authenticate(conn, settings, request.headers.get("authorization"))
        if unit is None:
            raise ApiError(401, "invalid or revoked unit key", headers={"WWW-Authenticate": "Bearer"})
        return UnitAuth(unit)

    def unit_response(auth: "UnitAuth", content: Any = None, *, data: bytes | None = None,
                      media_type: str = "application/json", headers: dict | None = None) -> Response:
        """A response to a unit, signed when the request was signed."""
        body = data if data is not None else JSONResponse(content).body
        out = dict(headers or {})
        if auth.key is not None:
            out["X-Signature"] = response_signature(auth.key, auth.nonce, 200, body)
        return Response(body, media_type=media_type, headers=out)

    # -- health -----------------------------------------------------------

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            log.error("health check: database unavailable: %s", exc)
            raise ApiError(503, "database unavailable") from exc
        return {"status": "ok"}

    static_dir = STATIC_DIR
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    page = versioned_page(static_dir)

    @app.get("/", include_in_schema=False)
    def dashboard() -> Response:
        return Response(page, media_type="text/html", headers={"Cache-Control": "no-cache"})

    # -- unit API ---------------------------------------------------------

    @app.get("/api/unit/v1/time")
    def unit_time() -> dict[str, Any]:
        """Unauthenticated: lets a unit set its clock before signing its first request."""
        return {"server_time_ms": ingest.server_ms(clock())}

    @app.post("/api/unit/v1/hello")
    async def unit_hello(request: Request) -> Response:
        body = await read_body(request, HELLO_MAX_BYTES)

        def work() -> Response:
            with engine.begin() as conn:
                auth = unit_or_401(conn, request, body)
                result = ingest.handle_hello(conn, settings, auth.unit, parse(Hello, body), client_ip(request),
                                             clock(), agent_source.get())
            return unit_response(auth, result)

        return await run_in_threadpool(work)

    @app.post("/api/unit/v1/batch")
    async def unit_batch(request: Request) -> Response:
        body = await read_body(request, BATCH_MAX_BYTES)

        def work() -> Response:
            with engine.begin() as conn:
                auth = unit_or_401(conn, request, body)
                result = ingest.store_batch(conn, settings, auth.unit, parse(Batch, body), client_ip(request),
                                            clock())
            return unit_response(auth, result)

        return await run_in_threadpool(work)

    @app.get("/api/unit/v1/ping")
    def unit_ping(request: Request) -> Response:
        with engine.connect() as conn:
            auth = unit_or_401(conn, request)
        now = clock()
        return unit_response(auth, {"server_time": now.timestamp(), "server_time_ms": ingest.server_ms(now)})

    @app.get("/api/unit/v1/agent")
    def unit_agent(request: Request) -> Response:
        with engine.connect() as conn:
            auth = unit_or_401(conn, request)
        bundle = agent_source.get()
        if bundle is None:
            raise ApiError(404, "agent updates are not available")
        return unit_response(auth, data=bundle.data, media_type="text/x-python",
                             headers={"X-Agent-Version": bundle.version, "X-Agent-SHA256": bundle.sha256})

    # -- admin: session -----------------------------------------------------

    @app.post("/api/admin/login")
    def login(data: Login, request: Request, response: Response) -> dict[str, Any]:
        check_write(request)
        who = client_ip(request) or "unknown"
        wait = throttle.retry_after(who)
        if wait > 0:
            raise ApiError(429, f"too many failed logins, try again in {int(wait) + 1} s",
                           headers={"Retry-After": str(int(wait) + 1)})
        if not sessions.check_admin_password(settings, data.password):
            throttle.failed(who)
            raise ApiError(401, "wrong password")
        throttle.succeeded(who)
        with engine.begin() as conn:
            token = sessions.create_session(conn, settings, who, clock())
        response.set_cookie(sessions.COOKIE_NAME, token, max_age=int(settings.session_hours * 3600),
                            httponly=True, samesite="strict", secure=settings.secure_cookies, path="/")
        return {"ok": True}

    @app.post("/api/admin/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        check_write(request)
        with engine.begin() as conn:
            sessions.end_session(conn, request.cookies.get(sessions.COOKIE_NAME))
        response.delete_cookie(sessions.COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/admin/me")
    def me(_: Admin) -> dict[str, Any]:
        return {"admin": True, "environment": settings.environment, "public_url": settings.public_url}

    # -- admin: units ---------------------------------------------------------

    @app.get("/api/admin/settings")
    def get_settings(_: Admin) -> dict[str, Any]:
        return {"unit_defaults": settings.unit_defaults.as_dict(), "offline_after_s": settings.offline_after_s,
                "public_url": settings.public_url}

    @app.get("/api/admin/units")
    def list_units(_: Admin) -> dict[str, Any]:
        with engine.connect() as conn:
            return {"units": [units.public_unit(u, settings) for u in units.list_units(conn)]}

    @app.post("/api/admin/units", status_code=201)
    def create_unit(data: UnitCreate, _: Admin) -> dict[str, Any]:
        with engine.begin() as conn:
            unit, key = units.create_unit(conn, settings, data, clock())
        return {"unit": units.public_unit(unit, settings), "key": key, "server_url": settings.public_url}

    @app.get("/api/admin/units/{unit_id}")
    def get_unit(unit_id: str, _: Admin) -> dict[str, Any]:
        with engine.connect() as conn:
            unit = units.get_unit(conn, unit_id)
        if unit is None:
            raise ApiError(404, "unit not found")
        return {"unit": units.public_unit(unit, settings)}

    @app.patch("/api/admin/units/{unit_id}")
    def update_unit(unit_id: str, data: UnitUpdate, _: Admin) -> dict[str, Any]:
        with engine.begin() as conn:
            unit = units.update_unit(conn, unit_id, data, clock())
        if unit is None:
            raise ApiError(404, "unit not found")
        return {"unit": units.public_unit(unit, settings)}

    @app.post("/api/admin/units/{unit_id}/rotate-key")
    def rotate_key(unit_id: str, _: Admin) -> dict[str, Any]:
        with engine.begin() as conn:
            key = units.rotate_key(conn, settings, unit_id, clock())
            unit = units.get_unit(conn, unit_id) if key else None
        if unit is None:
            raise ApiError(404, "unit not found")
        return {"unit": units.public_unit(unit, settings), "key": key, "server_url": settings.public_url}

    @app.delete("/api/admin/units/{unit_id}")
    def delete_unit(unit_id: str, _: Admin) -> dict[str, Any]:
        with engine.begin() as conn:
            name = units.delete_unit(conn, unit_id, clock())
        if name is None:
            raise ApiError(404, "unit not found")
        return {"deleted": unit_id}

    @app.post("/api/admin/units/{unit_id}/revoke")
    def revoke(unit_id: str, _: Admin) -> dict[str, Any]:
        with engine.begin() as conn:
            unit = units.revoke_unit(conn, unit_id, clock())
        if unit is None:
            raise ApiError(404, "unit not found")
        return {"unit": units.public_unit(unit, settings)}

    # -- admin: dashboard data (#5) ------------------------------------------------

    def _query(func, *args):
        try:
            with engine.connect() as conn:
                return func(conn, *args)
        except analytics.QueryError as exc:
            raise ApiError(422, str(exc)) from exc

    @app.get("/api/admin/rooms")
    def rooms(_: Admin, window: str = "24h") -> dict[str, Any]:
        computed = room_stats(window)
        with engine.connect() as conn:
            all_units = [units.public_unit(u, settings) for u in units.list_units(conn)]
            return analytics.rooms(conn, all_units, window, clock(), computed)

    @app.get("/api/admin/units/{unit_id}/summary")
    def unit_summary(unit_id: str, _: Admin, window: str = "24h") -> dict[str, Any]:
        with engine.connect() as conn:
            unit = units.get_unit(conn, unit_id)
        if unit is None:
            raise ApiError(404, "unit not found")
        return {"unit": units.public_unit(unit, settings),
                **_query(analytics.unit_summary, unit_id, window, clock())}

    @app.get("/api/admin/units/{unit_id}/candles")
    def unit_candles(unit_id: str, _: Admin, metric: str = "rssi", bucket: str = "1h",
                     start: Annotated[float | None, Query(alias="from")] = None,
                     end: Annotated[float | None, Query(alias="to")] = None) -> dict[str, Any]:
        with engine.connect() as conn:
            if units.get_unit(conn, unit_id) is None:
                raise ApiError(404, "unit not found")
        if bucket not in analytics.BUCKETS:
            raise ApiError(422, f"bucket must be one of: {', '.join(analytics.BUCKETS)}")
        now = clock()
        to = from_epoch(end) if end is not None else now
        frm = from_epoch(start) if start is not None else to - analytics.DEFAULT_RANGE[bucket]
        data = _query(analytics.candles, unit_id, metric, bucket, frm, to, settings.timezone)
        return {"metric": metric, "bucket": bucket, "from": frm.timestamp(), "to": to.timestamp(),
                "timezone": settings.timezone, "candles": data}

    # -- admin: floor plans (#6) ------------------------------------------------------

    def _floors(func, *args):
        try:
            with engine.begin() as conn:
                return func(conn, *args)
        except floors.FloorError as exc:
            raise ApiError(422, str(exc)) from exc

    @app.get("/api/admin/floors")
    def list_floors(_: Admin) -> dict[str, Any]:
        with engine.connect() as conn:
            return {"floors": floors.list_floors(conn)}

    @app.post("/api/admin/floors", status_code=201)
    async def create_floor(request: Request, _: Admin, name: str = "") -> dict[str, Any]:
        data = await read_body(request, floors.MAX_IMAGE_BYTES + 1)
        floor = await run_in_threadpool(_floors, floors.create_floor, name, data, clock())
        return {"floor": floor}

    @app.patch("/api/admin/floors/{floor_id}")
    def update_floor(floor_id: int, data: FloorUpdate, _: Admin) -> dict[str, Any]:
        floor = _floors(floors.update_floor, floor_id, data.name, data.sort_order, clock())
        if floor is None:
            raise ApiError(404, "floor plan not found")
        return {"floor": floor}

    @app.put("/api/admin/floors/{floor_id}/image")
    async def replace_floor_image(floor_id: int, request: Request, _: Admin) -> dict[str, Any]:
        data = await read_body(request, floors.MAX_IMAGE_BYTES + 1)
        floor = await run_in_threadpool(_floors, floors.replace_image, floor_id, data, clock())
        if floor is None:
            raise ApiError(404, "floor plan not found")
        return {"floor": floor}

    @app.delete("/api/admin/floors/{floor_id}")
    def delete_floor(floor_id: int, _: Admin) -> dict[str, Any]:
        if not _floors(floors.delete_floor, floor_id, clock()):
            raise ApiError(404, "floor plan not found")
        return {"deleted": floor_id}

    @app.get("/api/admin/floors/{floor_id}/image")
    def floor_image(floor_id: int, _: Admin) -> Response:
        with engine.connect() as conn:
            found = floors.floor_image(conn, floor_id)
        if found is None:
            raise ApiError(404, "floor plan not found")
        data, content_type, updated = found
        headers = {"Cache-Control": "private, max-age=3600", "ETag": f'"{floor_id}-{int(updated.timestamp())}"',
                   "Content-Disposition": "inline"}
        if content_type == "image/svg+xml":
            # Opened directly, an SVG is a document: no scripts, no external loads, its own origin.
            headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
        return Response(data, media_type=content_type, headers=headers)

    @app.put("/api/admin/placements/{unit_id}")
    def place_unit(unit_id: str, data: Placement, _: Admin) -> dict[str, Any]:
        return {"placement": _floors(floors.place_unit, unit_id, data.floor_id, data.x, data.y, clock())}

    @app.delete("/api/admin/placements/{unit_id}")
    def remove_placement(unit_id: str, _: Admin) -> dict[str, Any]:
        if not _floors(floors.remove_placement, unit_id):
            raise ApiError(404, "unit is not placed on a floor plan")
        return {"removed": unit_id}

    # -- admin: provisioning (#4) ---------------------------------------------------

    def _provisioning(func, *args, write: bool = False):
        try:
            with (engine.begin() if write else engine.connect()) as conn:
                return func(conn, *args)
        except provisioning.BoardAlreadyRegistered as exc:
            raise ApiError(409, str(exc), extra={"existing_units": exc.existing}) from exc
        except provisioning.ProvisioningError as exc:
            raise ApiError(409, str(exc)) from exc

    @app.get("/api/admin/provisioning")
    def get_provisioning(_: Admin) -> dict[str, Any]:
        with engine.connect() as conn:
            wifi = provisioning.wifi_public(conn)
        return {"wifi": wifi, "unit_url": settings.effective_unit_url,
                "firmware": provisioning.firmware_public(settings)}

    @app.put("/api/admin/provisioning/wifi")
    def put_wifi(data: WifiUpdate, _: Admin) -> dict[str, Any]:
        return {"wifi": _provisioning(provisioning.update_wifi, settings, data, clock(), write=True)}

    @app.get("/api/admin/label-template")
    def get_label_template(_: Admin) -> dict[str, Any]:
        return {"template": _provisioning(provisioning.label_template)}

    @app.put("/api/admin/label-template")
    def put_label_template(data: LabelTemplate, _: Admin) -> dict[str, Any]:
        return {"template": _provisioning(provisioning.update_label_template, data, clock(), write=True)}

    @app.post("/api/admin/provision")
    def provision_unit(data: ProvisionRequest, _: Admin) -> dict[str, Any]:
        return _provisioning(provisioning.provision, settings, data, clock(), write=True)

    @app.get("/api/admin/provisioning/files/{name}")
    def unit_file(name: str, _: Admin) -> Response:
        try:
            data = provisioning.unit_file(settings, name)
        except provisioning.ProvisioningError as exc:
            raise ApiError(404, str(exc)) from exc
        return Response(data, media_type="text/x-python")

    @app.get("/api/admin/provisioning/firmware.bin")
    def firmware(_: Admin) -> FileResponse:
        if not provisioning.firmware_available(settings):
            raise ApiError(404, "firmware is not on the server; run `fleet fetch-firmware`")
        return FileResponse(provisioning.firmware_path(settings), media_type="application/octet-stream",
                            headers={"X-Firmware-Version": provisioning.FIRMWARE.version,
                                     "X-Firmware-SHA256": provisioning.FIRMWARE.sha256})

    # -- admin: events ----------------------------------------------------------

    @app.get("/api/admin/events")
    def list_events(_: Admin,
                    unit_id: str | None = None,
                    level: Annotated[str | None, Query(pattern="^(info|success|warning|error)$")] = None,
                    kind: Annotated[str | None, Query(max_length=32)] = None,
                    before: float | None = None,
                    limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> dict[str, Any]:
        conditions = []
        if unit_id:
            conditions.append(events.c.unit_id == unit_id)
        if level:
            conditions.append(events.c.level == level)
        if kind:
            conditions.append(events.c.kind == kind)
        if before is not None:
            conditions.append(events.c.time < from_epoch(before))
        query = select(events).order_by(events.c.time.desc(), events.c.id.desc()).limit(limit)
        if conditions:
            query = query.where(and_(*conditions))
        with engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        return {"events": [dict(r) for r in rows]}

    return app
