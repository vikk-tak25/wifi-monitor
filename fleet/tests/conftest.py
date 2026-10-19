"""Test fixtures. Tests run against a real TimescaleDB database.

Start it with `docker compose up -d db`. The fixtures create (and recreate) a separate
`fleet_test` database on that server, so development data is never touched.
Override the server with TEST_DATABASE_URL (it must point at an admin-capable database).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from fleet.app import create_app
from fleet.cli import migrate
from fleet.config import Settings
from fleet.db import make_engine
from fleet.security import hash_password

BASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://fleet:fleet-dev@127.0.0.1:5433/fleet")
TEST_DB = "fleet_test"
ADMIN_PASSWORD = "correct horse battery"
TABLES = ("events", "ingest_batches", "link_samples", "scans", "scan_observations", "admin_sessions", "units",
          "settings", "unit_placements", "floor_plans")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now

    @property
    def ts(self) -> float:
        return self.now.timestamp()


@pytest.fixture(scope="session")
def database_url() -> str:
    admin = create_engine(BASE_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    except Exception as exc:  # pragma: no cover - environment problem, not a test failure
        pytest.exit(f"Cannot reach the test database at {BASE_URL}: {exc}\n"
                    f"Start it with: docker compose up -d db", returncode=2)
    finally:
        admin.dispose()
    url = make_url(BASE_URL).set(database=TEST_DB).render_as_string(hide_password=False)
    migrate(url)
    return url


@pytest.fixture(scope="session")
def engine(database_url):
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_tables(engine):
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="session")
def admin_hash() -> str:
    return hash_password(ADMIN_PASSWORD, n=2 ** 10)


@pytest.fixture
def settings(database_url, admin_hash) -> Settings:
    return Settings(database_url=database_url, secret_key="test-secret-key-" + "x" * 32,
                    admin_password_hash=admin_hash, public_url="http://testserver")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(settings, engine, clock):
    app = create_app(settings, engine=engine, clock=clock, background=False)
    with TestClient(app) as c:
        yield c


WRITE_HEADERS = {"X-Requested-With": "fleet"}


@pytest.fixture
def admin(client):
    res = client.post("/api/admin/login", json={"password": ADMIN_PASSWORD}, headers=WRITE_HEADERS)
    assert res.status_code == 200, res.text
    client.headers.update(WRITE_HEADERS)
    return client


@pytest.fixture
def make_unit(admin):
    def make(name: str = "B-204", **extra: Any) -> tuple[dict, str]:
        res = admin.post("/api/admin/units", json={"name": name, **extra})
        assert res.status_code == 201, res.text
        body = res.json()
        return body["unit"], body["key"]

    return make


def unit_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def hello_payload(**overrides: Any) -> dict[str, Any]:
    payload = {"agent_version": "2.0.0", "fw_version": "1.29.0", "chip": "ESP32-C5",
               "mac": "02:00:00:c5:00:01", "reset_cause": "power_on", "uptime_s": 3.2,
               "ssid": "VIKK", "bssid": "02:00:00:aa:00:40", "channel": 9, "rssi": -41,
               "ip": "10.0.5.23", "buffered": 0}
    payload.update(overrides)
    return payload
