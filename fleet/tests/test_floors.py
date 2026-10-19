"""Floor plans and unit placements (#6)."""

import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from fleet import floors
from fleet.app import create_app


def png(width=4, height=3):
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><rect width="100" height="50"/></svg>'
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 30


def upload(admin, data, name="Floor 1", content_type="image/png"):
    return admin.post(f"/api/admin/floors?name={name}", content=data, headers={"Content-Type": content_type})


@pytest.fixture
def floor(admin):
    res = upload(admin, png())
    assert res.status_code == 201, res.text
    return res.json()["floor"]


@pytest.mark.parametrize("data,content_type", [(png(), "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"),
                                               (SVG, "image/svg+xml")])
def test_accepted_image_types_are_detected_from_the_bytes(admin, data, content_type):
    res = upload(admin, data, content_type="application/octet-stream")  # the client's claim is not trusted
    assert res.status_code == 201 and res.json()["floor"]["content_type"] == content_type


@pytest.mark.parametrize("data,message", [
    (b"", "empty"),
    (b"GIF89a" + b"\x00" * 20, "PNG, JPEG, WebP or SVG"),
    (b"%PDF-1.7", "PNG, JPEG, WebP or SVG"),
    (b"<html><body>hi</body></html>", "PNG, JPEG, WebP or SVG"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "scripts"),
    (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>', "scripts"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"><text>x</text></a></svg>', "scripts"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://tracker.example/x.png"/></svg>', "external"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div/></foreignObject></svg>', "scripts"),
    (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg>&a;</svg>', "scripts"),
])
def test_rejected_uploads(admin, data, message):
    res = upload(admin, data)
    assert res.status_code == 422 and message in res.json()["error"]


def test_svg_with_embedded_raster_and_internal_links_is_fine(admin):
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
           b'<image xlink:href="data:image/png;base64,iVBORw0KGgo="/><use href="#room"/></svg>')
    assert upload(admin, svg).status_code == 201


def test_size_and_name_limits(admin, monkeypatch):
    monkeypatch.setattr(floors, "MAX_IMAGE_BYTES", 1000)
    assert upload(admin, png() + b"\x00" * 2000).status_code == 413  # refused while reading the body
    big = admin.post("/api/admin/floors?name=x", content=b"\x00" * (5 * 1024 * 1024 + 10),
                     headers={"Content-Type": "image/png"})
    assert big.status_code == 413
    assert upload(admin, png(), name="").status_code == 422
    assert upload(admin, png(), name="x" * 81).status_code == 422


def test_floor_crud_and_image_serving(admin, floor):
    fid = floor["id"]
    assert floor["name"] == "Floor 1" and floor["size"] == len(png())
    second = upload(admin, SVG, name="Floor 2").json()["floor"]
    listed = admin.get("/api/admin/floors").json()["floors"]
    assert [f["name"] for f in listed] == ["Floor 1", "Floor 2"] and listed[0]["placements"] == []
    assert "image" not in listed[0]

    image = admin.get(f"/api/admin/floors/{fid}/image")
    assert image.status_code == 200 and image.content == png()
    assert image.headers["content-type"] == "image/png" and image.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in image.headers or "sandbox" not in image.headers["Content-Security-Policy"]
    svg_image = admin.get(f"/api/admin/floors/{second['id']}/image")
    assert "sandbox" in svg_image.headers["Content-Security-Policy"] and "default-src 'none'" in svg_image.headers["Content-Security-Policy"]

    renamed = admin.patch(f"/api/admin/floors/{fid}", json={"name": "Ground floor", "sort_order": 9}).json()["floor"]
    assert renamed["name"] == "Ground floor"
    assert [f["name"] for f in admin.get("/api/admin/floors").json()["floors"]] == ["Floor 2", "Ground floor"]
    replaced = admin.put(f"/api/admin/floors/{fid}/image", content=JPEG, headers={"Content-Type": "image/jpeg"})
    assert replaced.json()["floor"]["content_type"] == "image/jpeg"

    assert admin.delete(f"/api/admin/floors/{fid}").json() == {"deleted": fid}
    assert admin.get(f"/api/admin/floors/{fid}/image").status_code == 404
    assert admin.patch(f"/api/admin/floors/{fid}", json={"name": "x"}).status_code == 404
    assert admin.delete(f"/api/admin/floors/{fid}").status_code == 404
    kinds = [e["kind"] for e in admin.get("/api/admin/events").json()["events"]]
    assert "floor_added" in kinds and "floor_removed" in kinds


def test_placements(admin, floor, make_unit):
    unit, _ = make_unit("B-204")
    other_floor = upload(admin, SVG, name="Floor 2").json()["floor"]
    res = admin.put(f"/api/admin/placements/{unit['id']}", json={"floor_id": floor["id"], "x": 0.25, "y": 0.75})
    assert res.json()["placement"] == {"unit_id": unit["id"], "floor_id": floor["id"], "x": 0.25, "y": 0.75}
    # Moving to another floor replaces the placement: a unit is in one place.
    admin.put(f"/api/admin/placements/{unit['id']}", json={"floor_id": other_floor["id"], "x": 0.5, "y": 0.5})
    by_id = {f["id"]: f for f in admin.get("/api/admin/floors").json()["floors"]}
    assert by_id[floor["id"]]["placements"] == []
    assert by_id[other_floor["id"]]["placements"] == [{"unit_id": unit["id"], "x": 0.5, "y": 0.5}]

    assert admin.delete(f"/api/admin/placements/{unit['id']}").json() == {"removed": unit["id"]}
    assert admin.delete(f"/api/admin/placements/{unit['id']}").status_code == 404


@pytest.mark.parametrize("body,status", [
    ({"floor_id": 1, "x": -0.1, "y": 0.5}, 422), ({"floor_id": 1, "x": 0.5, "y": 1.01}, 422),
    ({"floor_id": 0, "x": 0.5, "y": 0.5}, 422), ({"floor_id": 1, "x": 0.5}, 422),
])
def test_placement_validation(admin, floor, make_unit, body, status):
    unit, _ = make_unit()
    assert admin.put(f"/api/admin/placements/{unit['id']}", json=body).status_code == status


def test_placement_needs_existing_unit_and_floor(admin, floor, make_unit):
    unit, _ = make_unit()
    assert "unit not found" in admin.put("/api/admin/placements/uaaaaaaaaaa",
                                         json={"floor_id": floor["id"], "x": 0.5, "y": 0.5}).json()["error"]
    assert "floor plan not found" in admin.put(f"/api/admin/placements/{unit['id']}",
                                               json={"floor_id": 999999, "x": 0.5, "y": 0.5}).json()["error"]


def test_deleting_a_floor_removes_its_placements(admin, floor, make_unit, engine):
    unit, _ = make_unit()
    admin.put(f"/api/admin/placements/{unit['id']}", json={"floor_id": floor["id"], "x": 0.1, "y": 0.2})
    admin.delete(f"/api/admin/floors/{floor['id']}")
    from sqlalchemy import func, select

    from fleet.tables import unit_placements

    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(unit_placements)).scalar_one() == 0


def test_floor_endpoints_require_login(settings, engine, clock):
    with TestClient(create_app(settings, engine=engine, clock=clock, background=False)) as anonymous:
        h = {"X-Requested-With": "fleet"}
        assert anonymous.get("/api/admin/floors").status_code == 401
        assert anonymous.post("/api/admin/floors?name=x", content=png(), headers=h).status_code == 401
        assert anonymous.get("/api/admin/floors/1/image").status_code == 401
        assert anonymous.put("/api/admin/placements/uaaaaaaaaaa", json={"floor_id": 1, "x": 0, "y": 0}, headers=h).status_code == 401


def test_floor_writes_need_the_csrf_header(admin):
    res = admin.post("/api/admin/floors?name=x", content=png(), headers={"Content-Type": "image/png", "X-Requested-With": ""})
    assert res.status_code == 403
