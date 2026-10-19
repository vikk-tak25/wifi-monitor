from pathlib import Path

import pytest

from fleet import security
from fleet.config import ConfigError, load_settings, parse_env_file

GOOD_HASH = security.hash_password("long enough password", n=2 ** 10)
PROD = {"ENVIRONMENT": "production", "DATABASE_URL": "postgresql+psycopg://u:p@db/fleet",
        "FLEET_SECRET_KEY": "s" * 40, "FLEET_ADMIN_PASSWORD_HASH": GOOD_HASH,
        "FLEET_PUBLIC_URL": "https://wifi.example.school"}


def test_password_hash_round_trip():
    stored = security.hash_password("hunter2 hunter2", n=2 ** 10)
    assert stored.startswith("scrypt$1024$8$1$")
    assert security.verify_password("hunter2 hunter2", stored)
    assert not security.verify_password("hunter3 hunter3", stored)
    assert not security.verify_password("x", "garbage")
    assert stored != security.hash_password("hunter2 hunter2", n=2 ** 10)  # salted


def test_unit_keys():
    unit_id = security.new_unit_id()
    assert security.UNIT_ID_RE.match(unit_id)
    key = security.unit_key_for("pepper", unit_id, 1)
    secret = key.split(".", 1)[1]
    assert len(secret) == 43 and security.split_unit_key(key) == (unit_id, secret)
    # The key is derived: same inputs, same key; another version or secret, another key.
    assert security.unit_key_for("pepper", unit_id, 1) == key
    assert security.unit_key_for("pepper", unit_id, 2) != key
    assert security.unit_key_for("other", unit_id, 1) != key
    import base64

    assert base64.urlsafe_b64decode(secret + "=") == security.signing_key("pepper", unit_id, 1)
    stored = security.hash_unit_key("pepper", key)
    assert security.unit_key_matches("pepper", key, stored)
    assert not security.unit_key_matches("other pepper", key, stored)
    assert security.split_unit_key(f"{unit_id}.{'a' * 32}") is not None  # keys from before #9
    for bad in ("", "nodot", f"{unit_id}.short", f"U{unit_id[1:]}.{'a' * 43}", f"{unit_id}.{'a' * 42}!"):
        assert security.split_unit_key(bad) is None


def test_request_and_response_signatures():
    import hashlib
    import hmac

    key = b"k" * 32
    body = b'{"a":1}'
    expected = hmac.new(key, b"POST\n/api/unit/v1/batch\n1792138472\nabcdef0123456789\n"
                        + hashlib.sha256(body).hexdigest().encode(), hashlib.sha256).hexdigest()
    assert security.request_signature(key, "post", "/api/unit/v1/batch", "1792138472", "abcdef0123456789",
                                      body) == expected
    expected = hmac.new(key, b"abcdef0123456789\n200\n" + hashlib.sha256(body).hexdigest().encode(),
                        hashlib.sha256).hexdigest()
    assert security.response_signature(key, "abcdef0123456789", 200, body) == expected


def test_nonce_cache():
    cache = security.NonceCache(ttl_s=300, max_entries=3)
    assert cache.check_and_store("u1", "n1", 1000)
    assert not cache.check_and_store("u1", "n1", 1100)
    assert cache.check_and_store("u2", "n1", 1100)  # per unit
    assert cache.check_and_store("u1", "n1", 1301)  # expired
    for i in range(5):
        cache.check_and_store("u1", f"x{i}", 2000 + i)
    assert len(cache._seen) <= 5  # pruned once over the limit


