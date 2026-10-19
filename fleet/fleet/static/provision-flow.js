// Programming a unit over USB (#4): shared by the "Add unit" page and the Node hardware check.
//
// Dependencies are passed in: `api` talks to the fleet server, `esptool` is the esptool-js module
// (ESPLoader, Transport). Progress goes to `onStep(step, detail)` and `onProgress(fraction)`.

import { MicroPythonREPL, SerialLink, ReplError } from "./serial-repl.js";

export const STEPS = ["connect", "firmware", "register", "files", "restart", "online"];
const HANDSHAKE_TIMEOUT_MS = 120000;

// Full-chip reset through the low-power watchdog, as Python esptool's `--after watchdog-reset` does for
// the ESP32-C5 (C6 register map, C3 sequence). The usual RTS reset restarts the CPU but not the
// USB-Serial/JTAG block, which the flasher stub leaves configured so MicroPython no longer receives
// input (Ctrl-C included) until a power cycle; esptool-js 0.7.0 has no C5 watchdog reset of its own.
const LP_WDT = { base: 0x600b1c00, config0: 0x0, config1: 0x4, wprotect: 0x18, key: 0x50d83aa1 };

export async function watchdogReset(loader) {
  const reg = (offset) => LP_WDT.base + offset;
  await loader.writeReg(reg(LP_WDT.wprotect), LP_WDT.key);  // unlock
  await loader.writeReg(reg(LP_WDT.config1), 2000);          // timeout
  await loader.writeReg(reg(LP_WDT.config0), ((1 << 31) | (5 << 28) | (1 << 8) | 2) >>> 0);  // enable: reset system
  await loader.writeReg(reg(LP_WDT.wprotect), 0);            // lock
  await sleep(500);
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const OPEN_HINT = "Could not open the board's USB port. Another program or browser tab may be using it " +
  "(a serial monitor, Arduino IDE, another tab of this page), or macOS may be asking to Allow the accessory. " +
  "Close those, unplug and replug the board, and try again.";

/** Turn the browser's bare "Failed to open serial port" into something the user can act on. */
export function explainOpenError(err) {
  return /Failed to open serial port|already open|Access denied|NetworkError/i.test(String(err?.message || err))
    ? new Error(OPEN_HINT) : err;
}

/** MAC addresses in one form: lowercase, and EUI-64 (aa:bb:cc:ff:fe:dd:ee:ff) reduced to the 6-byte MAC. */
export function normalizeMac(mac) {
  if (!mac) return null;
  const parts = String(mac).toLowerCase().split(":");
  if (parts.length === 8 && parts[3] === "ff" && parts[4] === "fe") parts.splice(3, 2);
  return parts.join(":");
}

/**
 * Fully reset the board through esptool-js: enter the ROM bootloader, check the chip, then reset
 * everything with the low-power watchdog. Needed because a unit whose agent ran with the hardware
 * watchdog stops reading USB input, and the plain RTS reset does not reset the USB-Serial/JTAG block.
 * The reset makes the board re-enumerate on USB, so the port is gone afterwards: see reacquire().
 * Returns { chip, mac }.
 */
export async function fullReset(port, firmware, esptool) {
  const transport = new esptool.Transport(port, false);
  const terminal = { clean() {}, writeLine() {}, write() {} };
  const loader = new esptool.ESPLoader({ transport, baudrate: 115200, terminal, debugLogging: false });
  try {
    let chip;
    try {
      chip = await loader.main();
    } catch (err) {
      throw explainOpenError(err);
    }
    if (!chip.startsWith(firmware.chip)) {
      throw new Error(`this page programs ${firmware.chip} boards; found ${chip}`);
    }
    let mac = null;
    try {
      mac = normalizeMac(await loader.chip.readMac(loader));
    } catch (err) { /* only used to recognise the board after the reset */ }
    await watchdogReset(loader);
    return { chip, mac };
  } finally {
    try { await transport.disconnect(); } catch (err) { /* the port may already be gone */ }
  }
}

/**
 * Find the board again after a watchdog reset (#11). The reset re-enumerates the USB device: in
 * Chrome the old SerialPort object can never be opened again and the board comes back as a new one,
 * which `serial.getPorts()` returns because the page was already allowed to use it. If the browser
 * does not hand it back, `requestReconnect()` asks the user to pick it once more. Without `serial`
 * (Node, which reopens by path) the same port is returned.
 */
export async function reacquire(oldPort, options = {}) {
  const { serial, requestReconnect, reacquireTimeoutMs = 10000 } = options;
  if (!serial) return oldPort;
  const want = oldPort.getInfo ? oldPort.getInfo() : {};
  const deadline = Date.now() + reacquireTimeoutMs;
  while (Date.now() < deadline) {
    await sleep(300);
    const ports = (await serial.getPorts()).filter((p) => {
      const info = p.getInfo ? p.getInfo() : {};
      return info.usbVendorId === want.usbVendorId && info.usbProductId === want.usbProductId;
    });
    for (const candidate of ports) {
      try {
        await candidate.open({ baudRate: 115200 });
        await candidate.close();
        return candidate;
      } catch (err) { /* the old object, or not ready yet */ }
    }
  }
  if (requestReconnect) return requestReconnect();
  throw new Error("The board did not come back after its reset. Unplug it, plug it back in and try again.");
}

async function checkSameBoard(expectedMac, board) {
  const found = normalizeMac(board.info?.mac);
  if (expectedMac && found && found !== expectedMac) {
    try { await board.repl.reboot(); } catch (e) { /* leave it running as it was */ }
    await board.link.close();
    throw new Error(`a different board answered after the reset (expected ${expectedMac}, got ${found}); ` +
                    "connect one board at a time");
  }
}

/** Open the port and stop main.py right after a reset. Returns { link, repl, info } (info null: no MicroPython). */
export async function openBoard(port, attempts = 5) {
  const link = new SerialLink(port);
  for (let i = 1; ; i++) {  // after a watchdog reset the USB device can take a moment to come back
    try {
      await link.open(115200);
      break;
    } catch (err) {
      if (i >= attempts) throw explainOpenError(err);
      await sleep(500);
    }
  }
  const repl = new MicroPythonREPL(link);
  const alive = await repl.interrupt(5000);
  let info = null;
  if (alive) {
    try {
      info = await repl.info();
    } catch (err) {
      if (!(err instanceof ReplError)) throw err;
    }
  }
  return { link, repl, info };
}

/** Erase the chip and write MicroPython with esptool-js, then fully reset it. The port is closed afterwards. */
export async function flashFirmware(port, firmware, data, esptool, onProgress) {
  const transport = new esptool.Transport(port, false);
  const terminal = { clean() {}, writeLine() {}, write() {} };
  const loader = new esptool.ESPLoader({ transport, baudrate: 460800, terminal, debugLogging: false });
  try {
    const chip = await loader.main();
    if (!chip.startsWith(firmware.chip)) {
      throw new Error(`this page programs ${firmware.chip} boards; found ${chip}`);
    }
    await loader.writeFlash({
      fileArray: [{ data, address: firmware.offset }],
      flashSize: "keep", flashMode: "keep", flashFreq: "keep",
      eraseAll: true, compress: true,
      reportProgress: (_index, written, total) => onProgress?.(written / total),
    });
    await watchdogReset(loader);
    return chip;
  } finally {
    try { await transport.disconnect(); } catch (err) { /* the port is gone after the reset */ }
  }
}

/**
 * Register the board. If its MAC already belongs to other units the server refuses (#14) and
 * `resolveExisting(existingUnits, request)` decides: { reprogram: unitId } to reuse that unit,
 * { retire: true } to register anew and retire the others. Anything else cancels.
 */
export async function register(api, request, resolveExisting) {
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      return await api.provision(request);
    } catch (err) {
      const existing = err?.status === 409 && err.body?.existing_units;
      if (!existing || !resolveExisting) throw err;
      const choice = await resolveExisting(existing, request);
      if (choice?.reprogram) request = { ...request, unit_id: choice.reprogram };
      else if (choice?.retire) request = { ...request, retire_existing: true };
      else throw new Error("cancelled: the board was not registered");
    }
  }
  throw new Error("could not register the board");
}

