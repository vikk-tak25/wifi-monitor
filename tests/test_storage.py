from wifimon.protocol import Link
from wifimon.storage import Storage

from .conftest import make_scan


def test_session_lifecycle(storage):
    sid = storage.create_session(mode="scan", started_at=10.0, scan_interval=15, link_interval=1, note="lab")
    session = storage.get_session(sid)
    assert session["mode"] == "scan" and session["ended_at"] is None and session["note"] == "lab"
    storage.end_session(sid, 99.0)
    storage.end_session(sid, 150.0)  # already ended: keeps the first end time
    assert storage.get_session(sid)["ended_at"] == 99.0
    assert storage.get_session(12345) is None


def test_scans_and_observations_round_trip(storage):
    sid = storage.create_session(mode="scan", started_at=0.0)
    scan_id = storage.add_scan(sid, 5.0, make_scan(("A", "aa", 1, -40), ("B", "bb", 36, -70)))
    storage.add_scan(sid, 20.0, make_scan(("A", "aa", 1, -42)))
    assert [s["ap_count"] for s in storage.scans(sid)] == [2, 1]
    rows = storage.observations(sid)
    assert len(rows) == 3 and rows[0]["scan_id"] == scan_id
    assert [r["rssi"] for r in storage.observations(sid, ["aa"])] == [-40, -42]


def test_link_samples_and_since_filter(storage):
    sid = storage.create_session(mode="connect", started_at=0.0, ssid="Home")
    storage.add_link(sid, 1.0, Link(True, -50, 3.2, True, "10.0.0.1"))
    storage.add_link(sid, 2.0, Link(False, None, None, False))
    samples = storage.link_samples(sid)
    assert samples[0] == {"ts": 1.0, "connected": True, "rssi": -50, "latency_ms": 3.2, "ok": True,
                          "target": "10.0.0.1"}
    assert samples[1]["connected"] is False
    assert len(storage.link_samples(sid, since=1.0)) == 1


def test_events_newest_first_and_counted(storage):
    sid = storage.create_session(mode="connect", started_at=0.0)
    storage.add_event(sid, 1.0, "connected", "Connected", level="success", detail={"ip": "10.0.0.5"})
    storage.add_event(sid, 2.0, "disconnected", "Lost", level="warning")
    storage.add_event(None, 3.0, "device_connected", "Board connected")
    events = storage.events(sid)
    assert [e["kind"] for e in events] == ["disconnected", "connected"]
    assert events[1]["detail"] == {"ip": "10.0.0.5"}
    assert storage.count_events(sid, "disconnected") == 1
    assert len(storage.events()) == 3


def test_close_open_sessions_uses_last_sample_time(storage):
    a = storage.create_session(mode="scan", started_at=0.0)
    storage.add_scan(a, 40.0, make_scan(("A", "aa", 1, -40)))
    b = storage.create_session(mode="connect", started_at=50.0)
    assert storage.close_open_sessions(1000.0) == 2
    assert storage.get_session(a)["ended_at"] == 40.0
    assert storage.get_session(b)["ended_at"] == 50.0


def test_list_and_delete_sessions(storage):
    a = storage.create_session(mode="scan", started_at=1.0)
    storage.add_scan(a, 2.0, make_scan(("A", "aa", 1, -40)))
    b = storage.create_session(mode="connect", started_at=5.0)
    storage.add_link(b, 6.0, Link(True, -50, 1.0, True))
    listed = storage.list_sessions()
    assert [s["id"] for s in listed] == [b, a]
    assert listed[1]["scan_count"] == 1 and listed[0]["link_count"] == 1
    assert storage.delete_session(a) is True
    assert storage.observations(a) == [] and storage.get_session(a) is None
    assert storage.delete_session(a) is False


def test_data_survives_reopen(tmp_path):
    path = tmp_path / "sub" / "wifi.db"
    db = Storage(path)
    sid = db.create_session(mode="scan", started_at=1.0)
    db.close()
    again = Storage(path)
    assert again.get_session(sid)["mode"] == "scan"
    again.close()
