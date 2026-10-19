"""Browser provisioning, server side (#4)."""

import hashlib
import io
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from fleet import provisioning
from fleet.app import create_app
from fleet.tables import settings as settings_table

from .agent_harness import UNIT_DIR, Rig, load_module


@pytest.fixture
def wifi(admin):
    res = admin.put("/api/admin/provisioning/wifi", json={"ssid": "VIKK opilane", "password": "school-secret-pw"})
    assert res.status_code == 200, res.text
    return res.json()["wifi"]


def test_wifi_password_is_encrypted_and_never_returned(admin, wifi, engine):
    assert wifi == {"ssid": "VIKK opilane", "password_set": True, "open_network": False,
                    "updated_at": wifi["updated_at"]}
    body = admin.get("/api/admin/provisioning").text
    assert "school-secret-pw" not in body
    with engine.connect() as conn:
        stored = conn.execute(select(settings_table.c.value).where(settings_table.c.key == "wifi")).scalar_one()
        raw = conn.execute(text("SELECT value::text FROM settings")).scalars().all()
    assert stored["ssid"] == "VIKK opilane" and stored["password"].startswith("gAAAA")  # Fernet token
    assert all("school-secret-pw" not in r for r in raw)


def test_wifi_update_keeps_or_replaces_the_password(admin, wifi, settings, engine):
    admin.put("/api/admin/provisioning/wifi", json={"ssid": "VIKK"})  # password omitted: keep it
    with engine.connect() as conn:
        assert provisioning.wifi_credentials(conn, settings) == ("VIKK", "school-secret-pw")
    admin.put("/api/admin/provisioning/wifi", json={"ssid": "Open-Lab", "password": ""})
    with engine.connect() as conn:
        assert provisioning.wifi_credentials(conn, settings) == ("Open-Lab", "")
    assert admin.get("/api/admin/provisioning").json()["wifi"]["open_network"] is True


@pytest.mark.parametrize("body,status", [
    ({"ssid": ""}, 422), ({"ssid": "x" * 33, "password": "12345678"}, 422),
    ({"ssid": "Net", "password": "short"}, 422), ({"ssid": "Net", "password": "p" * 64}, 422),
    ({"ssid": "Net"}, 409),  # no stored password to keep yet
])
def test_wifi_validation(admin, body, status):
    assert admin.put("/api/admin/provisioning/wifi", json=body).status_code == status


def test_changed_secret_key_is_reported_not_crashing(admin, wifi, settings, engine):
    other = replace(settings, secret_key="another-secret-key-" + "y" * 32)
    with engine.connect() as conn, pytest.raises(provisioning.ProvisioningError, match="cannot be decrypted"):
        provisioning.wifi_credentials(conn, other)


def test_provision_requires_wifi_settings(admin):
    res = admin.post("/api/admin/provision", json={"name": "B-204"})
    assert res.status_code == 409 and "Wi-Fi" in res.json()["error"]


def test_provision_returns_a_config_the_real_agent_can_use(admin, client, wifi, clock, engine, settings, tmp_path,
                                                           monkeypatch):
    res = admin.post("/api/admin/provision", json={"name": "B-204", "notes": "window", "mac": "02:00:00:C5:00:01"})
    assert res.status_code == 200
    body = res.json()
    unit, config = body["unit"], body["config"]
    assert unit["name"] == "B-204" and unit["mac"] == "02:00:00:c5:00:01"
    assert config == {"wifi_ssid": "VIKK opilane", "wifi_password": "school-secret-pw",
                      "server_url": "http://testserver", "unit_key": config["unit_key"], "watchdog_s": 120}
    assert config["unit_key"].startswith(unit["id"] + ".")

    # The real agent, configured exactly like that, gets online.
    monkeypatch.chdir(tmp_path)
    app_mod = load_module(UNIT_DIR / "app.py")
    rig = Rig(app_mod, client, clock, config["unit_key"], ssid="VIKK opilane")
    rig.agent.config.update(config, server_url=rig.link.base)
    rig.agent.boot("power_on")
    rig.run(61)
    assert rig.agent.hello_ok
    assert admin.get(f"/api/admin/units/{unit['id']}").json()["unit"]["status"] == "online"
    kinds = [e["kind"] for e in admin.get(f"/api/admin/events?unit_id={unit['id']}").json()["events"]]
    assert "unit_provisioned" in kinds


