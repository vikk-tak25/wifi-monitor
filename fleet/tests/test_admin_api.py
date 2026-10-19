from sqlalchemy import select

from fleet.tables import admin_sessions, units

from .conftest import ADMIN_PASSWORD, WRITE_HEADERS, hello_payload, unit_headers


# -- login and sessions -------------------------------------------------------------

def test_admin_endpoints_require_login(client):
    for method, path in (("get", "/api/admin/me"), ("get", "/api/admin/units"), ("get", "/api/admin/events"),
                         ("post", "/api/admin/units"), ("get", "/api/admin/settings")):
        res = getattr(client, method)(path, headers=WRITE_HEADERS)
        assert res.status_code == 401, path
        assert res.json() == {"error": "login required"}


def test_login_sets_secure_session_cookie(client, engine):
    res = client.post("/api/admin/login", json={"password": ADMIN_PASSWORD}, headers=WRITE_HEADERS)
    assert res.status_code == 200
    cookie = res.headers["set-cookie"]
    assert "fleet_session=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Max-Age=43200" in cookie
    token = client.cookies["fleet_session"]
    with engine.connect() as conn:
        stored = conn.execute(select(admin_sessions.c.token_hash)).scalars().all()
    assert len(stored) == 1 and stored[0] != token  # only a hash is stored
    assert client.get("/api/admin/me").json()["admin"] is True


def test_wrong_password_and_throttling(client):
    for _ in range(5):
        res = client.post("/api/admin/login", json={"password": "nope"}, headers=WRITE_HEADERS)
        assert res.status_code == 401 and res.json()["error"] == "wrong password"
    res = client.post("/api/admin/login", json={"password": "nope"}, headers=WRITE_HEADERS)
    assert res.status_code == 401
    res = client.post("/api/admin/login", json={"password": ADMIN_PASSWORD}, headers=WRITE_HEADERS)
    assert res.status_code == 429 and "Retry-After" in res.headers


def test_login_requires_csrf_header(client):
    res = client.post("/api/admin/login", json={"password": ADMIN_PASSWORD})
    assert res.status_code == 403


def test_cross_origin_writes_are_refused(admin):
    res = admin.post("/api/admin/units", json={"name": "X"}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403 and res.json()["error"] == "cross-origin request refused"
    res = admin.post("/api/admin/units", json={"name": "X"}, headers={"Origin": "http://testserver"})
    assert res.status_code == 201
    res = admin.post("/api/admin/units", json={"name": "Y"}, headers={"X-Requested-With": ""})
    assert res.status_code == 403


def test_logout_ends_session(admin):
    token = admin.cookies["fleet_session"]
    assert admin.post("/api/admin/logout").status_code == 200
    admin.cookies.set("fleet_session", token)
    assert admin.get("/api/admin/me").status_code == 401


def test_session_expires(admin, clock):
    clock.advance(hours=12, seconds=1)
    assert admin.get("/api/admin/me").status_code == 401


def test_development_default_password_only_without_hash(settings, engine, clock):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from fleet.app import create_app

    dev = replace(settings, admin_password_hash=None)
    with TestClient(create_app(dev, engine=engine, clock=clock, background=False)) as c:
        assert c.post("/api/admin/login", json={"password": "admin"}, headers=WRITE_HEADERS).status_code == 200
    prod = replace(settings, admin_password_hash=None, environment="production")
    with TestClient(create_app(prod, engine=engine, clock=clock, background=False)) as c:
        assert c.post("/api/admin/login", json={"password": "admin"}, headers=WRITE_HEADERS).status_code == 401


# -- units ------------------------------------------------------------------------------

def test_create_unit_returns_key_once(admin, engine):
    res = admin.post("/api/admin/units", json={"name": "  B-204  ", "notes": "by the window",
                                               "config": {"scan_interval_s": 900}})
    assert res.status_code == 201
    body = res.json()
    unit, key = body["unit"], body["key"]
    assert body["server_url"] == "http://testserver"
    assert unit["name"] == "B-204" and unit["status"] == "never_seen" and unit["online"] is False
    assert key.startswith(unit["id"] + ".")
    assert unit["effective_config"]["scan_interval_s"] == 900
    assert unit["effective_config"]["sample_interval_s"] == 5.0
    assert "key_hash" not in unit and key not in str(admin.get("/api/admin/units").json())
    with engine.connect() as conn:
        assert conn.execute(select(units.c.key_hash)).scalar_one() != key


def test_list_get_and_update_units(admin, make_unit):
    a, _ = make_unit("A-101")
    b, _ = make_unit("B-204")
    names = [u["name"] for u in admin.get("/api/admin/units").json()["units"]]
    assert names == ["A-101", "B-204"]
    assert admin.get(f"/api/admin/units/{a['id']}").json()["unit"]["name"] == "A-101"

    res = admin.patch(f"/api/admin/units/{b['id']}", json={"name": "B-205", "mac": "02:00:00:C5:00:01",
                                                           "config": {"sample_interval_s": 10}})
    unit = res.json()["unit"]
    assert unit["name"] == "B-205" and unit["mac"] == "02:00:00:c5:00:01"
    assert unit["effective_config"]["sample_interval_s"] == 10
    events = admin.get(f"/api/admin/events?unit_id={b['id']}&kind=unit_renamed").json()["events"]
    assert events[0]["message"] == "Unit renamed from B-204 to B-205"

    res = admin.patch(f"/api/admin/units/{b['id']}", json={"config": None})
    assert res.json()["unit"]["config"] == {}


def test_unit_validation_errors(admin, make_unit):
    unit, _ = make_unit()
    assert admin.post("/api/admin/units", json={"name": "   "}).status_code == 422
    assert admin.post("/api/admin/units", json={"name": "x" * 101}).status_code == 422
    assert admin.post("/api/admin/units", json={"name": "A", "mac": "nope"}).status_code == 422
    assert admin.post("/api/admin/units", json={"name": "A", "config": {"scan_interval_s": -1}}).status_code == 422
    assert admin.post("/api/admin/units", json={"name": "A", "config": {"ping_target": "a b"}}).status_code == 422
    assert admin.post("/api/admin/units", json={"name": "A", "role": "x"}).status_code == 422
    res = admin.patch(f"/api/admin/units/{unit['id']}", json={"config": {"batch_interval_s": 1}})
    assert res.status_code == 422 and res.json()["details"][0]["loc"][-1] == "batch_interval_s"
    assert admin.get("/api/admin/units/unope").status_code == 404
    assert admin.patch("/api/admin/units/unope", json={"name": "x"}).status_code == 404


def test_revoke_blocks_unit_and_rotate_restores(admin, client, make_unit):
    unit, key = make_unit()
    assert client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key)).status_code == 200

    revoked = admin.post(f"/api/admin/units/{unit['id']}/revoke").json()["unit"]
    assert revoked["status"] == "revoked" and revoked["online"] is False
    assert client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key)).status_code == 401

    res = admin.post(f"/api/admin/units/{unit['id']}/rotate-key").json()
    new_key = res["key"]
    assert new_key != key and res["unit"]["revoked_at"] is None
    assert client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key)).status_code == 401
    assert client.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(new_key)).status_code == 200
    assert admin.post("/api/admin/units/unope/revoke").status_code == 404
    assert admin.post("/api/admin/units/unope/rotate-key").status_code == 404


