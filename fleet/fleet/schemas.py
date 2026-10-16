"""Request and response models. Everything a unit sends is validated here with tight limits."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BSSID_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,32}$")

MAX_SAMPLES = 2000
MAX_SCANS = 20
MAX_APS = 200
MAX_EVENTS = 500
MAX_DETAIL_JSON = 2000

Rssi = Annotated[int, Field(ge=-127, le=0)]
Channel = Annotated[int, Field(ge=1, le=233)]
Millis = Annotated[float, Field(ge=0, le=60000)]
Epoch = Annotated[float, Field(gt=1_600_000_000, lt=4_000_000_000)]  # unix seconds


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


def _bssid(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.lower()
    if not BSSID_RE.match(value):
        raise ValueError("must look like aa:bb:cc:dd:ee:ff")
    return value


def _ssid(value: str | None) -> str | None:
    if value is not None and len(value.encode("utf-8")) > 32:
        raise ValueError("SSID is longer than 32 bytes")
    return value


def _name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("name is empty")
    return value


# -- unit API ---------------------------------------------------------------

class Hello(Strict):
    agent_version: Annotated[str, Field(pattern=VERSION_RE.pattern)]
    fw_version: Annotated[str, Field(pattern=VERSION_RE.pattern)] | None = None
    chip: Annotated[str, Field(max_length=64)] | None = None
    mac: str | None = None
    reset_cause: Annotated[str, Field(max_length=32)] | None = None
    uptime_s: Annotated[float, Field(ge=0)] | None = None
    ssid: str | None = None
    bssid: str | None = None
    channel: Channel | None = None
    rssi: Rssi | None = None
    ip: Annotated[str, Field(max_length=45)] | None = None
    buffered: Annotated[int, Field(ge=0)] | None = None

    @field_validator("mac", "bssid")
    @classmethod
    def _check_bssid(cls, value: str | None) -> str | None:
        return _bssid(value)

    @field_validator("ssid")
    @classmethod
    def _check_ssid(cls, value: str | None) -> str | None:
        return _ssid(value)


class Sample(Strict):
    """One link measurement. A lost ping is `*_lost: true` with no `*_ms`."""

    t: Epoch
    connected: bool
    rssi: Rssi | None = None
    bssid: str | None = None
    channel: Channel | None = None
    gw_ms: Millis | None = None
    gw_lost: bool | None = None
    srv_ms: Millis | None = None
    srv_lost: bool | None = None

    @field_validator("bssid")
    @classmethod
    def _check_bssid(cls, value: str | None) -> str | None:
        return _bssid(value)

    @model_validator(mode="after")
    def _consistent(self) -> "Sample":
        for name in ("gw", "srv"):
            if getattr(self, f"{name}_lost") and getattr(self, f"{name}_ms") is not None:
                raise ValueError(f"{name}_ms must be empty when {name}_lost is true")
        return self


class AccessPoint(BaseModel):
    """[ssid, bssid, channel, rssi, auth, hidden] as sent by the agent."""

    ssid: str
    bssid: str
    channel: Channel
    rssi: Rssi
    auth: Annotated[int, Field(ge=0, le=31)]
    hidden: bool

    @field_validator("bssid")
    @classmethod
    def _check_bssid(cls, value: str) -> str:
        return _bssid(value)

    @field_validator("ssid")
    @classmethod
    def _check_ssid(cls, value: str) -> str:
        return _ssid(value)


class Scan(Strict):
    t: Epoch
    duration_ms: Annotated[int, Field(ge=0, le=120000)] | None = None
    aps: Annotated[list[AccessPoint], Field(max_length=MAX_APS)]

    @field_validator("aps", mode="before")
    @classmethod
    def _from_lists(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        out = []
        for item in value:
            if isinstance(item, list):
                if len(item) != 6:
                    raise ValueError("each access point must be [ssid, bssid, channel, rssi, auth, hidden]")
                item = dict(zip(("ssid", "bssid", "channel", "rssi", "auth", "hidden"), item))
            out.append(item)
        return out


class UnitEvent(Strict):
    t: Epoch
    kind: Annotated[str, Field(pattern=KIND_RE.pattern)]
    detail: dict[str, Any] | None = None

    @field_validator("detail")
    @classmethod
    def _small(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value)) > MAX_DETAIL_JSON:
            raise ValueError(f"detail is larger than {MAX_DETAIL_JSON} bytes")
        return value


class Batch(Strict):
    batch_id: Annotated[str, Field(pattern=BATCH_ID_RE.pattern)]
    sent_at: Epoch
    samples: Annotated[list[Sample], Field(max_length=MAX_SAMPLES)] = []
    scans: Annotated[list[Scan], Field(max_length=MAX_SCANS)] = []
    events: Annotated[list[UnitEvent], Field(max_length=MAX_EVENTS)] = []


# -- admin API --------------------------------------------------------------

class Login(Strict):
    password: Annotated[str, Field(min_length=1, max_length=256)]


class UnitConfig(Strict):
    """Per-unit overrides of the fleet defaults. Missing fields use the defaults."""

    sample_interval_s: Annotated[float, Field(ge=1, le=300)] | None = None
    batch_interval_s: Annotated[float, Field(ge=5, le=3600)] | None = None
    scan_interval_s: Annotated[float, Field(ge=0, le=86400)] | None = None
    server_ping_interval_s: Annotated[float, Field(ge=0, le=3600)] | None = None
    ping_target: Annotated[str, Field(pattern=r"^(gateway|[A-Za-z0-9.\-:]{1,253})$")] | None = None


class UnitCreate(Strict):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    notes: Annotated[str, Field(max_length=2000)] | None = None
    mac: str | None = None
    config: UnitConfig | None = None

    @field_validator("mac")
    @classmethod
    def _check_mac(cls, value: str | None) -> str | None:
        return _bssid(value)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _name(value)


class UnitUpdate(Strict):
    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None
    mac: str | None = None
    config: UnitConfig | None = None

    @field_validator("mac")
    @classmethod
    def _check_mac(cls, value: str | None) -> str | None:
        return _bssid(value)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        return None if value is None else _name(value)
