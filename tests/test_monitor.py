import pytest

from wifimon.monitor import Monitor, ValidationError, describe_event
from wifimon.protocol import DeviceError, Event, Hello, Link, Status

from .conftest import FakeDevice, make_scan


@pytest.fixture
def device():
    return FakeDevice()


@pytest.fixture
def monitor(storage, device, settings, clock):
    m = Monitor(storage, device, settings, clock=clock)
    m.start()
    return m


def test_board_is_told_to_idle_when_no_session(monitor, device):
    assert device.sent == [{"cmd": "stop"}]


def test_scan_session_stores_scans(monitor, device, storage, clock):
    session = monitor.start_session("scan", scan_interval=10)
    assert device.sent[-1] == {"cmd": "scan", "scan_interval": 10.0, "link_interval": 1.0}
    clock.advance(10)
    device.deliver(make_scan(("A", "aa", 1, -40), ("B", "bb", 36, -70)))
    device.deliver(Link(True, -50, 2.0, True))  # ignored: not a connection session
    assert len(storage.scans(session["id"])) == 1
    assert storage.link_samples(session["id"]) == []
    assert monitor.last_scan["count"] == 2


def test_connection_session_uses_env_password_and_never_stores_it(monitor, device, storage, tmp_path):
    session = monitor.start_session("connect", ssid="HomeNet", password="")
    assert device.sent[-1]["password"] == "env-secret"
    assert session["ssid"] == "HomeNet"
    device.deliver(Link(True, -52, 3.1, True, "192.168.1.1"))
    assert storage.link_samples(session["id"])[0]["latency_ms"] == 3.1
    monitor.stop_session()
    assert device.sent[-1] == {"cmd": "stop"}
    raw = (tmp_path / "test.db").read_bytes() + b"".join(
        p.read_bytes() for p in tmp_path.glob("test.db-*"))
    assert b"env-secret" not in raw


def test_explicit_password_for_other_network(monitor, device):
    monitor.start_session("both", ssid="Office", password="office-pw", target="1.1.1.1")
    cmd = device.sent[-1]
    assert cmd["cmd"] == "both" and cmd["password"] == "office-pw" and cmd["target"] == "1.1.1.1"
    assert cmd["scan_interval"] == 60.0


@pytest.mark.parametrize("kwargs,message", [
    ({"mode": "sniff"}, "mode"),
    ({"mode": "scan", "scan_interval": "abc"}, "number"),
    ({"mode": "scan", "scan_interval": 0}, "between"),
    ({"mode": "connect", "ssid": "x" * 33}, "32 bytes"),
    ({"mode": "connect", "ssid": "Net", "password": "p" * 65}, "64"),
    ({"mode": "connect", "ssid": "Net", "target": "evil; rm -rf"}, "hostname"),
    ({"mode": "connect", "link_interval": 0.01, "ssid": "Net"}, "between"),
])
def test_invalid_session_requests(monitor, kwargs, message):
    mode = kwargs.pop("mode")
    with pytest.raises(ValidationError, match=message):
        monitor.start_session(mode, **kwargs)


def test_connect_without_any_ssid_is_rejected(storage, device, clock):
    from wifimon.config import Settings

    m = Monitor(storage, device, Settings(), clock=clock)
    with pytest.raises(ValidationError, match="ssid is required"):
        m.start_session("connect")


def test_starting_a_new_session_ends_the_previous_one(monitor, storage, clock):
    first = monitor.start_session("scan")
    clock.advance(30)
    second = monitor.start_session("scan")
    assert storage.get_session(first["id"])["ended_at"] == clock.now
    assert monitor.current_session_id() == second["id"]


def test_session_resumes_after_unplug_and_replug(monitor, device, storage, clock):
    session = monitor.start_session("connect", ssid="HomeNet")
    device.unplug()
    assert device.send({"cmd": "x"}) is False
    device.plug()
    assert device.sent[-1]["cmd"] == "connect"  # resent on reconnect
    # The board rebooted and its agent says hello in idle mode: resend immediately.
    device.sent.clear()
    device.deliver(Hello({"agent": "1", "mode": None}))
    assert device.sent and device.sent[-1]["cmd"] == "connect"
    kinds = [e["kind"] for e in storage.events(session["id"])]
    assert "device_disconnected" in kinds and "device_connected" in kinds and "session_resumed" in kinds


def test_idle_status_resend_is_rate_limited(monitor, device, clock):
    monitor.start_session("scan")
    device.sent.clear()
    device.deliver(Status(None, False))
    assert device.sent == []  # just sent the command; status may be stale
    clock.advance(20)
    device.deliver(Status(None, False))
    assert device.sent == [{"cmd": "scan", "scan_interval": 15.0, "link_interval": 1.0}]
    device.sent.clear()
    device.deliver(Status("scan", False))
    assert device.sent == []


def test_session_started_while_board_offline(storage, settings, clock):
    device = FakeDevice(online=False)
    m = Monitor(storage, device, settings, clock=clock)
    m.start()
    session = m.start_session("scan")
    assert device.sent == []
    assert storage.events(session["id"])[0]["kind"] == "device_offline"
    device.plug()
    assert device.sent[-1]["cmd"] == "scan"


def test_board_events_and_errors_are_logged(monitor, device, storage):
    session = monitor.start_session("connect", ssid="HomeNet")
    device.deliver(Event("disconnected", {"status": 202}))
    device.deliver(DeviceError("scan failed: boom"))
    events = storage.events(session["id"])
    assert events[1]["message"] == "Connection lost: wrong password" and events[1]["level"] == "warning"
    assert events[0]["level"] == "error"
    assert storage.count_events(session["id"], "disconnected") == 1


def test_subscribers_receive_live_updates(monitor, device):
    q = monitor.subscribe()
    monitor.start_session("scan")
    device.deliver(make_scan(("A", "aa", 1, -40)))
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait()[0])
    assert "session" in kinds and "scan" in kinds and "event" in kinds
    monitor.unsubscribe(q)


def test_restart_closes_sessions_left_open(storage, settings, clock):
    sid = storage.create_session(mode="scan", started_at=clock.now - 100)
    Monitor(storage, FakeDevice(), settings, clock=clock).start()
    assert storage.get_session(sid)["ended_at"] is not None


def test_status_does_not_expose_password(monitor):
    monitor.start_session("connect", ssid="HomeNet", password="hunter2")
    status = monitor.status()
    assert "hunter2" not in repr(status) and "env-secret" not in repr(status)
    assert status["defaults"]["has_password"] is True


def test_describe_event_messages():
    assert describe_event("connected", {"ip": "10.0.0.2", "channel": 6})[1] == "Connected (IP 10.0.0.2, channel 6)"
    assert describe_event("connect_timeout", {"status": 201}) == ("warning", "Connection attempt timed out: network not found")
    assert describe_event("something_new", {}) == ("info", "something new")
