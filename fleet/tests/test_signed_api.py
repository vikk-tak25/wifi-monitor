"""Signed unit requests over plain HTTP (#9)."""

import base64
import json
import secrets
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from fleet import security
from fleet.app import create_app
from fleet.tables import link_samples

from .conftest import hello_payload, unit_headers


def key_bytes(unit_key: str) -> bytes:
    secret = unit_key.split(".", 1)[1]
    return base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))


def signed(unit_key: str, method: str, path: str, body: bytes, ts: float, nonce: str | None = None) -> dict:
    nonce = nonce or secrets.token_hex(8)
    unit_id = unit_key.split(".")[0]
    sig = security.request_signature(key_bytes(unit_key), method, path, str(int(ts)), nonce, body)
    return {"X-Unit-Id": unit_id, "X-Timestamp": str(int(ts)), "X-Nonce": nonce, "X-Signature": sig,
            "Content-Type": "application/json"}


def post(client, unit_key, path, payload, clock, **kw):
    body = json.dumps(payload).encode()
    headers = signed(unit_key, "POST", path, body, kw.pop("ts", clock.ts), kw.pop("nonce", None))
    headers.update(kw.pop("override", {}))
    return client.post(kw.pop("send_path", path), content=kw.pop("send_body", body), headers=headers)


def check_response_signature(res, unit_key) -> None:
    nonce = res.request.headers["X-Nonce"]
    expected = security.response_signature(key_bytes(unit_key), nonce, res.status_code, res.content)
    assert res.headers["X-Signature"] == expected


@pytest.fixture
def unit(make_unit):
    return make_unit("B-204")


def test_time_endpoint_needs_no_auth(client, clock):
    assert client.get("/api/unit/v1/time").json() == {"server_time_ms": int(clock.ts * 1000)}


def test_signed_hello_and_signed_response(client, unit, clock):
    info, key = unit
    res = post(client, key, "/api/unit/v1/hello", hello_payload(), clock)
    assert res.status_code == 200 and res.json()["unit_id"] == info["id"]
    check_response_signature(res, key)


def test_signed_batch_ping_and_agent(client, unit, clock, engine, settings, tmp_path):
    _, key = unit
    payload = {"batch_id": "s1", "sent_at": clock.ts,
               "samples": [{"t": clock.ts - 1, "connected": True, "rssi": -50}]}
    res = post(client, key, "/api/unit/v1/batch", payload, clock)
    assert res.status_code == 200
    check_response_signature(res, key)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(link_samples)).scalar_one() == 1

    res = client.get("/api/unit/v1/ping", headers=signed(key, "GET", "/api/unit/v1/ping", b"", clock.ts))
    assert res.status_code == 200
    check_response_signature(res, key)

    agent = tmp_path / "app.py"
    agent.write_text('AGENT_VERSION = "9.9.9"\n')
    with TestClient(create_app(replace(settings, agent_path=agent), engine=engine, clock=clock,
                               background=False)) as c:
        res = c.get("/api/unit/v1/agent", headers=signed(key, "GET", "/api/unit/v1/agent", b"", clock.ts))
    assert res.status_code == 200 and res.content == agent.read_bytes()
    check_response_signature(res, key)


@pytest.mark.parametrize("case", ["wrong key", "tampered body", "other path", "stale", "future", "unknown unit",
                                  "bad nonce", "bad timestamp", "no unit id"])
def test_signed_request_rejections(client, unit, clock, make_unit, case):
    _, key = unit
    path = "/api/unit/v1/hello"
    kw = {}
    if case == "wrong key":
        _, other = make_unit("other")
        key = key.split(".")[0] + "." + other.split(".", 1)[1]
    elif case == "tampered body":
        kw["send_body"] = json.dumps(hello_payload(agent_version="6.6.6")).encode()
    elif case == "other path":
        kw["send_path"] = "/api/unit/v1/batch"
    elif case == "stale":
        kw["ts"] = clock.ts - 121
    elif case == "future":
        kw["ts"] = clock.ts + 121
    elif case == "unknown unit":
        kw["override"] = {"X-Unit-Id": "uaaaaaaaaaa"}
    elif case == "bad nonce":
        kw["override"] = {"X-Nonce": "short"}
    elif case == "bad timestamp":
        kw["override"] = {"X-Timestamp": "soon"}
    elif case == "no unit id":
        kw["override"] = {"X-Unit-Id": ""}
    res = post(client, key, path, hello_payload(), clock, **kw)
    assert res.status_code == 401, res.text
    assert res.json()["error"].startswith("signed request rejected")


def test_replayed_request_is_rejected(client, unit, clock):
    _, key = unit
    assert post(client, key, "/api/unit/v1/hello", hello_payload(), clock, nonce="00112233445566778899").status_code == 200
    again = post(client, key, "/api/unit/v1/hello", hello_payload(), clock, nonce="00112233445566778899")
    assert again.status_code == 401 and "nonce already used" in again.json()["error"]


def test_revoked_and_rotated_keys(client, admin, unit, clock):
    info, key = unit
    admin.post(f"/api/admin/units/{info['id']}/revoke")
    assert post(client, key, "/api/unit/v1/hello", hello_payload(), clock).status_code == 401
    new_key = admin.post(f"/api/admin/units/{info['id']}/rotate-key").json()["key"]
    assert new_key != key
    assert post(client, key, "/api/unit/v1/hello", hello_payload(), clock).status_code == 401
    assert post(client, new_key, "/api/unit/v1/hello", hello_payload(), clock).status_code == 200


def test_production_rejects_bearer_over_plain_http(settings, engine, clock, make_unit):
    _, key = make_unit()
    prod = replace(settings, environment="production")
    app = create_app(prod, engine=engine, clock=clock, background=False)
    with TestClient(app) as c:
        res = c.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key))
        assert res.status_code == 401 and "signed requests" in res.json()["error"]
        assert post(c, key, "/api/unit/v1/hello", hello_payload(), clock).status_code == 200
    with TestClient(app, base_url="https://testserver") as c:
        assert c.post("/api/unit/v1/hello", json=hello_payload(), headers=unit_headers(key)).status_code == 200


def test_key_is_derived_not_stored(admin, engine, settings):
    from fleet.tables import units

    res = admin.post("/api/admin/units", json={"name": "X"}).json()
    with engine.connect() as conn:
        row = conn.execute(select(units).where(units.c.id == res["unit"]["id"])).first()
    assert row.key_version == 1
    assert security.unit_key_for(settings.secret_key, row.id, 1) == res["key"]
    assert res["key"].split(".", 1)[1] not in str(dict(row._mapping))