def test_reprogramming_rekeys_and_renames(admin, wifi):
    first = admin.post("/api/admin/provision", json={"name": "B-204"}).json()
    unit_id = first["unit"]["id"]
    again = admin.post("/api/admin/provision", json={"name": "C-301", "unit_id": unit_id}).json()
    assert again["unit"]["id"] == unit_id and again["unit"]["name"] == "C-301"
    assert again["config"]["unit_key"] != first["config"]["unit_key"]
    assert admin.post("/api/admin/provision", json={"name": "X", "unit_id": "uaaaaaaaaaa"}).status_code == 409
    assert admin.post("/api/admin/provision", json={"name": "X", "unit_id": "bad"}).status_code == 422


def test_unit_url_defaults_to_http_of_public_url(settings):
    assert replace(settings, public_url="https://wifi.example.school").effective_unit_url == "http://wifi.example.school"
    assert replace(settings, unit_url="http://10.0.0.5:8800").effective_unit_url == "http://10.0.0.5:8800"


def test_unit_files_are_served(admin):
    for name in ("main.py", "app.py"):
        res = admin.get(f"/api/admin/provisioning/files/{name}")
        assert res.status_code == 200 and res.content == (UNIT_DIR / name).read_bytes()
    assert admin.get("/api/admin/provisioning/files/config.json").status_code == 404
    assert admin.get("/api/admin/provisioning/files/..%2Fsecrets").status_code == 404


def test_firmware_endpoint_and_status(admin, settings, engine, clock, tmp_path):
    fw_settings = replace(settings, firmware_dir=tmp_path)
    with TestClient(create_app(fw_settings, engine=engine, clock=clock, background=False)) as c:
        c.cookies = admin.cookies
        c.headers.update({"X-Requested-With": "fleet"})
        assert c.get("/api/admin/provisioning").json()["firmware"]["available"] is False
        assert c.get("/api/admin/provisioning/firmware.bin").status_code == 404
        (tmp_path / provisioning.FIRMWARE.file).write_bytes(b"not the real firmware")
        assert c.get("/api/admin/provisioning").json()["firmware"]["available"] is False  # hash mismatch


def test_fetch_firmware_verifies_the_download(settings, tmp_path, monkeypatch):
    fw = provisioning.Firmware(chip="ESP32-C5", version="9", file="fw.bin", url="https://example/fw.bin",
                               sha256=hashlib.sha256(b"firmware!").hexdigest(), size=9, offset=0x2000)
    monkeypatch.setattr(provisioning, "FIRMWARE", fw)
    s = replace(settings, firmware_dir=tmp_path)
    path = provisioning.fetch_firmware(s, opener=lambda url, timeout: io.BytesIO(b"firmware!"))
    assert path.read_bytes() == b"firmware!" and provisioning.firmware_available(s)
    (tmp_path / "fw.bin").unlink()
    with pytest.raises(provisioning.ProvisioningError, match="does not match"):
        provisioning.fetch_firmware(s, opener=lambda url, timeout: io.BytesIO(b"tampered!"))
    assert not (tmp_path / "fw.bin").exists()


def test_label_template_defaults_and_validation(admin):
    default = admin.get("/api/admin/label-template").json()["template"]
    assert default == {"width_mm": 62, "height_mm": 29, "font_scale": 1.0,
                       "fields": {"room": True, "unit_id": True, "qr": True, "network": False, "date": True, "mac": False}}
    custom = {**default, "width_mm": 89, "height_mm": 36, "fields": {**default["fields"], "mac": True}}
    assert admin.put("/api/admin/label-template", json=custom).json()["template"]["width_mm"] == 89
    assert admin.get("/api/admin/label-template").json()["template"]["fields"]["mac"] is True
    assert admin.put("/api/admin/label-template", json={**default, "width_mm": 500}).status_code == 422
    assert admin.put("/api/admin/label-template", json={**default, "fields": {"logo": True}}).status_code == 422


def test_provisioning_endpoints_require_login(settings, engine, clock):
    with TestClient(create_app(settings, engine=engine, clock=clock, background=False)) as anonymous:
        for method, path in (("get", "/api/admin/provisioning"), ("put", "/api/admin/provisioning/wifi"),
                             ("post", "/api/admin/provision"), ("get", "/api/admin/provisioning/files/app.py"),
                             ("get", "/api/admin/provisioning/firmware.bin"), ("get", "/api/admin/label-template")):
            assert getattr(anonymous, method)(path, headers={"X-Requested-With": "fleet"}).status_code == 401, path


# -- the same board registered twice (#14) ---------------------------------------------------------

BOARD = "02:00:00:c5:12:54"


