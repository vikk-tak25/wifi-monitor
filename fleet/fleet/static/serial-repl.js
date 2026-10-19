// MicroPython raw REPL over Web Serial (#4).
//
// Used by the "Add unit" page to read a board's identity and copy files onto it. Plain ES module with
// no browser-only dependencies, so the same code runs in Node for tests and hardware checks: anything
// with the Web Serial `SerialPort` shape (open, readable, writable, setSignals, close) works.
//
// Protocol (as in MicroPython's mpremote): Ctrl-A enters raw mode; code followed by Ctrl-D runs it;
// the board answers "OK", the output, Ctrl-D, the error output, Ctrl-D and ">". Ctrl-B leaves raw mode.

const CTRL_A = "\x01", CTRL_B = "\x02", CTRL_C = "\x03", CTRL_D = "\x04";
const RAW_BANNER = "raw REPL; CTRL-B to exit\r\n>";
const encoder = new TextEncoder();
const decoder = new TextDecoder();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class ReplError extends Error {}
export class TimeoutError extends ReplError {}

export function toBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

/** Python snippets that write `bytes` to `path` in base64 pieces, one exec() each. */
export function fileWriteScripts(path, bytes, chunkBytes = 3072) {
  const scripts = [`import ubinascii as _b\n_f=open(${JSON.stringify(path)},'wb')`];
  for (let i = 0; i < bytes.length; i += chunkBytes) {
    scripts.push(`_f.write(_b.a2b_base64('${toBase64(bytes.subarray(i, i + chunkBytes))}'))`);
  }
  scripts.push("_f.close()\ndel _f,_b");
  return scripts;
}

export class SerialLink {
  constructor(port) {
    this.port = port;
    this.buffer = "";
    this.reader = null;
    this.readLoop = null;
    this.wake = null;
    this.error = null;
  }

  async open(baudRate = 115200) {
    await this.port.open({ baudRate });
    this.reader = this.port.readable.getReader();
    this.readLoop = (async () => {
      try {
        for (;;) {
          const { value, done } = await this.reader.read();
          if (done) break;
          this.buffer += decoder.decode(value, { stream: true });
          if (this.buffer.length > 1_000_000) this.buffer = this.buffer.slice(-100_000);
          this.wake?.();
        }
      } catch (err) {
        this.error = err;
        this.wake?.();
      }
    })();
  }

  async close() {
    try { await this.reader?.cancel(); } catch (e) { /* already closed */ }
    try { await this.readLoop; } catch (e) { /* ignore */ }
    try { this.reader?.releaseLock(); } catch (e) { /* ignore */ }
    try { await this.port.close(); } catch (e) { /* ignore */ }
  }

  async write(data) {
    const writer = this.port.writable.getWriter();
    try {
      await writer.write(typeof data === "string" ? encoder.encode(data) : data);
    } finally {
      writer.releaseLock();
    }
  }

  clear() {
    this.buffer = "";
  }

  /** Wait until the buffer contains `marker`; consume and return everything before it. */
  async readUntil(marker, timeoutMs = 5000) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const at = this.buffer.indexOf(marker);
      if (at >= 0) {
        const before = this.buffer.slice(0, at);
        this.buffer = this.buffer.slice(at + marker.length);
        return before;
      }
      if (this.error) throw new ReplError(`serial port error: ${this.error.message || this.error}`);
      const left = deadline - Date.now();
      if (left <= 0) throw new TimeoutError(`timed out waiting for ${JSON.stringify(marker)}`);
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, Math.min(left, 200));
        this.wake = () => { clearTimeout(timer); resolve(); };
      });
    }
  }

  async setSignals(signals) {
    await this.port.setSignals(signals);
  }
}

export class MicroPythonREPL {
  constructor(link) {
    this.link = link;
    this.raw = false;
  }

