from pathlib import Path

import pytest

from wifimon.config import ConfigError, load_settings, parse_env_file


def test_defaults_are_development_and_local(tmp_path):
    s = load_settings(env={}, env_file=tmp_path / "missing.env")
    assert s.environment == "development" and not s.is_production
    assert s.device == "serial" and s.serial_port is None
    assert s.http_host == "127.0.0.1" and s.http_port == 8765
    assert s.db_path == Path("data/wifimon.db")


def test_env_file_parsing_and_precedence(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nexport WIFI_SSID='Home Net'\nWIFI_PASSWORD=\" spaced \"\n"
        "WIFIMON_HTTP_PORT=9000\nWIFIMON_DEVICE=simulated\nnot a line\n")
    assert parse_env_file(env_file)["WIFI_SSID"] == "Home Net"
    s = load_settings(env={"WIFIMON_HTTP_PORT": "9100"}, env_file=env_file)
    assert s.wifi_ssid == "Home Net"
    assert s.wifi_password == " spaced "
    assert s.http_port == 9100
    assert s.device == "simulated"


@pytest.mark.parametrize("env,message", [
    ({"ENVIRONMENT": "staging"}, "ENVIRONMENT"),
    ({"WIFIMON_DEVICE": "wifi"}, "WIFIMON_DEVICE"),
    ({"WIFIMON_HTTP_PORT": "abc"}, "number"),
    ({"WIFIMON_HTTP_PORT": "70000"}, "range"),
    ({"ENVIRONMENT": "production", "WIFIMON_DEVICE": "simulated"}, "not allowed"),
])
def test_invalid_settings(tmp_path, env, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(env=env, env_file=None)


def test_env_example_documents_every_setting():
    example = parse_env_file(Path(__file__).resolve().parent.parent / ".env.example")
    # The file also documents the fleet server (fleet/), so check that every wifimon setting is present.
    assert {"ENVIRONMENT", "WIFIMON_DEVICE", "WIFIMON_SERIAL_PORT", "WIFIMON_HTTP_HOST", "WIFIMON_HTTP_PORT",
            "WIFIMON_DB", "WIFI_SSID", "WIFI_PASSWORD", "WIFIMON_PING_TARGET"} <= set(example)
    assert example["ENVIRONMENT"] == "development"
    assert load_settings(env=example, env_file=None).environment == "development"
