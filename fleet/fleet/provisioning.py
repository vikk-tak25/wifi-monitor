"""Browser provisioning (#4): Wi-Fi credentials (encrypted at rest), unit configs, firmware, labels."""

from __future__ import annotations

import base64
import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import units
from .config import Settings
from .db import add_event
from .schemas import LabelTemplate, ProvisionRequest, UnitCreate, WifiUpdate
from .tables import settings as settings_table


@dataclass(frozen=True)
class Firmware:
    chip: str
    version: str
    file: str
    url: str
    sha256: str
    size: int
    offset: int


# The MicroPython build units run. Pinned: the browser checks the board's version against it, and
# `fleet fetch-firmware` verifies the download against the hash.
FIRMWARE = Firmware(
    chip="ESP32-C5",
    version="1.29.0",
    file="ESP32_GENERIC_C5-20260824-v1.29.0.bin",
    url="https://micropython.org/resources/firmware/ESP32_GENERIC_C5-20260824-v1.29.0.bin",
    sha256="24a9d43e2b97b191507b6c23ce6825f79a1b6e3292e51ffe4de2d5240bdc77d0",
    size=1965104,
    offset=0x2000,
)
UNIT_FILES = ("main.py", "app.py")
WATCHDOG_S = 120
WIFI_KEY = "wifi"
LABEL_KEY = "label_template"


class ProvisioningError(ValueError):
    pass


class BoardAlreadyRegistered(ProvisioningError):
    """The board's MAC belongs to other active units; the caller must choose what to do (#14)."""

    def __init__(self, existing: list[dict[str, Any]]) -> None:
        names = ", ".join(u["name"] for u in existing)
        super().__init__(f"this board is already registered as {names}")
        self.existing = existing


# -- encryption ------------------------------------------------------------------

