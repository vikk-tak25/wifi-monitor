"""Settings loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENVIRONMENTS = ("development", "production")
DEVICES = ("serial", "simulated")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    environment: str = "development"
    device: str = "serial"
    serial_port: str | None = None
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    db_path: Path = Path("data/wifimon.db")
    wifi_ssid: str = ""
    wifi_password: str = ""
    ping_target: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Supports comments, blank lines, `export` and quoted values."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load_settings(env: dict[str, str] | None = None, env_file: Path | str | None = ".env") -> Settings:
    merged: dict[str, str] = {}
    if env_file is not None:
        merged.update(parse_env_file(Path(env_file)))
    merged.update(os.environ if env is None else env)

    def get(key: str, default: str = "") -> str:
        return merged.get(key, default).strip()

    environment = get("ENVIRONMENT", "development").lower() or "development"
    if environment not in ENVIRONMENTS:
        raise ConfigError(f"ENVIRONMENT must be one of {ENVIRONMENTS}, got {environment!r}")

    device = get("WIFIMON_DEVICE", "serial").lower() or "serial"
    if device not in DEVICES:
        raise ConfigError(f"WIFIMON_DEVICE must be one of {DEVICES}, got {device!r}")
    if device == "simulated" and environment == "production":
        raise ConfigError("the simulated device is not allowed when ENVIRONMENT=production")

    port_text = get("WIFIMON_HTTP_PORT", "8765") or "8765"
    try:
        http_port = int(port_text)
    except ValueError as exc:
        raise ConfigError(f"WIFIMON_HTTP_PORT must be a number, got {port_text!r}") from exc
    if not 0 <= http_port <= 65535:
        raise ConfigError(f"WIFIMON_HTTP_PORT out of range: {http_port}")

    return Settings(
        environment=environment,
        device=device,
        serial_port=get("WIFIMON_SERIAL_PORT") or None,
        http_host=get("WIFIMON_HTTP_HOST", "127.0.0.1") or "127.0.0.1",
        http_port=http_port,
        db_path=Path(get("WIFIMON_DB", "data/wifimon.db") or "data/wifimon.db"),
        wifi_ssid=get("WIFI_SSID"),
        # Passwords may legitimately start or end with spaces.
        wifi_password=merged.get("WIFI_PASSWORD", ""),
        ping_target=get("WIFIMON_PING_TARGET"),
    )
