// Tests for the heat map's colours and interpolation (fleet/static/heatmap.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { METRICS, cssColor, fade, interpolate, rgbFor, score } from "../../fleet/static/heatmap.js";

test("scores run from bad (0) to good (1) and clamp", () => {
  assert.equal(score("rssi_median", -50), 1);
  assert.equal(score("rssi_median", -85), 0);
  assert.equal(score("rssi_median", -30), 1);
  assert.equal(score("rssi_median", -95), 0);
  assert.ok(Math.abs(score("rssi_median", -67.5) - 0.5) < 1e-9);
  assert.equal(score("gw_loss_pct", 0), 1);   // lower is better
  assert.equal(score("gw_loss_pct", 10), 0);
  assert.equal(score("gw_median_ms", 52.5), 0.5);
  assert.equal(score("stability", 100), 1);
  assert.equal(score("uptime_pct", 97.5), 0.5);
  assert.equal(score("rssi_median", null), null);
  assert.equal(score("unknown", 1), null);
  for (const m of Object.values(METRICS)) assert.ok(m.label && m.unit && m.good !== m.bad);
});

test("colours go red to amber to green", () => {
  assert.deepEqual(rgbFor(0), [220, 38, 38]);
  assert.deepEqual(rgbFor(0.5), [217, 119, 6]);
  assert.deepEqual(rgbFor(1), [22, 163, 74]);
  assert.deepEqual(rgbFor(2), [22, 163, 74]);
  assert.equal(cssColor(1), "rgb(22, 163, 74)");
  assert.equal(cssColor(0, 0.5), "rgba(220, 38, 38, 0.5)");
});

test("interpolation is exact at a unit and weighted in between", () => {
  const pts = [{ x: 0, y: 0, t: 1 }, { x: 10, y: 0, t: 0 }];
  assert.deepEqual(interpolate(pts, 0, 0), { t: 1, nearest: 0 });
  assert.ok(Math.abs(interpolate(pts, 5, 0).t - 0.5) < 1e-9);
  assert.ok(interpolate(pts, 2, 0).t > 0.9);
  assert.equal(interpolate(pts, 2, 0).nearest, 2);
  assert.equal(interpolate([], 1, 1), null);
});

test("the heat layer fades out away from units", () => {
  assert.equal(fade(0, 100), 0.55);
  assert.ok(fade(100, 100) < 0.25);
  assert.ok(fade(300, 100) < 0.001);
});