def test_events_filters_and_paging(admin, make_unit, clock):
    a, _ = make_unit("A-101")
    clock.advance(minutes=1)
    b, _ = make_unit("B-204")
    clock.advance(minutes=1)
    admin.post(f"/api/admin/units/{b['id']}/revoke")
    all_events = admin.get("/api/admin/events").json()["events"]
    assert [e["kind"] for e in all_events] == ["unit_revoked", "unit_created", "unit_created"]
    assert [e["kind"] for e in admin.get("/api/admin/events?level=warning").json()["events"]] == ["unit_revoked"]
    assert len(admin.get(f"/api/admin/events?unit_id={a['id']}").json()["events"]) == 1
    before = clock.ts - 90
    assert [e["unit_id"] for e in admin.get(f"/api/admin/events?before={before}").json()["events"]] == [a["id"]]
    assert len(admin.get("/api/admin/events?limit=1").json()["events"]) == 1
    assert admin.get("/api/admin/events?level=panic").status_code == 422
    assert admin.get("/api/admin/events?limit=5000").status_code == 422


def test_settings_endpoint(admin):
    data = admin.get("/api/admin/settings").json()
    assert data["unit_defaults"]["batch_interval_s"] == 60.0 and data["offline_after_s"] == 600.0


def test_hsts_only_over_https(settings, engine, clock):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from fleet.app import create_app

    with TestClient(create_app(replace(settings, public_url="https://wifi.example"), engine=engine, clock=clock,
                               background=False)) as c:
        assert c.get("/healthz").headers["Strict-Transport-Security"] == "max-age=31536000"


def test_forwarded_headers_from_trusted_proxy_are_used(settings, engine, clock, make_unit):
    """With uvicorn's proxy middleware (as `fleet serve` enables it), the unit IP comes from Caddy."""
    from fastapi.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from fleet.app import create_app

    from .conftest import hello_payload, unit_headers

    _, key = make_unit()
    app = ProxyHeadersMiddleware(create_app(settings, engine=engine, clock=clock, background=False),
                                 trusted_hosts="testclient")
    with TestClient(app) as c:
        c.post("/api/unit/v1/hello", json=hello_payload(),
               headers={**unit_headers(key), "X-Forwarded-For": "203.0.113.10", "X-Forwarded-Proto": "https"})
    with engine.connect() as conn:
        assert conn.execute(select(units.c.last_ip)).scalar_one() == "203.0.113.10"


def test_health_and_headers(client):
    res = client.get("/healthz")
    assert res.status_code == 200 and res.json() == {"status": "ok"}
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/api/admin/me").headers["Cache-Control"] == "no-store"
