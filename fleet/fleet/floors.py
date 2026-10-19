"""Floor plans and unit placements for the heat map (#6)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import add_event
from .tables import floor_plans, unit_placements, units

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_FLOORS = 100
NAME_RE = re.compile(r"^\S.{0,79}$", re.S)
# Things that can run code or load other content from an SVG. The dashboard shows plans in <img>,
# where none of this runs, and the image endpoint adds a sandboxing CSP; this check is the third line.
SVG_FORBIDDEN = re.compile(
    rb"<script|<foreignObject|<iframe|<embed|<object|\son[a-z]+\s*=|javascript:|<!ENTITY"
    rb"|href\s*=\s*[\"'](?!#|data:image/(?:png|jpe?g|webp|gif);)", re.I)


class FloorError(ValueError):
    pass


def sniff_image(data: bytes) -> str:
    """Content type from the bytes themselves; the client's claim is not trusted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")
    if (head.startswith(b"<?xml") or head.startswith(b"<svg") or head.startswith(b"<!--")) and b"<svg" in data[:4096]:
        if SVG_FORBIDDEN.search(data):
            raise FloorError("this SVG contains scripts, event handlers or external links; export it again without them")
        return "image/svg+xml"
    raise FloorError("upload a PNG, JPEG, WebP or SVG image")


def check_image(data: bytes) -> str:
    if not data:
        raise FloorError("the image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise FloorError(f"the image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    return sniff_image(data)


def check_name(name: str | None) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise FloorError("give the floor plan a name of 1 to 80 characters")
    return name


def _public(row) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "content_type": row.content_type, "size": row.size,
            "sort_order": row.sort_order, "created_at": row.created_at, "updated_at": row.updated_at}


def list_floors(conn: Connection) -> list[dict[str, Any]]:
    columns = [c for c in floor_plans.c if c.name != "image"]
    floors = [_public(r) for r in conn.execute(select(*columns).order_by(floor_plans.c.sort_order, floor_plans.c.id))]
    placements: dict[int, list[dict[str, Any]]] = {}
    for p in conn.execute(select(unit_placements).order_by(unit_placements.c.unit_id)):
        placements.setdefault(p.floor_id, []).append({"unit_id": p.unit_id, "x": p.x, "y": p.y})
    for floor in floors:
        floor["placements"] = placements.get(floor["id"], [])
    return floors


def get_floor(conn: Connection, floor_id: int) -> dict[str, Any] | None:
    columns = [c for c in floor_plans.c if c.name != "image"]
    row = conn.execute(select(*columns).where(floor_plans.c.id == floor_id)).first()
    return _public(row) if row else None


def floor_image(conn: Connection, floor_id: int) -> tuple[bytes, str, datetime] | None:
    row = conn.execute(select(floor_plans.c.image, floor_plans.c.content_type, floor_plans.c.updated_at)
                       .where(floor_plans.c.id == floor_id)).first()
    return (bytes(row.image), row.content_type, row.updated_at) if row else None


def create_floor(conn: Connection, name: str | None, data: bytes, now: datetime) -> dict[str, Any]:
    name = check_name(name)
    content_type = check_image(data)
    count, last = conn.execute(select(func.count(), func.max(floor_plans.c.sort_order))).one()
    if count >= MAX_FLOORS:
        raise FloorError(f"at most {MAX_FLOORS} floor plans")
    floor_id = conn.execute(insert(floor_plans).values(
        name=name, image=data, content_type=content_type, size=len(data), sort_order=(last or 0) + 1,
        created_at=now, updated_at=now).returning(floor_plans.c.id)).scalar_one()
    add_event(conn, time=now, unit_id=None, kind="floor_added", message=f"Floor plan added: {name}")
    return get_floor(conn, floor_id)


def replace_image(conn: Connection, floor_id: int, data: bytes, now: datetime) -> dict[str, Any] | None:
    content_type = check_image(data)
    done = conn.execute(update(floor_plans).where(floor_plans.c.id == floor_id).values(
        image=data, content_type=content_type, size=len(data), updated_at=now)).rowcount
    return get_floor(conn, floor_id) if done else None


def update_floor(conn: Connection, floor_id: int, name: str | None, sort_order: int | None,
                 now: datetime) -> dict[str, Any] | None:
    values: dict[str, Any] = {"updated_at": now}
    if name is not None:
        values["name"] = check_name(name)
    if sort_order is not None:
        values["sort_order"] = sort_order
    done = conn.execute(update(floor_plans).where(floor_plans.c.id == floor_id).values(**values)).rowcount
    return get_floor(conn, floor_id) if done else None


def delete_floor(conn: Connection, floor_id: int, now: datetime) -> bool:
    row = conn.execute(delete(floor_plans).where(floor_plans.c.id == floor_id)
                       .returning(floor_plans.c.name)).first()
    if row:
        add_event(conn, time=now, unit_id=None, kind="floor_removed", level="warning",
                  message=f"Floor plan removed: {row.name}")
    return row is not None


def place_unit(conn: Connection, unit_id: str, floor_id: int, x: float, y: float, now: datetime) -> dict[str, Any]:
    if conn.execute(select(units.c.id).where(units.c.id == unit_id)).first() is None:
        raise FloorError("unit not found")
    if conn.execute(select(floor_plans.c.id).where(floor_plans.c.id == floor_id)).first() is None:
        raise FloorError("floor plan not found")
    stmt = pg_insert(unit_placements).values(unit_id=unit_id, floor_id=floor_id, x=x, y=y, updated_at=now)
    conn.execute(stmt.on_conflict_do_update(index_elements=["unit_id"],
                                            set_={"floor_id": floor_id, "x": x, "y": y, "updated_at": now}))
    return {"unit_id": unit_id, "floor_id": floor_id, "x": x, "y": y}


def remove_placement(conn: Connection, unit_id: str) -> bool:
    return conn.execute(delete(unit_placements).where(unit_placements.c.unit_id == unit_id)).rowcount > 0
