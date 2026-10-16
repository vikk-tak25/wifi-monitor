"""Fleet server settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ENVIRONMENTS = ("development", "production")
DEV_DATABASE_URL = "postgresql+psycopg://fleet:fleet-dev@127.0.0.1:5433/fleet"
DEV_SECRET_KEY = "dev-only-secret-key-change-me"
DEV_ADMIN_PASSWORD = "admin"
MIN_SECRET_KEY_LENGTH = 32
# The unit agent served for remote updates. In a source checkout it is unit/app.py.
DEFAULT_AGENT_PATH = Path(__file__).resolve().parents[2] / "unit" / "app.py"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class UnitDefaults:
    """Measurement configuration sent to every unit in the handshake."""

    sample_interval_s: float = 5.0
    batch_interval_s: float = 60.0
    scan_interval_s: float = 600.0
    server_ping_interval_s: float = 30.0
    ping_target: str = "gateway"

    def as_dict(self) -> dict:
        return {
            "sample_interval_s": self.sample_interval_s,
            "batch_interval_s": self.batch_interval_s,
            "scan_interval_s": self.scan_interval_s,
            "server_ping_interval_s": self.server_ping_interval_s,
            "ping_target": self.ping_target,
        }


@dataclass(frozen=True)
class Settings:
    environment: str = "development"
    database_url: str = DEV_DATABASE_URL
    secret_key: str = DEV_SECRET_KEY
    admin_password_hash: str | None = None
    public_url: str = "http://localhost:8800"
    session_hours: float = 12.0
    offline_after_s: float = 600.0
    agent_path: Path | None = None
    unit_defaults: UnitDefaults = field(default_factory=UnitDefaults)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.public_url).scheme == "https"

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}".lower()


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _float(merged: dict[str, str], key: str, default: float, low: float, high: float) -> float:
    text = merged.get(key, "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {text!r}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{key} must be between {low:g} and {high:g}")
    return value


def load_settings(env: dict[str, str] | None = None, env_file: Path | str | None = ".env") -> Settings:
    merged: dict[str, str] = {}
    if env_file is not None:
        merged.update(parse_env_file(Path(env_file)))
    merged.update(os.environ if env is None else env)

    def get(key: str, default: str = "") -> str:
        return merged.get(key, default).strip() or default

    environment = get("ENVIRONMENT", "development").lower()
    if environment not in ENVIRONMENTS:
        raise ConfigError(f"ENVIRONMENT must be one of {ENVIRONMENTS}, got {environment!r}")
    production = environment == "production"

    database_url = get("DATABASE_URL", "" if production else DEV_DATABASE_URL)
    if not database_url:
        raise ConfigError("DATABASE_URL is required in production")

    secret_key = get("FLEET_SECRET_KEY", "" if production else DEV_SECRET_KEY)
    if production and (secret_key == DEV_SECRET_KEY or len(secret_key) < MIN_SECRET_KEY_LENGTH):
        raise ConfigError(f"FLEET_SECRET_KEY must be set to a random value of at least "
                          f"{MIN_SECRET_KEY_LENGTH} characters in production")

    admin_hash = get("FLEET_ADMIN_PASSWORD_HASH") or None
    if production and not admin_hash:
        raise ConfigError("FLEET_ADMIN_PASSWORD_HASH is required in production (create one with `fleet hash-password`)")
    if admin_hash and not admin_hash.startswith("scrypt$"):
        raise ConfigError("FLEET_ADMIN_PASSWORD_HASH must be created with `fleet hash-password`")

    public_url = get("FLEET_PUBLIC_URL", "http://localhost:8800").rstrip("/")
    scheme = urlsplit(public_url).scheme
    if scheme not in ("http", "https"):
        raise ConfigError("FLEET_PUBLIC_URL must start with http:// or https://")
    if production and scheme != "https":
        raise ConfigError("FLEET_PUBLIC_URL must use https:// in production")

    updates = get("FLEET_AGENT_UPDATES", "true").lower()
    if updates not in ("true", "false"):
        raise ConfigError("FLEET_AGENT_UPDATES must be true or false")
    agent_path = Path(get("FLEET_AGENT_PATH", str(DEFAULT_AGENT_PATH))) if updates == "true" else None

    defaults = UnitDefaults(
        sample_interval_s=_float(merged, "FLEET_SAMPLE_INTERVAL_S", 5.0, 1, 300),
        batch_interval_s=_float(merged, "FLEET_BATCH_INTERVAL_S", 60.0, 5, 3600),
        scan_interval_s=_float(merged, "FLEET_SCAN_INTERVAL_S", 600.0, 0, 86400),
        server_ping_interval_s=_float(merged, "FLEET_SERVER_PING_INTERVAL_S", 30.0, 0, 3600),
    )

    return Settings(
        environment=environment,
        database_url=database_url,
        secret_key=secret_key,
        admin_password_hash=admin_hash,
        public_url=public_url,
        session_hours=_float(merged, "FLEET_SESSION_HOURS", 12.0, 0.1, 24 * 30),
        offline_after_s=_float(merged, "FLEET_OFFLINE_AFTER_S", 600.0, 30, 86400),
        agent_path=agent_path,
        unit_defaults=defaults,
    )
