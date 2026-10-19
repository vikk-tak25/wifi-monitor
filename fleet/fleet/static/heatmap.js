// Colours and interpolation for the floor-plan heat map (#6). Pure functions plus one canvas
// painter; an ES module so the same code is tested in Node.

/** Metrics the map can show, with the value that counts as good and the one that counts as bad. */
export const METRICS = {
  rssi_median: { label: "Signal (median)", unit: "dBm", good: -50, bad: -85, digits: 0 },
  gw_loss_pct: { label: "Packet loss", unit: "%", good: 0, bad: 10, digits: 1 },
  gw_median_ms: { label: "Latency (median)", unit: "ms", good: 5, bad: 100, digits: 1 },
  stability: { label: "Stability", unit: "/100", good: 100, bad: 40, digits: 0 },
  uptime_pct: { label: "Uptime", unit: "%", good: 100, bad: 95, digits: 1 },
};

/** 0 (bad) to 1 (good) for a metric value; null when there is no value. */
export function score(metric, value) {
  const m = METRICS[metric];
  if (!m || value == null || Number.isNaN(value)) return null;
  const t = (value - m.bad) / (m.good - m.bad);
  return Math.max(0, Math.min(1, t));
}

/** Red (0) through amber to green (1), as [r, g, b]. */
export function rgbFor(t) {
  const stops = [[220, 38, 38], [217, 119, 6], [22, 163, 74]];
  const x = Math.max(0, Math.min(1, t)) * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(x));
  const f = x - i;
  return stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f));
}

export function cssColor(t, alpha = 1) {
  const [r, g, b] = rgbFor(t);
  return alpha === 1 ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/**
 * Inverse-distance-weighted score at (x, y) from points [{ x, y, t }] (same units as x, y).
 * Returns { t, nearest } with nearest the distance to the closest point, or null without points.
 */
export function interpolate(points, x, y, power = 2) {
  let num = 0, den = 0, nearest = Infinity;
  for (const p of points) {
    const d = Math.hypot(p.x - x, p.y - y);
    if (d < 1e-6) return { t: p.t, nearest: 0 };
    const w = 1 / d ** power;
    num += w * p.t;
    den += w;
    nearest = Math.min(nearest, d);
  }
  return den ? { t: num / den, nearest } : null;
}

/** Opacity of the heat layer: strong next to a unit, fading out where there is no measurement. */
export function fade(nearest, radius, maxAlpha = 0.55) {
  return maxAlpha * Math.exp(-((nearest / radius) ** 2));
}

/**
 * Paint the heat layer onto a canvas of size width x height (CSS pixels, scaled by `ratio`).
 * points: [{ x, y, t }] with x, y as fractions 0 to 1 of the plan. Computed on a coarse grid and
 * scaled up smoothly, so it stays fast on large plans.
 */
export function paintHeat(canvas, points, width, height, ratio = 1, cell = 6) {
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const usable = points.filter((p) => p.t != null);
  if (!usable.length) return;
  const cols = Math.max(2, Math.ceil(width / cell)), rows = Math.max(2, Math.ceil(height / cell));
  const grid = makeCanvas(cols, rows);
  const gctx = grid.getContext("2d");
  const img = gctx.createImageData(cols, rows);
  const pts = usable.map((p) => ({ x: p.x * width, y: p.y * height, t: p.t }));
  const radius = Math.hypot(width, height) * 0.16;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const at = interpolate(pts, (c + 0.5) * cell, (r + 0.5) * cell);
      const [red, green, blue] = rgbFor(at.t);
      const o = (r * cols + c) * 4;
      img.data[o] = red;
      img.data[o + 1] = green;
      img.data[o + 2] = blue;
      img.data[o + 3] = Math.round(255 * fade(at.nearest, radius));
    }
  }
  gctx.putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(grid, 0, 0, canvas.width, canvas.height);
}

function makeCanvas(w, h) {
  if (typeof OffscreenCanvas !== "undefined") return new OffscreenCanvas(w, h);
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  return c;
}