def test_provisioning_a_registered_board_asks_what_to_do(admin, wifi):
    first = admin.post("/api/admin/provision", json={"name": "Desk test", "mac": BOARD}).json()["unit"]
    res = admin.post("/api/admin/provision", json={"name": "Klassiruum-2", "mac": BOARD.upper()})
    assert res.status_code == 409
    body = res.json()
    assert "already registered" in body["error"]
    assert body["existing_units"] == [{"id": first["id"], "name": "Desk test"}]
    names = [u["name"] for u in admin.get("/api/admin/units").json()["units"]]
    assert names == ["Desk test"]  # nothing was created


def test_new_unit_can_retire_the_existing_registration(admin, wifi):
    first = admin.post("/api/admin/provision", json={"name": "Desk test", "mac": BOARD}).json()["unit"]
    res = admin.post("/api/admin/provision", json={"name": "Klassiruum-2", "mac": BOARD, "retire_existing": True})
    assert res.status_code == 200
    units = {u["name"]: u for u in admin.get("/api/admin/units").json()["units"]}
    assert units["Desk test"]["status"] == "revoked" and units["Klassiruum-2"]["status"] == "never_seen"
    events = admin.get(f"/api/admin/events?unit_id={first['id']}").json()["events"]
    assert any(e["kind"] == "unit_retired" and "Klassiruum-2" in e["message"] for e in events)


def test_reprogramming_the_registered_unit_is_not_a_conflict(admin, wifi):
    first = admin.post("/api/admin/provision", json={"name": "Desk test", "mac": BOARD}).json()["unit"]
    res = admin.post("/api/admin/provision", json={"name": "Klassiruum-3", "mac": BOARD, "unit_id": first["id"]})
    assert res.status_code == 200 and res.json()["unit"]["name"] == "Klassiruum-3"


def test_revoked_units_do_not_count_as_registered(admin, wifi):
    first = admin.post("/api/admin/provision", json={"name": "Old", "mac": BOARD}).json()["unit"]
    admin.post(f"/api/admin/units/{first['id']}/revoke")
    assert admin.post("/api/admin/provision", json={"name": "New", "mac": BOARD}).status_code == 200


def test_remove_unit_deletes_it_with_its_data(admin, client, wifi, engine, clock):
    from sqlalchemy import func, select, text

    from fleet.tables import events, ingest_batches, link_samples, scan_observations, scans, units
    from .conftest import unit_headers

    made = admin.post("/api/admin/provision", json={"name": "Klassiruum-2", "mac": BOARD}).json()
    unit_id, key = made["unit"]["id"], made["config"]["unit_key"]
    other = admin.post("/api/admin/provision", json={"name": "Keep me", "mac": "02:00:00:c5:00:09"}).json()["unit"]
    client.post("/api/unit/v1/batch", headers=unit_headers(key), json={
        "batch_id": "b1", "sent_at": clock.ts,
        "samples": [{"t": clock.ts - 1, "connected": True, "rssi": -50}],
        "scans": [{"t": clock.ts - 2, "aps": [["VIKK", "02:00:00:aa:00:40", 6, -40, 3, False]]}]})
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO floor_plans (name, image, content_type, size, sort_order, created_at, updated_at) "
                          "VALUES ('F', '\\x89', 'image/png', 1, 1, now(), now())"))
        floor_id = conn.execute(text("SELECT id FROM floor_plans")).scalar_one()
    admin.put(f"/api/admin/placements/{unit_id}", json={"floor_id": floor_id, "x": 0.5, "y": 0.5})

    res = admin.delete(f"/api/admin/units/{unit_id}")
    assert res.status_code == 200 and res.json() == {"deleted": unit_id}
    with engine.connect() as conn:
        for table in (units, link_samples, scans, scan_observations, ingest_batches, events):
            assert conn.execute(select(func.count()).select_from(table).where(table.c.unit_id == unit_id
                                if table is not units else table.c.id == unit_id)).scalar_one() == 0, table.name
    assert admin.get(f"/api/admin/units/{other['id']}").status_code == 200
    assert admin.delete(f"/api/admin/units/{unit_id}").status_code == 404
    removed = [e for e in admin.get("/api/admin/events?kind=unit_removed").json()["events"]]
    assert removed and "Klassiruum-2" in removed[0]["message"] and removed[0]["unit_id"] is None


def test_remove_unit_needs_login_and_csrf_header(admin, settings, engine, clock, wifi):
    unit_id = admin.post("/api/admin/provision", json={"name": "X"}).json()["unit"]["id"]
    assert admin.delete(f"/api/admin/units/{unit_id}", headers={"X-Requested-With": ""}).status_code == 403
    with TestClient(create_app(settings, engine=engine, clock=clock, background=False)) as anonymous:
        assert anonymous.delete(f"/api/admin/units/{unit_id}", headers={"X-Requested-With": "fleet"}).status_code == 401