/** Increment the number at the end of a room name: "B-204" -> "B-205", "Lab 9" -> "Lab 10". */
export function nextRoomName(name) {
  const match = /^(.*?)(\d+)(\D*)$/.exec(name);
  if (!match) return name;
  const [, head, digits, tail] = match;
  const next = String(Number(digits) + 1).padStart(digits.length, "0");
  return `${head}${next}${tail}`;
}

/**
 * Program one board end to end. `port` is a Web Serial port (or compatible), not yet opened.
 * options: { name, notes, unitId, forceFlash, firmware, api, esptool, onStep, onProgress }
 */
export async function programBoard(port, options) {
  const { api, firmware, onStep = () => {}, onProgress = () => {} } = options;
  const started = Date.now();

  onStep("connect", "Resetting the board and looking for MicroPython");
  const { chip, mac } = await fullReset(port, firmware, options.esptool);
  let current = await reacquire(port, options);
  let board = await openBoard(current);
  await checkSameBoard(mac, board);
  const needsFlash = options.forceFlash || !board.info || board.info.version !== firmware.version;
  if (needsFlash) {
    onStep("firmware", board.info
      ? `Replacing MicroPython ${board.info.version} with ${firmware.version}`
      : `Installing MicroPython ${firmware.version}`);
    await board.link.close();
    const data = await api.firmware();
    await flashFirmware(current, firmware, data, options.esptool, onProgress);
    current = await reacquire(current, options);
    board = await openBoard(current);
    if (!board.info) throw new Error("MicroPython did not start after flashing");
    await checkSameBoard(mac, board);
  } else {
    onStep("firmware", `MicroPython ${board.info.version} already installed`);
  }
  const { link, repl, info } = board;
  let restarted = false;

  try {
    onStep("register", `Registering ${options.name} (board ${info.mac})`);
    const { unit, config } = await register(api, {
      name: options.name, notes: options.notes || null, mac: info.mac, unit_id: options.unitId || null },
      options.resolveExisting);

    onStep("files", "Copying the agent and its configuration");
    const files = [["main.py", await api.unitFile("main.py")], ["app.py", await api.unitFile("app.py")],
                   ["config.json", new TextEncoder().encode(JSON.stringify(config))]];
    const total = files.reduce((n, [, bytes]) => n + bytes.length, 0);
    let done = 0;
    await repl.clearUnitState();
    for (const [path, bytes] of files) {
      await repl.writeFile(path, bytes, (i, n) => onProgress((done + bytes.length * i / n) / total));
      done += bytes.length;
    }

    onStep("restart", "Restarting the board");
    await repl.reboot();
    restarted = true;
    await link.close();

    onStep("online", "Waiting for the unit's first handshake (Wi-Fi join and server contact)");
    // Wait for a handshake newer than the one known at registration (none for a new unit). Comparing
    // server timestamps with each other avoids depending on this computer's clock.
    const deadline = Date.now() + HANDSHAKE_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const current = await api.unit(unit.id);
      if (current.last_hello_at && current.last_hello_at !== unit.last_hello_at) {
        return { unit: current, info, chip, flashed: needsFlash, seconds: Math.round((Date.now() - started) / 1000) };
      }
      await sleep(2000);
    }
    throw new Error("the unit was programmed but has not contacted the server within 2 minutes; " +
                    "check that it is in range of the Wi-Fi network and look at the Events page");
  } catch (err) {
    // The board was paused at the MicroPython prompt for programming. Whatever went wrong (a refused
    // or cancelled registration, a lost connection), restart it so it carries on as before (#14).
    if (!restarted) {
      try { await repl.reboot(); } catch (e) { /* the board may already be gone */ }
    }
    await link.close();
    throw err;
  }
}
