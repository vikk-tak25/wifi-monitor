// Hardware check for #4: runs fleet/static/provision-flow.js against a real board and a real server.
//   node run-provision.mjs <serial port> <server> <room name> [unit id] [--flash]
import { WebSerialAdapter } from "./webserial-adapter.mjs";

const STATIC = new URL("../../fleet/static", import.meta.url).pathname;
const { programBoard } = await import(`${STATIC}/provision-flow.js`);
const esptool = await import(`${STATIC}/vendor/esptool-js-0.7.0.bundle.js`);
const [portPath, server, name, maybeUnit] = process.argv.slice(2);
const unitId = maybeUnit && maybeUnit.startsWith("u") ? maybeUnit : null;
const forceFlash = process.argv.includes("--flash");

let cookie = "";
async function call(method, path, body, raw = false) {
  const res = await fetch(server + path, { method, headers: { "Content-Type": "application/json", "X-Requested-With": "fleet", Cookie: cookie },
                                            body: body === undefined ? undefined : JSON.stringify(body) });
  const setCookie = res.headers.get("set-cookie");
  if (setCookie) cookie = setCookie.split(";")[0];
  if (!res.ok) throw new Error(`${method} ${path}: ${res.status} ${await res.text()}`);
  return raw ? new Uint8Array(await res.arrayBuffer()) : res.json();
}
await call("POST", "/api/admin/login", { password: process.env.FLEET_PASSWORD });
const { firmware } = await call("GET", "/api/admin/provisioning");
const api = {
  firmware: () => call("GET", "/api/admin/provisioning/firmware.bin", undefined, true),
  provision: (body) => call("POST", "/api/admin/provision", body),
  unitFile: (n) => call("GET", `/api/admin/provisioning/files/${n}`, undefined, true),
  unit: async (id) => (await call("GET", `/api/admin/units/${id}`)).unit,
};
const t0 = Date.now();
let lastPct = -1;
const result = await programBoard(new WebSerialAdapter(portPath), {
  name, unitId, forceFlash, firmware, api, esptool,
  onStep: (step, detail) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1).padStart(5)} s] ${step.padEnd(8)} ${detail}`),
  onProgress: (f) => { const p = Math.floor(f * 10) * 10; if (p !== lastPct) { lastPct = p; process.stdout.write(`  ${p}%`); if (p === 100) process.stdout.write("\n"); } },
});
console.log(`\nDONE in ${result.seconds} s: ${result.unit.name} (${result.unit.id}) ${result.unit.status}, flashed=${result.flashed}, ` +
            `board ${result.info.mac}, MicroPython ${result.info.version}, agent ${result.unit.agent_version}, AP ${result.unit.last_bssid} ch ${result.unit.last_channel}`);
process.exit(0);