def test_login_throttle_backs_off_and_resets():
    now = [0.0]
    throttle = security.LoginThrottle(free=2, base_s=2, max_s=10, clock=lambda: now[0])
    for _ in range(2):
        throttle.failed("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 0
    throttle.failed("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 2
    throttle.failed("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 4
    for _ in range(5):
        throttle.failed("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 10  # capped
    assert throttle.retry_after("5.6.7.8") == 0
    now[0] = 11
    assert throttle.retry_after("1.2.3.4") == 0
    throttle.succeeded("1.2.3.4")
    throttle.failed("1.2.3.4")
    assert throttle.retry_after("1.2.3.4") == 0


def test_development_defaults():
    s = load_settings(env={}, env_file=None)
    assert s.environment == "development" and not s.is_production
    assert s.database_url.endswith("@127.0.0.1:5433/fleet")
    assert s.admin_password_hash is None and not s.secure_cookies
    assert s.unit_defaults.as_dict() == {"sample_interval_s": 5.0, "batch_interval_s": 60.0,
                                         "scan_interval_s": 600.0, "server_ping_interval_s": 30.0,
                                         "ping_target": "gateway"}


def test_valid_production_settings():
    s = load_settings(env=PROD, env_file=None)
    assert s.is_production and s.secure_cookies
    assert s.public_origin == "https://wifi.example.school"


@pytest.mark.parametrize("change,message", [
    ({"DATABASE_URL": ""}, "DATABASE_URL"),
    ({"FLEET_SECRET_KEY": ""}, "FLEET_SECRET_KEY"),
    ({"FLEET_SECRET_KEY": "short"}, "FLEET_SECRET_KEY"),
    ({"FLEET_SECRET_KEY": "dev-only-secret-key-change-me"}, "FLEET_SECRET_KEY"),
    ({"FLEET_ADMIN_PASSWORD_HASH": ""}, "FLEET_ADMIN_PASSWORD_HASH"),
    ({"FLEET_ADMIN_PASSWORD_HASH": "plaintext"}, "hash-password"),
    ({"FLEET_PUBLIC_URL": "http://wifi.example.school"}, "https"),
    ({"ENVIRONMENT": "staging"}, "ENVIRONMENT"),
    ({"FLEET_SAMPLE_INTERVAL_S": "0.1"}, "between"),
    ({"FLEET_OFFLINE_AFTER_S": "soon"}, "number"),
])
def test_production_refuses_insecure_settings(change, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(env={**PROD, **change}, env_file=None)


def test_trusted_proxies():
    assert load_settings(env={}, env_file=None).trusted_proxies == ()
    s = load_settings(env={"FLEET_TRUSTED_PROXIES": "127.0.0.1, ::1,10.0.0.0/8"}, env_file=None)
    assert s.trusted_proxies == ("127.0.0.1", "::1", "10.0.0.0/8")
    with pytest.raises(ConfigError, match="FLEET_TRUSTED_PROXIES"):
        load_settings(env={"FLEET_TRUSTED_PROXIES": "caddy"}, env_file=None)


def test_timezone_setting():
    assert load_settings(env={}, env_file=None).timezone == "Europe/Tallinn"
    assert load_settings(env={"FLEET_TIMEZONE": "UTC"}, env_file=None).timezone == "UTC"
    with pytest.raises(ConfigError, match="FLEET_TIMEZONE"):
        load_settings(env={"FLEET_TIMEZONE": "Mars/Olympus"}, env_file=None)


def test_agent_update_settings(tmp_path):
    from fleet.config import DEFAULT_AGENT_PATH

    assert load_settings(env={}, env_file=None).agent_path == DEFAULT_AGENT_PATH
    assert load_settings(env={"FLEET_AGENT_UPDATES": "false"}, env_file=None).agent_path is None
    custom = load_settings(env={"FLEET_AGENT_PATH": str(tmp_path / "a.py")}, env_file=None)
    assert custom.agent_path == tmp_path / "a.py"
    with pytest.raises(ConfigError, match="FLEET_AGENT_UPDATES"):
        load_settings(env={"FLEET_AGENT_UPDATES": "maybe"}, env_file=None)


def test_env_example_documents_fleet_settings():
    example = parse_env_file(Path(__file__).resolve().parents[2] / ".env.example")
    expected = {"DATABASE_URL", "FLEET_SECRET_KEY", "FLEET_ADMIN_PASSWORD_HASH", "FLEET_PUBLIC_URL",
                "FLEET_SESSION_HOURS", "FLEET_OFFLINE_AFTER_S", "FLEET_SAMPLE_INTERVAL_S",
                "FLEET_BATCH_INTERVAL_S", "FLEET_SCAN_INTERVAL_S", "FLEET_SERVER_PING_INTERVAL_S",
                "FLEET_AGENT_UPDATES", "FLEET_AGENT_PATH", "FLEET_TRUSTED_PROXIES", "FLEET_TIMEZONE", "FLEET_UNIT_URL", "FLEET_FIRMWARE_DIR"}
    assert expected <= set(example)
    settings = load_settings(env=example, env_file=None)
    assert settings.environment == "development"
