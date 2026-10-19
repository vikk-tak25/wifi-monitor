// Tests for the browser's MicroPython raw REPL client (fleet/static/serial-repl.js), run with:
//   node --test fleet/tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { MicroPythonREPL, SerialLink, ReplError, TimeoutError, fileWriteScripts, toBase64 }
  from "../../fleet/static/serial-repl.js";
import { nextRoomName } from "../../fleet/static/provision-flow.js";

/** A fake MicroPython board speaking the raw REPL protocol, with a tiny filesystem. */
class FakeBoard {
  constructor({ alive = true, version = "1.29.0" } = {}) {
    this.alive = alive;
    this.version = version;
    this.files = new Map();
    this.signals = [];
    this.raw = false;
    this.code = "";
    this.openPath = null;
    this.chunks = [];
    const board = this;
    this.readable = new ReadableStream({ start(controller) { board.controller = controller; } });
    this.writable = new WritableStream({ write(chunk) { board.receive(new TextDecoder().decode(chunk)); } });
  }
  async open() {}
  async close() { try { this.controller.close(); } catch (e) { /* closed */ } }
  async setSignals(s) { this.signals.push(s); }
  send(text) { this.controller.enqueue(new TextEncoder().encode(text)); }
  receive(text) {
    if (!this.alive) return;
    for (const ch of text) {
      if (ch === "\x03") { this.raw = false; this.code = ""; this.send("\r\nKeyboardInterrupt\r\n>>> "); }
      else if (ch === "\x01") { this.raw = true; this.code = ""; this.send("raw REPL; CTRL-B to exit\r\n>"); }
      else if (ch === "\x02") { this.raw = false; this.send("\r\n>>> "); }
      else if (ch === "\x04" && this.raw) { this.run(this.code); this.code = ""; }
      else if (this.raw) this.code += ch;
    }
  }
  run(code) {
    this.send("OK");
    let out = "", err = "";
    let m;
    if (code.includes("raise")) err = "Traceback (most recent call last):\r\n  File \"<stdin>\"\r\nValueError: boom\r\n";
    else if ((m = /_f=open\("([^"]+)",'wb'\)/.exec(code))) { this.openPath = m[1]; this.chunks = []; }
    else if ((m = /_f\.write\(_b\.a2b_base64\('([^']*)'\)\)/.exec(code))) this.chunks.push(Buffer.from(m[1], "base64"));
    else if (code.startsWith("_f.close()")) this.files.set(this.openPath, Buffer.concat(this.chunks));
    else if ((m = /os'\)\.stat\("([^"]+)"\)\[6\]/.exec(code))) out = `${(this.files.get(m[1]) || []).length}\r\n`;
    else if (code.includes("implementation.version")) out = JSON.stringify({ version: this.version, machine: "ESP32C5 module with ESP32-C5", mac: "02:00:00:c5:00:01" }) + "\r\n";
    else if (code.includes("listdir()")) out = JSON.stringify([...this.files.keys()]) + "\r\n";
    this.send(`${out}\x04${err}\x04>`);
  }
}

async function connect(options) {
  const board = new FakeBoard(options);
  const link = new SerialLink(board);
  await link.open();
  return { board, link, repl: new MicroPythonREPL(link) };
}

test("hard reset pulses RTS like esptool", async () => {
  const { board, repl, link } = await connect();
  await repl.hardReset();
  assert.deepEqual(board.signals, [{ dataTerminalReady: false, requestToSend: true }, { requestToSend: false }]);
  await link.close();
});

test("interrupt finds the REPL, or reports a board without MicroPython", async () => {
  const live = await connect();
  assert.equal(await live.repl.interrupt(2000), true);
  await live.link.close();
  const dead = await connect({ alive: false });
  assert.equal(await dead.repl.interrupt(600), false);
  await dead.link.close();
});

test("info and exec in raw mode", async () => {
  const { repl, link } = await connect();
  assert.deepEqual(await repl.info(), { version: "1.29.0", machine: "ESP32C5 module with ESP32-C5", mac: "02:00:00:c5:00:01" });
  await assert.rejects(repl.exec("raise ValueError('boom')"), (err) => err instanceof ReplError && err.message === "ValueError: boom");
  await link.close();
});

test("writeFile transfers bytes exactly and verifies the size", async () => {
  const { board, repl, link } = await connect();
  const bytes = new Uint8Array(10000).map((_, i) => (i * 7) % 256);
  let progress = 0;
  await repl.writeFile("app.py", bytes, (i, n) => { progress = i / n; });
  assert.deepEqual(new Uint8Array(board.files.get("app.py")), bytes);
  assert.equal(progress, 1);
  assert.deepEqual(await repl.listFiles(), ["app.py"]);
  await link.close();
});

test("readUntil times out instead of hanging", async () => {
  const { link } = await connect({ alive: false });
  await assert.rejects(link.readUntil(">>> ", 300), TimeoutError);
  await link.close();
});

test("file scripts split into base64 chunks", () => {
  const scripts = fileWriteScripts("config.json", new TextEncoder().encode("x".repeat(7000)), 3072);
  assert.equal(scripts.length, 5);  // open, 3 chunks, close
  assert.equal(toBase64(new Uint8Array([0, 255, 104, 105])), "AP9oaQ==");
});

test("next room name increments the trailing number", () => {
  assert.equal(nextRoomName("B-204"), "B-205");
  assert.equal(nextRoomName("Lab 9"), "Lab 10");
  assert.equal(nextRoomName("Room 007"), "Room 008");
  assert.equal(nextRoomName("A-101 (window)"), "A-102 (window)");
  assert.equal(nextRoomName("Library"), "Library");
});
