"""SerialDevice against a fake serial port: framing, handshake and reconnect."""

import threading

from wifimon.device import SerialDevice, SimulatedDevice
from wifimon.protocol import Hello, Scan

from .conftest import wait_for


class FakeSerial:
    """Minimal pyserial stand-in. `script` maps written bytes to replies."""

    instances = []

    def __init__(self, fail_open=False):
        self.fail_open = fail_open
        self.port = None
        self.baudrate = None
        self.timeout = None
        self.written = []
        self._inbox = bytearray()
        self._lock = threading.Lock()
        self.closed = False
        self.broken = False
        self.on_write = None
        FakeSerial.instances.append(self)

    def open(self):
        if self.fail_open:
            raise OSError("busy")

    def feed(self, data):
        with self._lock:
            self._inbox += data

    @property
    def in_waiting(self):
        return len(self._inbox)

    def read(self, n):
        if self.broken:
            raise OSError("device unplugged")
        with self._lock:
            chunk = bytes(self._inbox[:n])
            del self._inbox[:n]
        if not chunk:
            threading.Event().wait(0.005)
        return chunk

    def write(self, data):
        self.written.append(bytes(data))
        if self.on_write:
            self.on_write(self, bytes(data))
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


def make_device(**kwargs):
    FakeSerial.instances = []
    received = []
    states = []
    device = SerialDevice(port_finder=lambda: "/dev/cu.fake", reconnect_delay=0.05,
                          handshake_timeout=0.1, scan_grace=0.2, serial_factory=FakeSerial, **kwargs)
    device.on_message = received.append
    device.on_state = lambda connected, state: states.append((connected, state.get("detail")))
    return device, received, states


def test_lines_are_framed_across_chunks_and_noise_ignored():
    device, received, states = make_device()
    device.start()
    try:
        assert wait_for(lambda: FakeSerial.instances)
        ser = FakeSerial.instances[0]
        ser.feed(b"MicroPython v1.29.0\r\n{\"t\":\"hello\",\"agent\":\"1\",")
        ser.feed(b"\"mode\":null}\r\n{\"t\":\"scan\",\"n\":1,\"dur\":9700,\"aps\":[[\"A\",\"aa\",1,-40,3,false]]}\n")
        ser.feed(b"{\"t\":\"scan\",\"aps\":\"broken\"}\n")
        assert wait_for(lambda: len(received) == 2)
        assert isinstance(received[0], Hello) and device.info["agent"] == "1"
        assert isinstance(received[1], Scan)
        assert states[0] == (True, "connected")
    finally:
        device.stop()


def test_silent_board_gets_hello_then_repl_restart():
    device, received, _ = make_device()

    def board(ser, data):
        if b"import agent" in data:
            ser.feed(b'{"t":"hello","agent":"1","mode":null}\n')

    original = FakeSerial.__init__

    def patched(self, *a, **k):
        original(self, *a, **k)
        self.on_write = board

    FakeSerial.__init__ = patched
    try:
        device.start()
        assert wait_for(lambda: received, timeout=3)
        writes = b"".join(FakeSerial.instances[0].written)
        assert writes.index(b'{"cmd":"hello"}') < writes.index(b"\x03\x03") < writes.index(b"import agent")
    finally:
        FakeSerial.__init__ = original
        device.stop()


def test_reconnects_after_unplug():
    device, _, states = make_device()
    device.start()
    try:
        assert wait_for(lambda: FakeSerial.instances)
        FakeSerial.instances[0].broken = True
        assert wait_for(lambda: len(FakeSerial.instances) >= 2, timeout=3)
        assert wait_for(lambda: device.connected)
        details = [d for _, d in states]
        assert details[:3] == ["connected", "disconnected", "connected"]
        assert FakeSerial.instances[0].closed
    finally:
        device.stop()


def test_no_board_found_reports_state():
    device = SerialDevice(port_finder=lambda: None, reconnect_delay=0.05)
    states = []
    device.on_state = lambda connected, state: states.append((connected, state["detail"]))
    device.start()
    try:
        assert wait_for(lambda: states)
        assert device.send({"cmd": "hello"}) is False
        assert device.connected is False
        assert device.state["detail"] == "no Espressif board found"
    finally:
        device.stop()


def test_simulated_device_runs_a_session():
    device = SimulatedDevice(seed=1, speed=500)
    received = []
    device.on_message = received.append
    device.start()
    try:
        assert wait_for(lambda: device.connected and received)
        assert isinstance(received[0], Hello)
        device.send({"cmd": "scan", "scan_interval": 15})
        assert wait_for(lambda: sum(isinstance(m, Scan) for m in received) >= 3)
        scan = next(m for m in received if isinstance(m, Scan))
        assert {ap.band for ap in scan.aps} == {"2.4 GHz", "5 GHz"}
    finally:
        device.stop()
    assert device.connected is False
