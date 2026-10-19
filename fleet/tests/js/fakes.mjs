// Test doubles for the browser provisioning modules: a MicroPython board speaking the raw REPL,
// Web Serial ports that behave like Chrome's, and an esptool-js stand-in.

const OPEN_ERROR = "Failed to execute 'open' on 'SerialPort': Failed to open serial port.";

/** The board itself: filesystem, firmware and REPL state survive across USB reconnections. */
export class FakeBoard {
  constructor({ micropython = "1.29.0", mac = "02:00:00:c5:6e:38", reconnectMs = 300, comesBack = true } = {}) {
    this.micropython = micropython;  // null: factory firmware, nothing answers on the REPL
    this.mac = mac;
    this.files = new Map();
    this.ports = [];
    this.reconnectMs = reconnectMs;
    this.comesBack = comesBack;
    this.watchdogResets = 0;
    this.port = this.newPort();
  }

  newPort() {
    const port = new ChromeLikePort(this);
    this.ports.push(port);
    return port;
  }

  /** A full reset re-enumerates USB: the old port object is gone for good, a new one appears later. */
  watchdogReset() {
    this.watchdogResets += 1;
    this.port.unplug();
    this.port = null;
    if (this.comesBack) setTimeout(() => { this.port = this.newPort(); }, this.reconnectMs);
  }
}

/** Like Chrome's SerialPort: fresh streams per open(); unusable after the device re-enumerates. */
export class ChromeLikePort {
  constructor(board) {
    this.board = board;
    this.gone = false;
    this.isOpen = false;
    this.signals = [];
    this.raw = false;
    this.code = "";
  }

  getInfo() { return { usbVendorId: 0x303a, usbProductId: 0x1001 }; }

  async open() {
    if (this.gone) throw new Error(OPEN_ERROR);
    if (this.isOpen) throw new Error("Failed to execute 'open' on 'SerialPort': The port is already open.");
    this.isOpen = true;
    const port = this;
    this.readable = new ReadableStream({ start(c) { port.controller = c; } });
    this.writable = new WritableStream({ write(chunk) { port.receive(new TextDecoder().decode(chunk)); } });
  }

  async close() {
    this.isOpen = false;
    try { this.controller?.close(); } catch (e) { /* already closed */ }
  }

  unplug() {
    this.gone = true;
    try { this.controller?.error(new Error("The device has been lost.")); } catch (e) { /* closed */ }
    this.isOpen = false;
  }

  async setSignals(s) { this.signals.push(s); }

  send(text) {
    try { this.controller.enqueue(new TextEncoder().encode(text)); } catch (e) { /* stream closed */ }
  }

  receive(text) {
    if (!this.board.micropython) return;
    for (const ch of text) {
      if (ch === "\x03") { this.raw = false; this.code = ""; this.send("\r\nKeyboardInterrupt\r\n>>> "); }
      else if (ch === "\x01") { this.raw = true; this.code = ""; this.send("raw REPL; CTRL-B to exit\r\n>"); }
      else if (ch === "\x02") { this.raw = false; this.send("\r\n>>> "); }
      else if (ch === "\x04" && this.raw) { this.run(this.code); this.code = ""; }
      else if (this.raw) this.code += ch;
    }
  }

  run(code) {
    const b = this.board;
    this.send("OK");
    let out = "", err = "", m;
    if (code.includes("raise")) err = "Traceback (most recent call last):\r\nValueError: boom\r\n";
    else if ((m = /_f=open\("([^"]+)",'wb'\)/.exec(code))) { b.openPath = m[1]; b.chunks = []; }
    else if ((m = /_f\.write\(_b\.a2b_base64\('([^']*)'\)\)/.exec(code))) b.chunks.push(Buffer.from(m[1], "base64"));
    else if (code.startsWith("_f.close()")) b.files.set(b.openPath, Buffer.concat(b.chunks));
    else if ((m = /os'\)\.stat\("([^"]+)"\)\[6\]/.exec(code))) out = `${(b.files.get(m[1]) || []).length}\r\n`;
    else if (code.includes("implementation.version")) {
      out = JSON.stringify({ version: b.micropython, machine: "ESP32C5 module with ESP32-C5", mac: b.mac }) + "\r\n";
    } else if (code.includes("listdir()")) out = JSON.stringify([...b.files.keys()]) + "\r\n";
    this.send(`${out}\x04${err}\x04>`);
  }
}

/** The parts of esptool-js the provisioning flow uses. Writing the watchdog enable value resets the board. */
export function fakeEsptool(board, { chip = "ESP32-C5 (revision v1.0)" } = {}) {
  class Transport {
    constructor(port) { this.port = port; }
    async connect() { await this.port.open({ baudRate: 115200 }); }
    async disconnect() { if (!this.port.gone) await this.port.close(); }
  }
  class ESPLoader {
    constructor({ transport }) { this.transport = transport; this.chip = { readMac: async () => board.mac }; }
    async main() { await this.transport.connect(); return chip; }
    async writeReg(address, value) {
      if (address === 0x600b1c00 && value === 0xd0000102) setTimeout(() => board.watchdogReset(), 20);
    }
    async writeFlash({ fileArray, reportProgress }) {
      reportProgress?.(0, fileArray[0].data.length, fileArray[0].data.length);
      board.micropython = "1.29.0";
      board.files.clear();
    }
    async after() {}
  }
  return { Transport, ESPLoader };
}

/** navigator.serial stand-in: only ports of connected devices the page may use. */
export function fakeSerial(board) {
  return { getPorts: async () => (board.port ? [board.port] : []) };
}

/** The fleet server API as the flow uses it. */
export function fakeApi(board) {
  const calls = [];
  let helloAfterReboot = false;
  return {
    calls,
    markOnline() { helloAfterReboot = true; },
    firmware: async () => { calls.push("firmware"); return new Uint8Array(64); },
    unitFile: async (name) => { calls.push(`file:${name}`); return new TextEncoder().encode(`# ${name}\n`); },
    provision: async (body) => {
      calls.push(["provision", body]);
      return { unit: { id: "u3k7x2m9qpa", name: body.name, last_hello_at: null },
               config: { wifi_ssid: "SchoolNet", unit_key: "u3k7x2m9qpa.key" } };
    },
    unit: async (id) => ({ id, name: "B-204", last_hello_at: board.files.has("config.json") ? "2026-10-16T12:00:00Z" : null,
                           last_bssid: "02:00:00:aa:00:40", last_channel: 6 }),
  };
}
