"""Command line: `python -m wifimon [serve|install-agent]`."""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import subprocess
import sys
import threading
import webbrowser
from dataclasses import replace
from pathlib import Path

from .config import ConfigError, load_settings
from .device import SerialDevice, SimulatedDevice, find_espressif_port
from .monitor import Monitor
from .server import make_server
from .storage import Storage

DEVICE_DIR = Path(__file__).resolve().parent.parent / "device"


def serve(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.simulate:
        settings = replace(settings, device="simulated")
    if args.port is not None:
        settings = replace(settings, http_port=args.port)
    if settings.device == "simulated" and settings.is_production:
        raise ConfigError("the simulated device is not allowed when ENVIRONMENT=production")

    logging.basicConfig(
        level=logging.INFO if settings.is_production else logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    device = SimulatedDevice() if settings.device == "simulated" else SerialDevice(settings.serial_port)
    storage = Storage(settings.db_path)
    monitor = Monitor(storage, device, settings)
    server = make_server(monitor, storage, settings.http_host, settings.http_port)
    url = f"http://{'localhost' if settings.http_host in ('127.0.0.1', '::1') else settings.http_host}:{server.server_address[1]}/"

    monitor.start()
    logging.getLogger("wifimon").info("dashboard at %s (device: %s, db: %s)", url, device.kind, settings.db_path)
    if args.open:
        threading.Timer(0.5, webbrowser.open, (url,)).start()

    def stop(signum: int, frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.shutdown()
        server.server_close()
        storage.close()
    return 0


def install_agent(args: argparse.Namespace) -> int:
    """Copy the MicroPython agent to the board and restart it."""
    settings = load_settings()
    port = args.serial_port or settings.serial_port or find_espressif_port()
    if not port:
        print("No Espressif board found. Plug it in or pass --serial-port.", file=sys.stderr)
        return 1
    mpremote = shutil.which("mpremote") or str(Path(sys.executable).parent / "mpremote")
    if not Path(mpremote).exists():
        print("mpremote is not installed. Run: uv pip install -e '.[dev]'", file=sys.stderr)
        return 1
    cmd = [mpremote, "connect", port,
           "cp", str(DEVICE_DIR / "agent.py"), ":agent.py", "+",
           "cp", str(DEVICE_DIR / "main.py"), ":main.py", "+", "reset"]
    print("Installing agent on", port)
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wifimon", description="ESP32 Wi-Fi scanner and stability monitor")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the dashboard (default)")
    p_serve.add_argument("--simulate", action="store_true", help="use simulated data instead of the board")
    p_serve.add_argument("--port", type=int, help="HTTP port (overrides WIFIMON_HTTP_PORT)")
    p_serve.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    p_serve.add_argument("-v", "--verbose", action="store_true", help="debug logging, including raw board output")
    p_serve.set_defaults(func=serve)

    p_install = sub.add_parser("install-agent", help="copy the MicroPython agent to the board")
    p_install.add_argument("--serial-port", help="serial port (default: auto-detect)")
    p_install.set_defaults(func=install_agent)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("serve", "install-agent", "-h", "--help"):
        argv = ["serve", *argv]
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