def _fernet(secret_key: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"wifi-fleet",
               info=b"wifi-credentials").derive(secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(secret_key: str, plaintext: str) -> str:
    return _fernet(secret_key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(secret_key: str, token: str) -> str:
    try:
        return _fernet(secret_key).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ProvisioningError("the stored Wi-Fi password cannot be decrypted (was FLEET_SECRET_KEY changed?); "
                                "set it again in Settings") from exc


# -- settings table ------------------------------------------------------------------

def get_setting(conn: Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute(select(settings_table.c.value, settings_table.c.updated_at)
                       .where(settings_table.c.key == key)).first()
    return {**row.value, "updated_at": row.updated_at} if row else None


def put_setting(conn: Connection, key: str, value: dict[str, Any], now: datetime) -> None:
    stmt = pg_insert(settings_table).values(key=key, value=value, updated_at=now)
    conn.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": now}))


def wifi_public(conn: Connection) -> dict[str, Any]:
    stored = get_setting(conn, WIFI_KEY)
    if not stored:
        return {"ssid": None, "password_set": False, "open_network": False, "updated_at": None}
    return {"ssid": stored["ssid"], "password_set": bool(stored.get("password")),
            "open_network": not stored.get("password"), "updated_at": stored["updated_at"]}


def update_wifi(conn: Connection, settings: Settings, data: WifiUpdate, now: datetime) -> dict[str, Any]:
    stored = get_setting(conn, WIFI_KEY) or {}
    if data.password is None:
        if "password" not in stored:
            raise ProvisioningError("enter the Wi-Fi password (or an empty one for an open network)")
        token = stored["password"]
    else:
        token = encrypt(settings.secret_key, data.password) if data.password else ""
    put_setting(conn, WIFI_KEY, {"ssid": data.ssid, "password": token}, now)
    add_event(conn, time=now, unit_id=None, kind="wifi_settings_changed",
              message=f"Provisioning Wi-Fi network set to {data.ssid}")
    return wifi_public(conn)


def wifi_credentials(conn: Connection, settings: Settings) -> tuple[str, str]:
    stored = get_setting(conn, WIFI_KEY)
    if not stored:
        raise ProvisioningError("set the Wi-Fi network in Settings before adding units")
    token = stored.get("password") or ""
    return stored["ssid"], decrypt(settings.secret_key, token) if token else ""


# -- labels -----------------------------------------------------------------------------

def label_template(conn: Connection) -> dict[str, Any]:
    stored = get_setting(conn, LABEL_KEY)
    template = LabelTemplate.model_validate({k: v for k, v in (stored or {}).items() if k != "updated_at"})
    return template.model_dump()


def update_label_template(conn: Connection, data: LabelTemplate, now: datetime) -> dict[str, Any]:
    put_setting(conn, LABEL_KEY, data.model_dump(), now)
    return label_template(conn)


# -- provisioning ---------------------------------------------------------------------------

def unit_config(settings: Settings, unit_key: str, ssid: str, password: str) -> dict[str, Any]:
    """The config.json written to a unit (read by unit/app.py)."""
    return {"wifi_ssid": ssid, "wifi_password": password, "server_url": settings.effective_unit_url,
            "unit_key": unit_key, "watchdog_s": WATCHDOG_S}


def other_units_with_mac(conn: Connection, mac: str | None, exclude: str | None) -> list[dict[str, Any]]:
    if not mac:
        return []
    from .tables import units as units_table

    query = (select(units_table.c.id, units_table.c.name)
             .where(units_table.c.mac == mac.lower(), units_table.c.revoked_at.is_(None))
             .order_by(units_table.c.created_at))
    if exclude:
        query = query.where(units_table.c.id != exclude)
    return [{"id": r.id, "name": r.name} for r in conn.execute(query)]


def provision(conn: Connection, settings: Settings, data: ProvisionRequest, now: datetime) -> dict[str, Any]:
    ssid, password = wifi_credentials(conn, settings)
    others = other_units_with_mac(conn, data.mac, data.unit_id)
    if others and not data.retire_existing:
        raise BoardAlreadyRegistered(others)
    if data.unit_id:
        existing = units.get_unit(conn, data.unit_id)
        if existing is None:
            raise ProvisioningError("unit not found")
        key = units.rotate_key(conn, settings, data.unit_id, now)
        units.update_unit(conn, data.unit_id, _update_for(data), now)
        unit = units.get_unit(conn, data.unit_id)
        message = f"Unit reprogrammed for {unit.name}"
    else:
        unit, key = units.create_unit(conn, settings, UnitCreate(name=data.name, notes=data.notes, mac=data.mac), now)
        message = f"Unit programmed for {unit.name}"
    add_event(conn, time=now, unit_id=unit.id, kind="unit_provisioned", level="success",
              message=message + (f" (board {data.mac})" if data.mac else ""), detail={"mac": data.mac})
    for old in others:  # only reached with retire_existing
        units.revoke_unit(conn, old["id"], now)
        add_event(conn, time=now, unit_id=old["id"], kind="unit_retired", level="warning",
                  message=f"Retired: its board was programmed as {unit.name}", detail={"replaced_by": unit.id})
    return {"unit": units.public_unit(unit, settings), "config": unit_config(settings, key, ssid, password)}


def _update_for(data: ProvisionRequest):
    from .schemas import UnitUpdate

    fields: dict[str, Any] = {"name": data.name}
    if data.notes is not None:
        fields["notes"] = data.notes
    if data.mac is not None:
        fields["mac"] = data.mac
    return UnitUpdate(**fields)


def unit_file(settings: Settings, name: str) -> bytes:
    """main.py or app.py, from the same directory as the agent offered for remote updates."""
    from .config import DEFAULT_AGENT_PATH

    if name not in UNIT_FILES:
        raise ProvisioningError("unknown unit file")
    directory = (settings.agent_path or DEFAULT_AGENT_PATH).parent
    try:
        return (directory / name).read_bytes()
    except OSError as exc:
        raise ProvisioningError(f"{name} is not available on the server") from exc


# -- firmware -----------------------------------------------------------------------------------

_firmware_checked: dict[Path, tuple[float, bool]] = {}
_firmware_lock = threading.Lock()


def firmware_path(settings: Settings) -> Path:
    return settings.firmware_dir / FIRMWARE.file


def firmware_available(settings: Settings) -> bool:
    """True when the pinned firmware file is present and matches its hash (checked once per file change)."""
    path = firmware_path(settings)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    with _firmware_lock:
        cached = _firmware_checked.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        ok = hashlib.sha256(path.read_bytes()).hexdigest() == FIRMWARE.sha256
        _firmware_checked[path] = (mtime, ok)
        return ok


def firmware_public(settings: Settings) -> dict[str, Any]:
    return {"chip": FIRMWARE.chip, "version": FIRMWARE.version, "sha256": FIRMWARE.sha256, "size": FIRMWARE.size,
            "offset": FIRMWARE.offset, "available": firmware_available(settings)}


def fetch_firmware(settings: Settings, opener=None) -> Path:
    """Download the pinned firmware into FLEET_FIRMWARE_DIR and verify it."""
    import urllib.request

    path = firmware_path(settings)
    if firmware_available(settings):
        return path
    open_url = opener or urllib.request.urlopen
    with open_url(FIRMWARE.url, timeout=60) as response:
        data = response.read(FIRMWARE.size + 1)
    if len(data) != FIRMWARE.size or hashlib.sha256(data).hexdigest() != FIRMWARE.sha256:
        raise ProvisioningError("downloaded firmware does not match the pinned size and SHA-256")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path
