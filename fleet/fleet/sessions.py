"""Admin login sessions, stored server-side as hashed random tokens."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Connection, delete, insert, select

from . import security
from .config import DEV_ADMIN_PASSWORD, Settings
from .tables import admin_sessions

COOKIE_NAME = "fleet_session"


def check_admin_password(settings: Settings, password: str) -> bool:
    if settings.admin_password_hash:
        return security.verify_password(password, settings.admin_password_hash)
    # Development without a configured hash: the documented default password.
    return not settings.is_production and password == DEV_ADMIN_PASSWORD


def create_session(conn: Connection, settings: Settings, client: str | None, now: datetime) -> str:
    token = security.new_session_token()
    conn.execute(insert(admin_sessions).values(
        token_hash=security.hash_token(token), created_at=now,
        expires_at=now + timedelta(hours=settings.session_hours), client=client))
    return token


def session_valid(conn: Connection, token: str | None, now: datetime) -> bool:
    if not token or len(token) > 128:
        return False
    row = conn.execute(select(admin_sessions.c.expires_at)
                       .where(admin_sessions.c.token_hash == security.hash_token(token))).first()
    return row is not None and row.expires_at > now


def end_session(conn: Connection, token: str | None) -> None:
    if token:
        conn.execute(delete(admin_sessions).where(admin_sessions.c.token_hash == security.hash_token(token)))


def purge_expired(conn: Connection, now: datetime) -> int:
    return conn.execute(delete(admin_sessions).where(admin_sessions.c.expires_at <= now)).rowcount
