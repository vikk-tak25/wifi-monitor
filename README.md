# wifimon

Wi-Fi measurement with ESP32 boards. This repository has two parts:

- **Single-board tool (`wifimon/`)**: one board on USB, a local dashboard on the Mac. Described first below.
- **Fleet server (`fleet/`)**: many networked units in school rooms reporting to a central server. See [Fleet server](#fleet-server).

## Single-board tool

Wi-Fi scanner and connection stability monitor. An ESP32 board measures, and a local web dashboard on the Mac shows the results.

- **Scan mode:** the board scans every nearby network on 2.4 GHz and 5 GHz and tracks each access point's signal strength over time.
- **Connection mode:** the board joins one network and pings the gateway (or a host you choose) to measure latency, packet loss, signal strength and disconnects.
- **Both:** stays connected and pings, and scans all networks less often.

Every sample is stored in SQLite, so past sessions can be reviewed and exported as CSV.

### Hardware

Tested with an **ESP32-C5** (ESP32-C5HF4 rev 1.0, dual-band Wi-Fi 6, 4 MB flash) on MicroPython **v1.29.0**. Other ESP32 boards running MicroPython should also work, but only the C5 and other dual-band chips can see 5 GHz networks.

One full dual-band scan takes about 10 seconds, so the shortest effective scan interval is about 10 s. While a scan runs, the board pauses pings (relevant in "both" mode).

### Setup

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
cp .env.example .env        # then edit if needed
```

#### Flash MicroPython (once)

Back up the factory firmware first, then flash (offset `0x2000` is for the ESP32-C5):

```sh
.venv/bin/esptool --port /dev/cu.usbmodem1101 read-flash 0 ALL backups/esp32c5-factory-flash.bin
curl -LO https://micropython.org/resources/firmware/ESP32_GENERIC_C5-20260824-v1.29.0.bin
.venv/bin/esptool --port /dev/cu.usbmodem1101 erase-flash
.venv/bin/esptool --port /dev/cu.usbmodem1101 --baud 460800 write-flash 0x2000 ESP32_GENERIC_C5-20260824-v1.29.0.bin
```

To restore the factory firmware: `.venv/bin/esptool --port /dev/cu.usbmodem1101 write-flash 0 backups/esp32c5-factory-flash.bin`

#### Install the board agent

```sh
.venv/bin/wifimon install-agent
```

This copies `device/agent.py` and `device/main.py` to the board, so the agent starts on every boot. Run it again after changing the agent.

### Run

```sh
.venv/bin/wifimon --open          # real board, opens http://localhost:8765/
.venv/bin/wifimon --simulate      # synthetic data, no hardware needed
```

Pick a mode in the dashboard and press **Start session**. The board is found automatically (USB vendor ID `0x303a`). If you unplug it, the server reconnects and resumes the session.

Stop the server with Ctrl-C. Any running session is closed and the board is told to stop.

### Configuration

All settings come from environment variables or `.env` (see `.env.example`). Real environment variables win.

| Variable | Default | Purpose |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` refuses the simulated device and keeps INFO logging |
| `WIFIMON_DEVICE` | `serial` | `serial` (the board) or `simulated` |
| `WIFIMON_SERIAL_PORT` | auto | Serial port of the board |
| `WIFIMON_HTTP_HOST` | `127.0.0.1` | Dashboard address. The dashboard has no login, so keep it local |
| `WIFIMON_HTTP_PORT` | `8765` | Dashboard port |
| `WIFIMON_DB` | `data/wifimon.db` | SQLite database |
| `WIFI_SSID` / `WIFI_PASSWORD` | empty | Default network for connection mode |
| `WIFIMON_PING_TARGET` | gateway | Host to ping in connection mode |

The Wi-Fi password is only sent to the board when a session starts. It is never written to the database, the logs, or the board's filesystem.

### Statistics

Per access point (BSSID):

- **Mean / min / max / std dev** of RSSI in dBm
- **Seen:** share of scans the network appeared in
- **Drops:** how many times it vanished from scans and came back
- **Stability (0 to 100):** 60% presence (seen share) + 40% steadiness, where steadiness falls linearly to 0 at an RSSI standard deviation of 8 dB

For the connection: uptime, packet loss, latency median / p95 / jitter (mean difference between consecutive pings), RSSI, disconnects and the longest outage.

### Architecture

```
ESP32 (MicroPython)            Mac
device/agent.py  ── USB serial, JSON lines ──>  wifimon/device.py   (serial or simulated)
                 <── commands ───────────────   wifimon/monitor.py  (sessions, live events)
                                                wifimon/storage.py  (SQLite)
                                                wifimon/stats.py    (stability numbers)
                                                wifimon/server.py   (HTTP API, SSE, CSV)
                                                wifimon/static/     (dashboard)
```

Board messages are one JSON object per line: `hello`, `status`, `scan`, `link`, `event`, `error`. Commands are `hello`, `scan`, `connect`, `both` and `stop`.

### Tests

```sh
.venv/bin/pytest
```

The tests need no hardware. They cover the protocol, statistics, storage, the HTTP API (including an end-to-end run with the simulated device), the serial reconnect logic, and the board agent itself, run under CPython with a fake WLAN.

## Fleet server

Central server for 50+ ESP32-C5 units placed permanently in rooms. Each unit joins the school Wi-Fi with preprogrammed credentials, authenticates with its own key, and uploads measurements in batches.

Stack: FastAPI, PostgreSQL + TimescaleDB, Docker Compose. Work is tracked in issues #2 to #7.

### Run locally

```sh
docker compose up --build            # database + server on http://localhost:8800
```

Without a `FLEET_ADMIN_PASSWORD_HASH` in `.env`, the development admin password is `admin`. API docs (development only): http://localhost:8800/api/docs

To run the server on the host instead (for example while editing code):

```sh
docker compose up -d db
uv pip install --python .venv/bin/python -e 'fleet[dev]'
.venv/bin/fleet serve --migrate --host 0.0.0.0
```

Register a unit from the command line (browser provisioning follows in #4):

```sh
.venv/bin/fleet create-unit --name "B-204"
```

The key is printed once. The server stores only an HMAC of it.

### Units (`unit/`)

Each room unit runs `unit/main.py` (boot loader) and `unit/app.py` (agent) on MicroPython. The agent:

- joins the configured Wi-Fi network, pinned to the strongest access point of that network (the firmware cannot report the BSSID of a normal connection, so pinning is how the unit knows which AP it measures)
- performs the handshake, then every 5 s records signal strength, AP and channel, and a ping to the gateway; every 30 s it also measures the round trip to the server
- scans all networks every 10 minutes, and uploads a batch every minute
- keeps batches on flash (up to 300) while the server is unreachable and sends them later, also after a reboot
- reports roaming, Wi-Fi loss, connection failures, reboots and crashes as events
- restarts itself after 10 minutes without Wi-Fi or 60 minutes without the server, and runs under a hardware watchdog
- installs a new agent version offered by the server, verified by SHA-256; `main.py` restores the previous version if the new one crashes or does not complete a handshake within two starts

Times are kept as integers: MicroPython floats on the ESP32 are single precision, which would put a Unix timestamp about 100 s off.

Units talk plain HTTP and sign every request (#9). An ESP32-C5 without PSRAM cannot keep enough contiguous memory free for TLS once the agent runs (the ESP-IDF heap's largest free block falls to about 1.7 KB; TLS needs about 35 KB). Signing protects the unit key, integrity and code updates; measurement data is readable in transit.

Until browser provisioning (#4) exists, a unit is set up by hand:

```sh
.venv/bin/fleet create-unit --name "B-204"     # prints the unit key once
cat > /tmp/config.json <<'JSON'
{"wifi_ssid": "VIKK", "wifi_password": "...", "server_url": "http://<server address>:8800",
 "unit_key": "<key from create-unit>", "watchdog_s": 120}
JSON
.venv/bin/mpremote cp unit/main.py :main.py + cp unit/app.py :app.py + cp /tmp/config.json :config.json + reset
```

Set `"watchdog_s": 0` while developing: the hardware watchdog cannot be stopped and would reset the board while you work in the REPL. Press Ctrl-C right after a reset to get the REPL without starting the agent.

### Dashboard

`https://<server>/` (or http://localhost:8800 locally) after signing in with the admin password:

- **Rooms**: every unit side by side for the last hour, 24 hours or 7 days: median signal with range and standard deviation, gateway packet loss, latency median and p95, server round trip, uptime, Wi-Fi drops, roams and a 0 to 100 stability score (50% uptime, 30% packet loss, 20% signal steadiness). Offline units are flagged.
- **Room**: candlestick charts (open, high, low, close per 5 min, 15 min, 1 hour or 1 day) for signal strength, gateway latency and server round trip, each with a median line; latency charts also show packet loss per candle. Plus the access points the unit used, the networks heard in its latest scan, and its events.
- **Events**: everything units and the server reported, filterable by room, level and kind.
- **Units**: status, last seen, agent and firmware versions, MAC, IP and current access point.

Candles and medians are exact (computed from raw samples). Daily candles start at midnight in `FLEET_TIMEZONE` (default `Europe/Tallinn`). The 7-day room comparison is recomputed in the background every 4 minutes because it takes about 2 s with 50 units; `fleet/scripts/benchmark_dashboard.py` measures all dashboard queries with 50 units and 7 days of synthetic data.

For a dashboard with something in it during development:

```sh
.venv/bin/fleet seed-demo --units 6 --days 3     # refused when ENVIRONMENT=production
```

### Floor plans

**Floors** shows the units on uploaded floor plans (PNG, JPEG, WebP or SVG, up to 5 MB each, stored in the database). Tick **Place units** to drag units onto their rooms, move them, or remove them (×); positions are stored as fractions of the image, so replacing a plan with a sharper scan keeps them. Outside edit mode each unit is coloured by the chosen metric (median signal, packet loss, latency, stability or uptime) for the top bar's time window, with an optional heat layer interpolated between units (inverse-distance weighting, fading out where there are no units). Clicking a unit opens its room.

Uploads are checked by their bytes, not the declared type. SVGs with scripts, event handlers, `javascript:` or external links are refused, and plans are served with `nosniff` and, for SVG, a sandboxing Content-Security-Policy.

### Adding units from the browser

1. **Settings** (once): enter the Wi-Fi network and password units should join. The password is stored encrypted (Fernet, key derived from `FLEET_SECRET_KEY`) and only leaves the server inside a unit's configuration. Choose the label size and fields.
2. On the server (once): `fleet fetch-firmware` downloads MicroPython for the ESP32-C5, pinned by version and SHA-256. micropython.org sends no CORS headers, so the browser fetches it from our server.
3. **Units → Add unit** in Chrome or Edge (Web Serial): plug the board in, type the room name, press **Connect board and program**. The page resets the board, installs MicroPython if it is missing or a different version, registers the unit, copies `main.py`, `app.py` and `config.json`, restarts it and waits for its first handshake. About 25 seconds, or about 40 with MicroPython installation.
4. Print the label (adjustable size and fields, with a QR code to the room page). With **Several boards in a row** the page offers the next room name (`B-204` → `B-205`) for the next board.

**Reprogram** on the Units page does the same for an existing unit (new room, new board, changed Wi-Fi): it issues a new key and the old one stops working.

Two ESP32-C5 details the page handles: esptool-js 0.7.0 cannot do the full watchdog reset this chip needs after flashing, so the page does it itself (as Python esptool's `--after watchdog-reset`); and a unit that ran with its hardware watchdog stops reading USB input until such a reset, so every programming run starts with one.

`fleet/scripts/hardware/` runs the same browser modules from Node against a real board (see its README); `node --test fleet/tests/js/*.test.mjs` tests the serial protocol.

### Unit API

| Endpoint | Purpose |
|---|---|
| `GET /api/unit/v1/time` | Server time in ms, unauthenticated, so a unit can sign its first request |
| `POST /api/unit/v1/hello` | Handshake at boot: returns server time, unit name and measurement config |
| `POST /api/unit/v1/batch` | Upload samples, scans and events. Idempotent per `batch_id`; clock skew over 2 s is corrected from `sent_at` |
| `GET /api/unit/v1/ping` | Cheap round trip for server latency |
| `GET /api/unit/v1/agent` | Download the agent offered in the handshake (`update` field) |

Units sign requests (#9). The unit key is `<unit id>.<base64url signing key>`; the server derives the signing key from `FLEET_SECRET_KEY`, the unit ID and a key version, and stores no per-unit secret (rotating a key increments the version; changing `FLEET_SECRET_KEY` invalidates every unit key). Each request carries:

| Header | Value |
|---|---|
| `X-Unit-Id` | unit ID |
| `X-Timestamp` | epoch seconds, accepted within 120 s of server time |
| `X-Nonce` | random hex, rejected if reused within 5 minutes |
| `X-Signature` | hex HMAC-SHA256 of `METHOD\nPATH\nTIMESTAMP\nNONCE\nsha256hex(body)` |

Responses to signed requests carry `X-Signature` = HMAC of `NONCE\nSTATUS\nsha256hex(body)`, and the unit rejects anything else. `Authorization: Bearer <unit key>` still works over HTTPS; in production it is refused over plain HTTP. A unit counts as offline after `FLEET_OFFLINE_AFTER_S` (default 10 minutes) of silence.

### Admin API

Login with `POST /api/admin/login`. Sessions are HttpOnly, SameSite=Strict cookies. Writes also need `X-Requested-With: fleet`, and cross-origin requests are refused. Endpoints: units (list, create, rename, configure, rotate key, revoke), events (filter by unit, level, kind) and settings.

### Production settings

`ENVIRONMENT=production` refuses to start without `DATABASE_URL`, a random `FLEET_SECRET_KEY` (32+ characters), a `FLEET_ADMIN_PASSWORD_HASH` (from `fleet hash-password`) and an `https://` `FLEET_PUBLIC_URL`. Deployment with HTTPS, retention and backups is issue #7.

### Tests

```sh
docker compose up -d db
cd fleet && ../.venv/bin/pytest
```

The tests create a separate `fleet_test` database on the development server, so development data is never touched.
