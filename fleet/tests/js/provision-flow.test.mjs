// Tests for the "Add unit" flow (fleet/static/provision-flow.js) with Chrome-like ports. Run with:
//   node --test fleet/tests/js/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { programBoard, register } from "../../fleet/static/provision-flow.js";
import { FakeBoard, fakeApi, fakeEsptool, fakeSerial } from "./fakes.mjs";

const FIRMWARE = { chip: "ESP32-C5", version: "1.29.0", offset: 0x2000 };

function run(board, extra = {}) {
  const steps = [];
  const options = {
    name: "B-204", firmware: FIRMWARE, api: fakeApi(board), esptool: fakeEsptool(board), serial: fakeSerial(board),
    onStep: (step) => steps.push(step), ...extra,
  };
  return { steps, promise: programBoard(board.port, options) };
}

test("reacquires the board after the watchdog reset re-enumerates USB (#11)", async () => {
  const board = new FakeBoard();
  const first = board.port;
  const { promise } = run(board);
  const result = await promise;
  assert.equal(result.unit.name, "B-204");
  assert.equal(first.gone, true, "the first port object died with the reset");
  assert.ok(board.ports.length >= 2, "the flow continued on the new port object");
  assert.deepEqual([...board.files.keys()].sort(), ["app.py", "config.json", "main.py"]);
  assert.equal(result.flashed, false);
});

test("reacquires the board again after flashing", async () => {
  const board = new FakeBoard({ micropython: null });
  const { promise, steps } = run(board);
  const result = await promise;
  assert.equal(result.flashed, true);
  assert.equal(board.watchdogResets, 2, "one reset to connect, one after flashing");
  assert.ok(board.ports.length >= 3);
  assert.deepEqual(steps, ["connect", "firmware", "register", "files", "restart", "online"]);
});

test("asks the user to pick the board again when the browser does not hand it back", async () => {
  const board = new FakeBoard({ comesBack: false });
  let asked = 0;
  const { promise } = run(board, {
    serial: { getPorts: async () => [] },
    reacquireTimeoutMs: 600,
    requestReconnect: async () => { asked += 1; board.port = board.newPort(); return board.port; },
  });
  await promise;
  assert.equal(asked, 1);
});

test("refuses a different board after the reset", async () => {
  const board = new FakeBoard();
  const esptool = fakeEsptool(board);
  const realLoader = esptool.ESPLoader;
  esptool.ESPLoader = class extends realLoader {
    constructor(...args) { super(...args); this.chip = { readMac: async () => "aa:bb:cc:dd:ee:ff" }; }
  };
  const { promise } = run(board, { esptool });
  await assert.rejects(promise, /different board/);
});

test("explains a port that cannot be opened", async () => {
  const board = new FakeBoard();
  board.port.gone = true;  // e.g. held by another program
  const { promise } = run(board);
  await assert.rejects(promise, /another program or browser tab|Allow/);
});


// -- a board that is already registered (#14) -------------------------------------------------------

function conflictApi(existing) {
  const calls = [];
  return {
    calls,
    provision: async (body) => {
      calls.push(body);
      const others = existing.filter((u) => u.id !== body.unit_id);
      if (others.length && !body.retire_existing) {
        const err = new Error("this board is already registered");
        err.status = 409;
        err.body = { existing_units: others };
        throw err;
      }
      return { unit: { id: body.unit_id || "unew0000000", name: body.name }, config: {} };
    },
  };
}

test("an already registered board is reprogrammed when the user chooses so (#14)", async () => {
  const api = conflictApi([{ id: "uold0000000", name: "Klassiruum-3" }]);
  const result = await register(api, { name: "Klassiruum-2", mac: "02:00:00:c5:12:54", unit_id: null },
    async (existing) => ({ reprogram: existing[0].id }));
  assert.equal(result.unit.id, "uold0000000");
  assert.equal(api.calls.length, 2);
  assert.equal(api.calls[1].unit_id, "uold0000000");
});

test("or registered anew with the old registrations retired", async () => {
  const api = conflictApi([{ id: "ua000000000", name: "Klassiruum-2" }, { id: "ub000000000", name: "Klassiruum-3" }]);
  const result = await register(api, { name: "Klassiruum-4", mac: "x", unit_id: null }, async () => ({ retire: true }));
  assert.equal(result.unit.id, "unew0000000");
  assert.equal(api.calls[1].retire_existing, true);
});

test("cancelling leaves the board unregistered, and without a resolver the conflict surfaces", async () => {
  const api = conflictApi([{ id: "uold0000000", name: "Klassiruum-3" }]);
  await assert.rejects(register(api, { name: "X", unit_id: null }, async () => null), /cancelled/);
  await assert.rejects(register(api, { name: "X", unit_id: null }), (err) => err.status === 409);
});

test("a cancelled or refused registration leaves the board running (#14)", async () => {
  const board = new FakeBoard();
  const api = fakeApi(board);
  api.provision = async () => {
    const err = new Error("this board is already registered as Klassiruum-3");
    err.status = 409;
    err.body = { existing_units: [{ id: "uold0000000", name: "Klassiruum-3" }] };
    throw err;
  };
  const { promise } = run(board, { api, resolveExisting: async () => null });
  await assert.rejects(promise, /cancelled/);
  const resets = board.port.signals.filter((s) => s.requestToSend === true);
  assert.equal(resets.length, 1, "the board was restarted into its program");
  assert.equal(board.port.isOpen, false, "and the port released");
  assert.equal(board.files.size, 0, "nothing was written");
});