  /** Reset the chip through the USB-serial control lines (as esptool does for USB-Serial/JTAG). */
  async hardReset() {
    await this.link.setSignals({ dataTerminalReady: false, requestToSend: true });
    await sleep(120);
    await this.link.setSignals({ requestToSend: false });
    this.raw = false;
  }

  /**
   * Get to the REPL. After a reset, Ctrl-C keeps arriving while main.py waits its first second, so the
   * agent and its hardware watchdog never start. Returns false when nothing answers (no MicroPython).
   */
  async interrupt(totalMs = 4000) {
    this.link.clear();
    const deadline = Date.now() + totalMs;
    while (Date.now() < deadline) {
      await this.link.write(`\r${CTRL_C}`);
      await sleep(150);
      if (this.link.buffer.includes(">>> ")) {
        await sleep(300);
        await this.link.write(`\r${CTRL_C}`);
        await sleep(200);
        this.link.clear();
        return true;
      }
    }
    return false;
  }

  async enterRaw() {
    this.link.clear();
    await this.link.write(`\r${CTRL_A}`);
    await this.link.readUntil(RAW_BANNER, 3000);
    this.raw = true;
  }

  async exitRaw() {
    if (!this.raw) return;
    await this.link.write(`\r${CTRL_B}`);
    this.raw = false;
  }

  /** Run code in raw mode; resolves with its output, rejects with the last line of a traceback. */
  async exec(code, timeoutMs = 10000) {
    if (!this.raw) await this.enterRaw();
    const bytes = encoder.encode(code);
    for (let i = 0; i < bytes.length; i += 256) {  // small pieces so the board's input buffer keeps up
      await this.link.write(bytes.subarray(i, i + 256));
      await sleep(4);
    }
    await this.link.write(CTRL_D);
    await this.link.readUntil("OK", 3000);
    const stdout = await this.link.readUntil(CTRL_D, timeoutMs);
    const stderr = await this.link.readUntil(CTRL_D, timeoutMs);
    await this.link.readUntil(">", 2000);
    if (stderr.trim()) throw new ReplError(stderr.trim().split("\n").pop());
    return stdout;
  }

  async evalJson(expr, timeoutMs) {
    const out = await this.exec(`import json\nprint(json.dumps(${expr}))`, timeoutMs);
    return JSON.parse(out.trim());
  }

  /** Board identity: MicroPython version, machine string and Wi-Fi MAC address. */
  async info() {
    return this.evalJson(
      "(lambda s, n: {'version': '.'.join(str(v) for v in s.implementation.version[:3]), " +
      "'machine': s.implementation._machine, " +
      "'mac': ':'.join('%02x' % b for b in n.WLAN(n.WLAN.IF_STA).config('mac'))})" +
      "(__import__('sys'), __import__('network'))");
  }

  async listFiles() {
    return this.evalJson("__import__('os').listdir()");
  }

  async writeFile(path, bytes, onProgress) {
    const scripts = fileWriteScripts(path, bytes);
    for (let i = 0; i < scripts.length; i++) {
      await this.exec(scripts[i]);
      onProgress?.(i + 1, scripts.length);
    }
    const size = await this.evalJson(`__import__('os').stat(${JSON.stringify(path)})[6]`);
    if (size !== bytes.length) throw new ReplError(`${path}: wrote ${size} of ${bytes.length} bytes`);
  }

  /** Remove state left by an earlier configuration (queued batches belong to the old unit key). */
  async clearUnitState() {
    await this.exec(
      "import os\n" +
      "for _p in ('state.json', 'update_pending.json', 'update_failed.json', 'last_error.txt',\n" +
      "           'app_prev.py', 'app_new.py'):\n" +
      "    try:\n        os.remove(_p)\n    except OSError:\n        pass\n" +
      "try:\n    for _p in os.listdir('spool'):\n        os.remove('spool/' + _p)\nexcept OSError:\n    pass\n");
  }

  /** Restart the board into main.py. */
  async reboot() {
    await this.exitRaw();
    await sleep(100);
    await this.hardReset();
  }
}
