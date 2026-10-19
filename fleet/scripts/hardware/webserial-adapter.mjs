// Gives a USB serial port (node-serialport) the Web Serial SerialPort shape, so the browser's
// provisioning modules can run unchanged in Node for hardware checks.
import { SerialPort } from "serialport";

export class WebSerialAdapter {
  constructor(path) { this.path = path; this.sp = null; this.readable = null; this.writable = null; }
  getInfo() { return { usbVendorId: 0x303a, usbProductId: 0x1001 }; }
  async open({ baudRate }) {
    const sp = this.sp = new SerialPort({ path: this.path, baudRate, autoOpen: false });
    await new Promise((res, rej) => sp.open((e) => (e ? rej(e) : res())));
    this.readable = new ReadableStream({
      start(c) {
        sp.on("data", (d) => c.enqueue(new Uint8Array(d)));
        sp.on("close", () => { try { c.close(); } catch (e) { /* closed */ } });
      },
    });
    this.writable = new WritableStream({
      // No drain(): tcdrain can block indefinitely on the ESP32 USB-Serial/JTAG port after a chip reset.
      write: (chunk) => new Promise((res, rej) => sp.write(Buffer.from(chunk), (e) => (e ? rej(e) : res()))),
    });
  }
  // Web Serial changes only the lines it is given; node-serialport's set() resets omitted lines to
  // their defaults (DTR on), which holds the ESP32 in download mode. Track and always send both.
  async setSignals({ dataTerminalReady, requestToSend }) {
    this.lines = this.lines || { dtr: false, rts: false };
    if (dataTerminalReady !== undefined) this.lines.dtr = dataTerminalReady;
    if (requestToSend !== undefined) this.lines.rts = requestToSend;
    await new Promise((res, rej) => this.sp.set({ ...this.lines }, (e) => (e ? rej(e) : res())));
  }
  async close() {
    if (this.sp?.isOpen) await new Promise((res) => this.sp.close(() => res()));
  }
}
