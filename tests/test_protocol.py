import json

import pytest

from wifimon.protocol import (AccessPoint, DeviceError, Event, Hello, Link, ProtocolError, Scan, Status,
                              auth_name, band_for_channel, encode_command, parse_line, start_command)


def test_scan_message_is_parsed_into_access_points():
    line = json.dumps({"t": "scan", "ms": 100, "n": 3, "dur": 9701,
                       "aps": [["VIKK", "02:00:00:AA:00:40", 13, -30, 3, False],
                               ["", "02:00:00:bb:00:b0", 108, -73, 7, True]]})
    msg = parse_line(line)
    assert isinstance(msg, Scan)
    assert msg.number == 3 and msg.duration_ms == 9701
    assert msg.aps[0] == AccessPoint("VIKK", "02:00:00:aa:00:40", 13, -30, 3, False)
    assert msg.aps[0].band == "2.4 GHz"
    assert msg.aps[1].band == "5 GHz" and msg.aps[1].hidden


def test_link_message_requires_latency_for_ok():
    msg = parse_line('{"t":"link","connected":true,"rssi":-55,"lat":3.4,"ok":true,"target":"10.0.0.1"}')
    assert msg == Link(connected=True, rssi=-55, latency_ms=3.4, ok=True, target="10.0.0.1")
    lost = parse_line('{"t":"link","connected":true,"rssi":-55,"lat":null,"ok":true}')
    assert lost.ok is False


def test_other_message_types():
    assert isinstance(parse_line(b'{"t":"hello","agent":"1","fw":"1.29.0","mode":null}\r\n'), Hello)
    assert parse_line('{"t":"status","mode":"scan","connected":false,"mem":1000}') == Status("scan", False, 1000)
    event = parse_line('{"t":"event","kind":"disconnected","status":200}')
    assert event == Event("disconnected", {"status": 200})
    assert parse_line('{"t":"error","message":"boom"}') == DeviceError("boom")


@pytest.mark.parametrize("line", [
    "", "MicroPython v1.29.0 on 2026-08-24", ">>> import agent", "{not json", '["t","scan"]', '{"no_type":1}',
    "E (190) MSPI Timing: Failed to allocate dummy cacheline",
])
def test_non_protocol_lines_are_ignored(line):
    assert parse_line(line) is None


@pytest.mark.parametrize("line", [
    '{"t":"scan","aps":"nope"}',
    '{"t":"scan","aps":[["a","b",1,-50,3]]}',
    '{"t":"scan","aps":[["a","b","one",-50,3,false]]}',
    '{"t":"link","connected":true,"rssi":"strong"}',
    '{"t":"event"}',
    '{"t":"mystery"}',
])
def test_malformed_messages_raise(line):
    with pytest.raises(ProtocolError):
        parse_line(line)


def test_start_command_includes_credentials_only_for_connection_modes():
    scan = start_command("scan", scan_interval=15, link_interval=1, ssid="x", password="y")
    assert scan == {"cmd": "scan", "scan_interval": 15, "link_interval": 1}
    both = start_command("both", scan_interval=60, link_interval=1, ssid="Home", password="pw", target="1.1.1.1")
    assert both["ssid"] == "Home" and both["password"] == "pw" and both["target"] == "1.1.1.1"
    with pytest.raises(ProtocolError):
        start_command("connect", scan_interval=1, link_interval=1)
    with pytest.raises(ProtocolError):
        start_command("sniff", scan_interval=1, link_interval=1)


def test_encode_command_is_one_json_line():
    data = encode_command({"cmd": "hello"})
    assert data == b'{"cmd":"hello"}\n'


def test_band_and_auth_names():
    assert band_for_channel(1) == "2.4 GHz"
    assert band_for_channel(165) == "5 GHz"
    assert band_for_channel(None) == "?"
    assert auth_name(0) == "Open"
    assert auth_name(7) == "WPA2/WPA3"
    assert auth_name(99) == "auth 99"
