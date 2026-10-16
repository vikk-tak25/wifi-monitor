"""FastAPI application: unit API (handshake, batches), admin API and health check."""


import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Callable, TypeVar

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, and_, select, text

from . import ingest, sessions, units
from .agentbundle import AgentSource
from .config import Settings
from .db import from_epoch, make_engine, utcnow
from .schemas import Batch, Hello, Login, UnitCreate, UnitUpdate
from .security import LoginThrottle, NonceCache, response_signature
from .tables import events

log = logging.getLogger(__name__)

HELLO_MAX_BYTES = 8 * 1024
BATCH_MAX_BYTES = 512 * 1024
HOUSEKEEPING_INTERVAL_S = 30.0

M = TypeVar("M", bound=BaseModel)


class ApiError(HTTPException):
    pass


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    return [{"loc": [str(p) for p in err.get("loc", ())], "msg": err.get("msg", "")} for err in exc.errors()[:20]]


def create_app(settings: Settings, *, engine: Engine | None = None,
               clock: Callable[[], datetime] = utcnow, background: bool = True) -> FastAPI:
    engine = engine or make_engine(settings.database_url)
    throttle = LoginThrottle()
    nonces = NonceCache()
    agent_source = AgentSource(settings.agent_path)

    def housekeeping() -> None:
        with engine.begin() as conn:
            units.mark_offline_units(conn, settings, clock())
            sessions.purge_expired(conn, clock())

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
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

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

    @app.get("/")
    def root() -> dict[str, Any]:
        return {"service": "wifi-fleet", "environment": settings.environment}

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

    @app.post("/api/admin/units/{unit_id}/revoke")
    def revoke(unit_id: str, _: Admin) -> dict[str, Any]:
        with engine.begin() as conn:
            unit = units.revoke_unit(conn, unit_id, clock())
        if unit is None:
            raise ApiError(404, "unit not found")
        return {"unit": units.public_unit(unit, settings)}

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
