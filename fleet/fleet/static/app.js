"use strict";

// -- helpers ------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

class AuthError extends Error {}

async function api(path, options = {}) {
  const headers = { "X-Requested-With": "fleet" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { credentials: "same-origin", ...options, headers });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.endsWith("/login")) throw new AuthError(data.error || "login required");
  if (!res.ok) {
    const err = new Error(data.error || `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.body = data;
    throw err;
  }
  return data;
}

const pad = (n) => String(n).padStart(2, "0");
const toDate = (v) => (typeof v === "number" ? new Date(v * 1000) : new Date(v));
function fmtDateTime(v) {
  if (v == null) return "never";
  const d = toDate(v);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + " " +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtAgo(v) {
  if (v == null) return "never";
  const s = Math.max(0, Math.round((Date.now() - toDate(v).getTime()) / 1000));
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
const fmt = (v, digits = 0) => (v == null ? "–" : Number(v).toFixed(digits));
const WINDOW_LABEL = { "1h": "last hour", "24h": "last 24 hours", "7d": "last 7 days" };
const STATUS_LABEL = { online: "Online", offline: "Offline", never_seen: "Never seen", revoked: "Revoked" };
const QUALITY_BARS = { excellent: 4, good: 3, fair: 2, weak: 1, poor: 1 };
const AUTH_NAMES = { 0: "Open", 1: "WEP", 2: "WPA", 3: "WPA2", 4: "WPA/WPA2", 5: "WPA2-Ent", 6: "WPA3", 7: "WPA2/WPA3" };

function quality(rssi) {
  if (rssi == null) return "none";
  if (rssi >= -50) return "excellent";
  if (rssi >= -60) return "good";
  if (rssi >= -70) return "fair";
  if (rssi >= -80) return "weak";
  return "poor";
}
function band(channel) {
  if (channel == null) return "–";
  return channel <= 14 ? "2.4 GHz" : "5 GHz";
}
function tone(value, good, warn, lowerIsBetter = true) {
  if (value == null) return null;
  if (lowerIsBetter) return value <= good ? "good" : value <= warn ? "warn" : "bad";
  return value >= good ? "good" : value >= warn ? "warn" : "bad";
}

function signalCell(rssi) {
  const q = quality(rssi);
  const bars = el("span", { class: `bars q${QUALITY_BARS[q] || 0}` }, el("i"), el("i"), el("i"), el("i"));
  return el("span", { class: `signal q-${q}` }, bars, el("span", { text: rssi == null ? "–" : `${fmt(rssi)}` }));
}
function meterCell(score) {
  if (score == null) return el("span", { class: "muted", text: "–" });
  const fill = el("div", { class: "fill" });
  fill.style.width = `${score}%`;
  return el("span", { class: `meter ${score >= 80 ? "" : score >= 55 ? "mid" : "low"}` },
    el("span", { class: "track" }, fill), el("b", { text: String(score) }));
}
function statusCell(status) {
  return el("span", { class: `status ${status}`, text: STATUS_LABEL[status] || status });
}
function kpi(label, value, unit, sub, toneName) {
  return el("div", { class: `kpi ${toneName || ""}` },
    el("div", { class: "label", text: label }),
    el("div", { class: "value" }, value, unit ? el("small", { text: unit }) : null),
    sub ? el("div", { class: "sub", text: sub }) : null);
}
function median(values) {
  const v = values.filter((x) => x != null).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

// -- state and routing ------------------------------------------------------------

const state = {
  window: "24h",
  timers: [],
  charts: [],
  units: new Map(),
  roomSort: { key: "name", dir: "asc" },
};
try { state.window = localStorage.getItem("fleet.window") || "24h"; } catch (e) { /* storage blocked */ }

function cleanup() {
  state.timers.forEach(clearInterval);
  state.timers = [];
  (state.observers || []).forEach((o) => o.disconnect());
  state.observers = [];
  state.charts.forEach((c) => c.remove());
  state.charts = [];
}

function every(ms, fn) {
  state.timers.push(setInterval(() => { if (!document.hidden) fn(); }, ms));
}

async function route() {
  cleanup();
  const [, page, arg] = (location.hash || "#/rooms").split("/");
  const section = page === "room" ? "rooms" : page === "provision" ? "units" : page;
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === section));
  try {
    if (page === "room" && arg) await renderRoom(decodeURIComponent(arg));
    else if (page === "events") await renderEvents();
    else if (page === "units") await renderUnits();
    else if (page === "settings") await renderSettings();
    else if (page === "floors") await renderFloors(arg);
    else if (page === "provision") await renderProvision(arg ? decodeURIComponent(arg) : null);
    else await renderRooms();
    $("#topbar").hidden = false;
  } catch (err) {
    if (err instanceof AuthError) return renderLogin();
    showError(err);
  }
}

function showError(err) {
  $("#view").replaceChildren(el("div", { class: "card notice", text: `Something went wrong: ${err.message}` }));
}

function renderLogin() {
  cleanup();
  $("#topbar").hidden = true;
  const view = $("#view");
  view.replaceChildren($("#login-template").content.cloneNode(true));
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = $("#login-error");
    error.hidden = true;
    try {
      await api("/api/admin/login", { method: "POST", body: JSON.stringify({ password: $("#password").value }) });
      if (!location.hash || location.hash === "#/login") location.hash = "#/rooms";
      else route();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  });
}

async function loadUnits() {
  const { units } = await api("/api/admin/units");
  state.units = new Map(units.map((u) => [u.id, u]));
  return units;
}

// -- rooms ------------------------------------------------------------------------------

// Columns where a smaller value is better (or alphabetical), so the first click sorts ascending.
const ASCENDING_FIRST = new Set(["name", "status", "rssi_std", "gw_loss_pct", "gw_median_ms", "srv_median_ms",
  "wifi_drops", "roams"]);

const ROOM_COLUMNS = [
  ["name", "Room"], ["status", "Status"], ["rssi_median", "Signal (median)", true], ["rssi_range", "Min / max", true],
  ["rssi_std", "Std dev", true], ["gw_loss_pct", "Loss", true], ["gw_median_ms", "Latency median / p95", true],
  ["srv_median_ms", "Server RTT", true], ["uptime_pct", "Uptime", true], ["wifi_drops", "Drops", true],
  ["roams", "Roams", true], ["stability", "Stability", true], ["last_seen_at", "Last seen"],
];

async function renderRooms() {
  const view = $("#view");
  const data = await api(`/api/admin/rooms?window=${state.window}`);
  const head = el("div", { class: "page-head" },
    el("div", {}, el("h1", { text: "Rooms" }),
      el("p", { text: `Signal and connection quality per room, ${WINDOW_LABEL[state.window]}. Click a room for its charts.` })));
  const kpis = el("section", { class: "kpis", id: "room-kpis" });
  const table = el("tbody");
  const thead = el("tr", {}, ROOM_COLUMNS.map(([key, label, num]) =>
    el("th", { "data-sort": key === "rssi_range" ? null : key, class: num ? "num" : null, text: label })));
  const card = el("section", { class: "card" },
    el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, thead), table)));
  view.replaceChildren(head, kpis, card);
  $$("th[data-sort]", thead).forEach((th) => th.addEventListener("click", () => {
    const key = th.dataset.sort;
    state.roomSort = state.roomSort.key === key
      ? { key, dir: state.roomSort.dir === "asc" ? "desc" : "asc" }
      : { key, dir: ASCENDING_FIRST.has(key) ? "asc" : "desc" };
    drawRooms(data, kpis, table, thead);
  }));
  drawRooms(data, kpis, table, thead);
  every(30000, async () => {
    try { Object.assign(data, await api(`/api/admin/rooms?window=${state.window}`)); drawRooms(data, kpis, table, thead); }
    catch (err) { if (err instanceof AuthError) renderLogin(); }
  });
}

function drawRooms(data, kpis, tbody, thead) {
  const rooms = data.rooms;
  const online = rooms.filter((r) => r.status === "online").length;
  const measured = rooms.filter((r) => r.samples);
  const worst = measured.slice().sort((a, b) => (a.stability ?? 101) - (b.stability ?? 101))[0];
  const med = median(measured.map((r) => r.rssi_median));
  const drops = rooms.reduce((n, r) => n + (r.wifi_drops || 0), 0);
  const losses = measured.map((r) => r.gw_loss_pct).filter((x) => x != null);
  kpis.replaceChildren(
    kpi("Rooms online", `${online}`, `of ${rooms.length}`,
        rooms.length - online ? `${rooms.length - online} offline or never seen` : "all reporting",
        online === rooms.length && rooms.length ? "good" : online ? "warn" : "bad"),
    kpi("Median signal", fmt(med), "dBm", "median of room medians", tone(med, -60, -70, false)),
    kpi("Weakest room", worst ? worst.name : "–", null,
        worst ? `stability ${worst.stability ?? "–"} · ${fmt(worst.rssi_median)} dBm` : "no data yet"),
    kpi("Packet loss", losses.length ? fmt(Math.max(...losses), 2) : "–", "%", "worst room, gateway pings",
        tone(losses.length ? Math.max(...losses) : null, 1, 5)),
    kpi("Wi-Fi drops", String(drops), null, WINDOW_LABEL[state.window], drops ? "warn" : "good"),
  );

  const { key, dir } = state.roomSort;
  const sign = dir === "asc" ? 1 : -1;
  const sorted = rooms.slice().sort((a, b) => {
    const x = a[key], y = b[key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return typeof x === "string" ? sign * x.localeCompare(y) : sign * (x - y);
  });
  $$("th[data-sort]", thead).forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === key);
    th.classList.toggle("asc", th.dataset.sort === key && dir === "asc");
  });
  tbody.replaceChildren(...sorted.map((r) => el("tr", {
    class: `link ${r.samples ? "" : "dim"}`, tabindex: "0",
    onclick: () => { location.hash = `#/room/${encodeURIComponent(r.unit_id)}`; },
    onkeydown: (e) => { if (e.key === "Enter") location.hash = `#/room/${encodeURIComponent(r.unit_id)}`; },
  },
    el("td", { class: "room-name", text: r.name }),
    el("td", {}, statusCell(r.status)),
    el("td", { class: "num" }, signalCell(r.rssi_median)),
    el("td", { class: "num", text: r.rssi_min == null ? "–" : `${r.rssi_min} / ${r.rssi_max}` }),
    el("td", { class: "num", text: fmt(r.rssi_std, 1) }),
    el("td", { class: `num tone-${tone(r.gw_loss_pct, 1, 5) || ""}`, text: r.gw_loss_pct == null ? "–" : `${fmt(r.gw_loss_pct, 2)}%` }),
    el("td", { class: "num", text: r.gw_median_ms == null ? "–" : `${fmt(r.gw_median_ms, 1)} / ${fmt(r.gw_p95_ms, 0)} ms` }),
    el("td", { class: "num", text: r.srv_median_ms == null ? "–" : `${fmt(r.srv_median_ms)} ms` }),
    el("td", { class: `num tone-${tone(r.uptime_pct, 99.5, 95, false) || ""}`, text: r.uptime_pct == null ? "–" : `${fmt(r.uptime_pct, 1)}%` }),
    el("td", { class: "num", text: String(r.wifi_drops) }),
    el("td", { class: "num", text: String(r.roams) }),
    el("td", { class: "num" }, meterCell(r.stability)),
    el("td", { class: "muted", text: fmtAgo(r.last_seen_at) }),
  )));
  if (!rooms.length) {
    tbody.append(el("tr", {}, el("td", { colspan: String(ROOM_COLUMNS.length), class: "empty",
      text: "No units yet. Register one with `fleet create-unit` or browser provisioning." })));
  }
}

// -- room detail ----------------------------------------------------------------------------

const RANGES = { "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400 };
const BUCKET_SECONDS = { "5m": 300, "15m": 900, "1h": 3600, "1d": 86400 };
const DEFAULT_BUCKET = { "6h": "5m", "24h": "15m", "7d": "1h", "30d": "1d", "90d": "1d" };

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function makeChart(container, { precision = 0, unit = "" } = {}) {
  const LC = window.LightweightCharts;
  const timeLabel = (t) => {
    const d = new Date(t * 1000);
    return `${d.toLocaleDateString(undefined, { day: "numeric", month: "short" })} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const chart = LC.createChart(container, {
    autoSize: true,
    layout: { background: { type: "solid", color: cssVar("--surface") }, textColor: cssVar("--muted"),
              fontFamily: getComputedStyle(document.body).fontFamily, fontSize: 11 },
    grid: { vertLines: { color: cssVar("--border") }, horzLines: { color: cssVar("--border") } },
    rightPriceScale: { borderColor: cssVar("--border") },
    timeScale: {
      borderColor: cssVar("--border"), timeVisible: true, secondsVisible: false,
      tickMarkFormatter: (t, type) => {
        const d = new Date(t * 1000);
        if (type <= 1) return d.toLocaleDateString(undefined, { month: "short", year: type === 0 ? "numeric" : undefined });
        if (type === 2) return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
        return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
      },
    },
    localization: { timeFormatter: timeLabel, priceFormatter: (p) => `${p.toFixed(precision)}${unit}` },
    crosshair: { mode: LC.CrosshairMode.Normal },
  });
  state.charts.push(chart);
  return chart;
}

function candleSeries(chart, higherIsBetter, precision) {
  const good = cssVar("--chart-up"), bad = cssVar("--chart-down");
  const up = higherIsBetter ? good : bad, down = higherIsBetter ? bad : good;
  return chart.addCandlestickSeries({
    upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false,
    priceFormat: { type: "price", precision, minMove: precision ? 0.01 : 1 },
  });
}

async function renderRoom(unitId) {
  const view = $("#view");
  const summary = await api(`/api/admin/units/${encodeURIComponent(unitId)}/summary?window=${state.window}`);
  const unit = summary.unit;
  let range = "24h", bucket = "5m";
  try { range = localStorage.getItem("fleet.range") || "24h"; bucket = DEFAULT_BUCKET[range]; } catch (e) { /* ignore */ }

  const kpis = el("section", { class: "kpis" });
  const rangeBox = el("div", { class: "segmented small" }, Object.keys(RANGES).map((r) =>
    el("button", { type: "button", "data-range": r, text: r })));
  const bucketBox = el("div", { class: "segmented small" }, Object.keys(BUCKET_SECONDS).map((b) =>
    el("button", { type: "button", "data-bucket": b, text: { "5m": "5 min", "15m": "15 min", "1h": "1 hour", "1d": "1 day" }[b] })));
  const rssiBox = el("div", { class: "chart" });
  const gwBox = el("div", { class: "chart short" });
  const srvBox = el("div", { class: "chart short" });
  const apBody = el("tbody");
  const scanBody = el("tbody");
  const scanHint = el("span", { class: "hint" });
  const eventList = el("ol", { class: "log compact" });
  // Green ("up" swatch) is always the good direction: stronger signal, or lower latency.
  const legend = (extra) => el("div", { class: "legend" },
    el("span", {}, el("i", { class: "up" }), extra[0]), el("span", {}, el("i", { class: "down" }), extra[1]),
    el("span", {}, el("i", { class: "median" }), "Median"), extra[2] ? el("span", {}, el("i", { class: "loss" }), extra[2]) : null);

  view.replaceChildren(
    el("div", { class: "page-head" },
      el("div", {},
        el("p", {}, el("a", { href: "#/rooms", text: "← All rooms" })),
        el("h1", { text: unit.name }),
        el("p", {}, statusCell(unit.status), ` · last seen ${fmtAgo(unit.last_seen_at)}`,
          unit.last_bssid ? ` · AP ${unit.last_bssid} (${band(unit.last_channel)}, ch ${unit.last_channel})` : "",
          unit.agent_version ? ` · agent ${unit.agent_version}` : "", unit.notes ? ` · ${unit.notes}` : "")),
      el("div", { class: "controls" }, el("span", { class: "hint", text: "Range" }), rangeBox,
        el("span", { class: "hint", text: "Candle" }), bucketBox)),
    kpis,
    el("section", { class: "card" },
      el("div", { class: "card-head" }, el("h2", { text: "Signal strength (dBm)" }), legend(["Got stronger", "Got weaker"])), rssiBox),
    el("div", { class: "grid-2" },
      el("section", { class: "card" },
        el("div", { class: "card-head" }, el("h2", { text: "Gateway latency (ms)" }), legend(["Got faster", "Got slower", "Packet loss"])), gwBox),
      el("section", { class: "card" },
        el("div", { class: "card-head" }, el("h2", { text: "Server round trip (ms)" }), legend(["Got faster", "Got slower", "Lost"])), srvBox)),
    el("div", { class: "grid-2" },
      el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", { text: "Access points used" }),
        el("span", { class: "hint", text: WINDOW_LABEL[state.window] })),
        el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {},
          el("th", { text: "BSSID" }), el("th", { text: "Band / ch" }), el("th", { class: "num", text: "Mean signal" }),
          el("th", { class: "num", text: "Samples" }), el("th", { text: "Last used" }))), apBody))),
      el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", { text: "Networks heard in the last scan" }), scanHint),
        el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {},
          el("th", { text: "Network" }), el("th", { text: "Band / ch" }), el("th", { text: "Security" }),
          el("th", { class: "num", text: "Signal" }))), scanBody)))),
    el("section", { class: "card" }, el("div", { class: "card-head" }, el("h2", { text: "Events" }),
      el("a", { href: `#/events`, text: "All events →" })), eventList),
  );

  drawRoomSummary(summary, kpis, apBody, scanBody, scanHint);
  const rssiChart = makeChart(rssiBox, { precision: 0 });
  const rssi = { candles: candleSeries(rssiChart, true, 0),
                 median: rssiChart.addLineSeries({ color: cssVar("--chart-median"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false }) };
  const latency = [gwBox, srvBox].map((box) => {
    const chart = makeChart(box, { precision: 1 });
    const loss = chart.addHistogramSeries({ priceScaleId: "loss", color: cssVar("--bad") + "70", priceLineVisible: false, lastValueVisible: false,
                                            priceFormat: { type: "custom", formatter: (v) => `${v.toFixed(1)}%` } });
    chart.priceScale("loss").applyOptions({ scaleMargins: { top: 0.75, bottom: 0 }, visible: false });
    return { chart, loss, candles: candleSeries(chart, false, 1),
             median: chart.addLineSeries({ color: cssVar("--chart-median"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false }) };
  });

  async function loadCharts(fit) {
    const to = Math.floor(Date.now() / 1000);
    const from = to - RANGES[range];
    const q = (metric) => api(`/api/admin/units/${encodeURIComponent(unitId)}/candles?metric=${metric}&bucket=${bucket}&from=${from}&to=${to}`);
    const [r, g, s] = await Promise.all([q("rssi"), q("gw_ms"), q("srv_ms")]);
    const toCandles = (rows) => rows.filter((c) => c.open != null).map((c) => ({ time: c.t, open: c.open, high: c.high, low: c.low, close: c.close }));
    const toMedian = (rows) => rows.filter((c) => c.median != null).map((c) => ({ time: c.t, value: c.median }));
    rssi.candles.setData(toCandles(r.candles));
    rssi.median.setData(toMedian(r.candles));
    [[latency[0], g], [latency[1], s]].forEach(([target, data]) => {
      target.candles.setData(toCandles(data.candles));
      target.median.setData(toMedian(data.candles));
      target.loss.setData(data.candles.filter((c) => c.loss_pct).map((c) => ({ time: c.t, value: c.loss_pct })));
    });
    [[rssiBox, r], [gwBox, g], [srvBox, s]].forEach(([box, data]) => {
      box.querySelector(".chart-empty")?.remove();
      if (!data.candles.length) box.append(el("div", { class: "chart-empty", text: "No measurements in this range" }));
    });
    if (fit) state.charts.forEach((c) => c.timeScale().fitContent());
  }

  function syncControls() {
    $$("button", rangeBox).forEach((b) => b.classList.toggle("active", b.dataset.range === range));
    $$("button", bucketBox).forEach((b) => {
      const tooMany = RANGES[range] / BUCKET_SECONDS[b.dataset.bucket] > 2000;
      const tooFew = RANGES[range] / BUCKET_SECONDS[b.dataset.bucket] < 2;
      b.disabled = tooMany || tooFew;
      b.classList.toggle("active", b.dataset.bucket === bucket);
    });
  }
  rangeBox.addEventListener("click", (e) => {
    const r = e.target.dataset.range;
    if (!r) return;
    range = r;
    bucket = DEFAULT_BUCKET[r];
    try { localStorage.setItem("fleet.range", r); } catch (err) { /* ignore */ }
    syncControls();
    loadCharts(true).catch(showError);
  });
  bucketBox.addEventListener("click", (e) => {
    const b = e.target.dataset.bucket;
    if (!b || e.target.disabled) return;
    bucket = b;
    syncControls();
    loadCharts(true).catch(showError);
  });
  syncControls();

  const loadEvents = async () => {
    const { events } = await api(`/api/admin/events?unit_id=${encodeURIComponent(unitId)}&limit=20`);
    eventList.replaceChildren(...events.map((e) => el("li", {},
      el("time", { text: fmtDateTime(e.time) }), el("span", { class: `lvl ${e.level}`, text: e.level }),
      el("span", { class: "msg", text: e.message }))));
    if (!events.length) eventList.append(el("li", { class: "empty", text: "No events." }));
  };
  await Promise.all([loadCharts(true), loadEvents()]);
  every(60000, async () => {
    try {
      const fresh = await api(`/api/admin/units/${encodeURIComponent(unitId)}/summary?window=${state.window}`);
      drawRoomSummary(fresh, kpis, apBody, scanBody, scanHint);
      await Promise.all([loadCharts(false), loadEvents()]);
    } catch (err) { if (err instanceof AuthError) renderLogin(); }
  });
}

function drawRoomSummary(summary, kpis, apBody, scanBody, scanHint) {
  const s = summary.stats;
  kpis.replaceChildren(
    kpi("Signal (median)", fmt(s.rssi_median), "dBm", s.rssi_min == null ? "no data" : `range ${s.rssi_min} to ${s.rssi_max} · std ${fmt(s.rssi_std, 1)}`,
        tone(s.rssi_median, -60, -70, false)),
    kpi("Packet loss", s.gw_loss_pct == null ? "–" : fmt(s.gw_loss_pct, 2), "%", `${s.gw_pings} gateway pings`, tone(s.gw_loss_pct, 1, 5)),
    kpi("Latency", fmt(s.gw_median_ms, 1), "ms", s.gw_p95_ms == null ? "no pings" : `p95 ${fmt(s.gw_p95_ms, 0)} ms`, tone(s.gw_median_ms, 20, 80)),
    kpi("Server round trip", fmt(s.srv_median_ms), "ms", s.srv_loss_pct == null ? "–" : `${fmt(s.srv_loss_pct, 1)}% lost`),
    kpi("Uptime", s.uptime_pct == null ? "–" : fmt(s.uptime_pct, 1), "%", `${s.wifi_drops} drops · ${s.roams} roams · ${s.boots} boots`,
        tone(s.uptime_pct, 99.5, 95, false)),
    kpi("Stability", s.stability == null ? "–" : String(s.stability), "/ 100", WINDOW_LABEL[summary.window],
        s.stability == null ? null : s.stability >= 80 ? "good" : s.stability >= 55 ? "warn" : "bad"),
  );
  apBody.replaceChildren(...summary.access_points.map((a) => el("tr", {},
    el("td", { class: "mono", text: a.bssid || "unknown" }),
    el("td", { text: `${band(a.channel)} · ${a.channel ?? "–"}` }),
    el("td", { class: "num" }, signalCell(a.rssi_mean)),
    el("td", { class: "num", text: String(a.samples) }),
    el("td", { class: "muted", text: fmtAgo(a.last_used) }))));
  if (!summary.access_points.length) apBody.append(el("tr", {}, el("td", { colspan: "5", class: "empty", text: "No connected samples in this window." })));
  const scan = summary.last_scan;
  scanHint.textContent = scan ? `${fmtAgo(scan.time)} · ${scan.ap_count} access points` : "no scan yet";
  scanBody.replaceChildren(...(scan ? scan.networks : []).map((n) => el("tr", {},
    el("td", {}, el("div", { class: "room-name", text: n.ssid || "(hidden network)" }), el("div", { class: "mono muted", text: n.bssid })),
    el("td", { text: `${band(n.channel)} · ${n.channel}` }),
    el("td", { text: AUTH_NAMES[n.auth] || `auth ${n.auth}` }),
    el("td", { class: "num" }, signalCell(n.rssi)))));
  if (!scan) scanBody.append(el("tr", {}, el("td", { colspan: "4", class: "empty", text: "Units scan every 10 minutes by default." })));
}

// -- events -----------------------------------------------------------------------------------

async function renderEvents() {
  await loadUnits();
  const view = $("#view");
  const roomSel = el("select", { "aria-label": "Room" }, el("option", { value: "", text: "All rooms" }),
    [...state.units.values()].map((u) => el("option", { value: u.id, text: u.name })));
  const levelSel = el("select", { "aria-label": "Level" }, ["", "info", "success", "warning", "error"].map((l) =>
    el("option", { value: l, text: l ? l[0].toUpperCase() + l.slice(1) : "All levels" })));
  const kindSel = el("select", { "aria-label": "Kind" }, ["", "wifi_lost", "wifi_connected", "roam", "boot", "unit_online", "unit_offline",
    "server_unreachable", "bad_signature", "update_ok", "update_failed", "hello"].map((k) =>
    el("option", { value: k, text: k ? k.replace(/_/g, " ") : "All kinds" })));
  const list = el("ol", { class: "log" });
  const more = el("button", { class: "btn", type: "button", text: "Load older" });
  view.replaceChildren(
    el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Events" }),
      el("p", { text: "Everything the units and the server reported, newest first." }))),
    el("section", { class: "card" }, el("div", { class: "card-head" }, el("div", { class: "filters" }, roomSel, levelSel, kindSel)),
      list, el("div", { class: "more" }, more)));

  let oldest = null;
  async function load(reset) {
    const params = new URLSearchParams({ limit: "100" });
    if (roomSel.value) params.set("unit_id", roomSel.value);
    if (levelSel.value) params.set("level", levelSel.value);
    if (kindSel.value) params.set("kind", kindSel.value);
    if (!reset && oldest != null) params.set("before", String(oldest));
    const { events } = await api(`/api/admin/events?${params}`);
    if (reset) list.replaceChildren();
    for (const e of events) {
      const unit = state.units.get(e.unit_id);
      list.append(el("li", {},
        el("time", { text: fmtDateTime(e.time) }),
        el("span", { class: `lvl ${e.level}`, text: e.level }),
        el("span", { class: "room" }, e.unit_id ? el("a", { href: `#/room/${encodeURIComponent(e.unit_id)}`, text: unit ? unit.name : e.unit_id }) : "server"),
        el("span", { class: "msg", text: e.message })));
    }
    if (events.length) oldest = toDate(events[events.length - 1].time).getTime() / 1000;
    more.hidden = events.length < 100;
    if (reset && !events.length) list.append(el("li", { class: "empty", text: "No events match." }));
  }
  [roomSel, levelSel, kindSel].forEach((s) => s.addEventListener("change", () => { oldest = null; load(true).catch(showError); }));
  more.addEventListener("click", () => load(false).catch(showError));
  await load(true);
}

// -- units ---------------------------------------------------------------------------------------

async function renderUnits() {
  const units = await loadUnits();
  const view = $("#view");
  const body = el("tbody", {}, units.map((u) => el("tr", { class: "link", onclick: () => { location.hash = `#/room/${encodeURIComponent(u.id)}`; } },
    el("td", { class: "room-name", text: u.name }),
    el("td", { class: "mono", text: u.id }),
    el("td", {}, statusCell(u.status)),
    el("td", { class: "muted", text: fmtAgo(u.last_seen_at) }),
    el("td", { text: u.agent_version || "–" }),
    el("td", { text: u.fw_version ? `MicroPython ${u.fw_version}` : "–" }),
    el("td", { text: u.chip || "–" }),
    el("td", { class: "mono", text: u.mac || "–" }),
    el("td", { class: "mono", text: u.last_ip || "–" }),
    el("td", { class: "mono", text: u.last_bssid ? `${u.last_bssid} · ch ${u.last_channel}` : "–" }),
    el("td", { class: "num" }, signalCell(u.last_rssi)),
    el("td", {}, el("span", { class: "row-actions" },
      el("button", { class: "btn small", type: "button", text: "Label",
                     onclick: (e) => { e.stopPropagation(); printLabel(u).catch(showError); } }),
      el("a", { class: "btn small", href: `#/provision/${encodeURIComponent(u.id)}`, text: "Reprogram",
                onclick: (e) => e.stopPropagation() }),
      el("button", { class: "btn small ghost", type: "button", text: "Remove",
                     onclick: async (e) => {
                       e.stopPropagation();
                       if (!confirm(`Remove “${u.name}” and all its measurements? This cannot be undone.`)) return;
                       try { await api(`/api/admin/units/${encodeURIComponent(u.id)}`, { method: "DELETE" }); route(); }
                       catch (err) { showError(err); }
                     } }))))));
  if (!units.length) body.append(el("tr", {}, el("td", { colspan: "12", class: "empty", text: "No units yet. Plug a board into this computer and choose Add unit." })));
  view.replaceChildren(
    el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Units" }),
      el("p", { text: `${units.length} registered · ${units.filter((u) => u.status === "online").length} online` })),
      el("a", { class: "btn primary", href: "#/provision", text: "Add unit" })),
    el("section", { class: "card" }, el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Room", "Unit ID", "Status", "Last seen", "Agent", "Firmware", "Chip", "MAC", "IP", "Access point", "Signal", ""]
        .map((h, i) => el("th", { class: i === 10 ? "num" : null, text: h })))), body))));
  every(30000, () => { if (!busyProgramming) route(); });  // route() clears this timer before rendering again
}

// -- floors (#6) ---------------------------------------------------------------------------------

async function renderFloors(floorArg) {
  const heat = await import("/static/heatmap.js");
  const [{ floors }, roomsData, unitList] = await Promise.all([
    api("/api/admin/floors"), api(`/api/admin/rooms?window=${state.window}`), loadUnits()]);
  const stats = new Map(roomsData.rooms.map((r) => [r.unit_id, r]));
  let activeId = Number(floorArg) || (floors[0] && floors[0].id);
  let floor = floors.find((f) => f.id === activeId) || floors[0];
  let editing = false;
  let metric = "rssi_median";
  let showHeat = true;
  try {
    metric = localStorage.getItem("fleet.floorMetric") || metric;
    showHeat = localStorage.getItem("fleet.floorHeat") !== "0";
  } catch (e) { /* ignore */ }
  if (!heat.METRICS[metric]) metric = "rssi_median";
  const view = $("#view");

  const fileInput = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/svg+xml", hidden: true });
  const replaceInput = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/svg+xml", hidden: true });
  const uploadBtn = el("button", { class: "btn", type: "button", text: "Upload floor plan" });
  uploadBtn.addEventListener("click", () => fileInput.click());
  const errorBox = el("p", { class: "form-error", role: "alert", hidden: true });
  const showError = (err) => { errorBox.textContent = err.message || String(err); errorBox.hidden = false; };

  async function uploadTo(url, file, method) {
    errorBox.hidden = true;
    const res = await fetch(url, { method, credentials: "same-origin", body: file,
      headers: { "X-Requested-With": "fleet", "Content-Type": file.type || "application/octet-stream" } });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) throw new AuthError("login required");
    if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
    return data.floor;
  }
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    try {
      const name = file.name.replace(/\.[^.]+$/, "").slice(0, 80) || "Floor";
      const created = await uploadTo(`/api/admin/floors?name=${encodeURIComponent(name)}`, file, "POST");
      location.hash = `#/floors/${created.id}`;
    } catch (err) { if (err instanceof AuthError) renderLogin(); else showError(err); }
  });
  replaceInput.addEventListener("change", async () => {
    const file = replaceInput.files[0];
    replaceInput.value = "";
    if (!file || !floor) return;
    try { await uploadTo(`/api/admin/floors/${floor.id}/image`, file, "PUT"); route(); }
    catch (err) { if (err instanceof AuthError) renderLogin(); else showError(err); }
  });

  const metricSel = el("select", { "aria-label": "Metric" }, Object.entries(heat.METRICS).map(([key, m]) =>
    el("option", { value: key, text: m.label })));
  metricSel.value = metric;
  const heatBox = el("input", { type: "checkbox" });
  heatBox.checked = showHeat;
  const editBox = el("input", { type: "checkbox" });

  const head = el("div", { class: "page-head" },
    el("div", {}, el("h1", { text: "Floors" }),
      el("p", { text: `Units on the floor plans, coloured by ${WINDOW_LABEL[state.window]}'s measurements. Click a unit for its room.` })),
    el("div", { class: "controls" }, metricSel,
      el("label", { class: "check" }, heatBox, "Heat layer"),
      el("label", { class: "check" }, editBox, "Place units"),
      uploadBtn, fileInput, replaceInput));

  if (!floors.length) {
    view.replaceChildren(head, errorBox, el("section", { class: "card empty-floor" },
      el("h2", { text: "No floor plans yet" }),
      el("p", { class: "muted", text: "Upload a floor plan (PNG, JPEG, WebP or SVG, up to 5 MB) and drag the units onto their rooms." }),
      el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", text: "Upload floor plan",
        onclick: () => fileInput.click() }))));
    return;
  }

  const tabs = el("div", { class: "segmented floor-tabs", role: "tablist" }, floors.map((f) =>
    el("button", { type: "button", role: "tab", class: f.id === floor.id ? "active" : null, text: f.name,
                   onclick: () => { location.hash = `#/floors/${f.id}`; } })));
  const nameInput = el("input", { value: floor.name, maxlength: "80", "aria-label": "Floor plan name" });
  const editBar = el("div", { class: "actions floor-edit", hidden: true },
    nameInput,
    el("button", { class: "btn", type: "button", text: "Rename", onclick: async () => {
      try { await api(`/api/admin/floors/${floor.id}`, { method: "PATCH", body: JSON.stringify({ name: nameInput.value }) }); route(); }
      catch (err) { showError(err); } } }),
    el("button", { class: "btn", type: "button", text: "Replace image", onclick: () => replaceInput.click() }),
    el("button", { class: "btn ghost", type: "button", text: "Delete floor plan", onclick: async () => {
      if (!confirm(`Delete the floor plan "${floor.name}"? Units placed on it become unplaced.`)) return;
      try { await api(`/api/admin/floors/${floor.id}`, { method: "DELETE" }); location.hash = "#/floors"; route(); }
      catch (err) { showError(err); } } }));

  const img = el("img", { class: "floor-image", alt: `Floor plan: ${floor.name}`,
                          src: `/api/admin/floors/${floor.id}/image?v=${encodeURIComponent(floor.updated_at)}` });
  const canvas = el("canvas", { class: "floor-heat" });
  const layer = el("div", { class: "floor-markers" });
  const stage = el("div", { class: "floor-stage" }, img, canvas, layer);
  const legend = el("div", { class: "floor-legend" });
  const tray = el("div", { class: "floor-tray", hidden: true });

  view.replaceChildren(head, errorBox,
    el("section", { class: "card" },
      el("div", { class: "card-head" }, tabs, legend),
      editBar, tray, stage));

  const placed = () => new Map(floor.placements.map((p) => [p.unit_id, p]));
  const placedAnywhere = new Set(floors.flatMap((f) => f.placements.map((p) => p.unit_id)));

  function valueText(unitId) {
    const m = heat.METRICS[metric];
    const v = stats.get(unitId)?.[metric];
    return v == null ? "no data" : `${Number(v).toFixed(m.digits)} ${m.unit}`.replace(" /100", "/100");
  }

  function drawLegend() {
    const m = heat.METRICS[metric];
    const bar = el("span", { class: "legend-bar" });
    bar.style.background = `linear-gradient(90deg, ${heat.cssColor(0)}, ${heat.cssColor(0.5)}, ${heat.cssColor(1)})`;
    legend.replaceChildren(el("span", { text: `${m.bad} ${m.unit}`.replace(" /100", "") }), bar,
      el("span", { text: `${m.good} ${m.unit}`.replace(" /100", "") }));
  }

  function drawMarkers() {
    layer.replaceChildren();
    for (const [unitId, p] of placed()) {
      const unit = state.units.get(unitId);
      const t = heat.score(metric, stats.get(unitId)?.[metric]);
      const marker = el("button", { type: "button", class: `marker ${t == null ? "nodata" : ""} ${editing ? "editing" : ""}`,
        title: `${unit ? unit.name : unitId}: ${valueText(unitId)}` },
        el("span", { class: "marker-name", text: unit ? unit.name : unitId }),
        el("span", { class: "marker-value", text: valueText(unitId) }));
      marker.style.left = `${p.x * 100}%`;
      marker.style.top = `${p.y * 100}%`;
      if (t != null) marker.style.setProperty("--marker", heat.cssColor(t));
      if (editing) {
        const remove = el("span", { class: "marker-remove", role: "button", "aria-label": "Remove from plan", text: "×" });
        remove.addEventListener("pointerdown", (e) => e.stopPropagation());
        remove.addEventListener("click", async (e) => {
          e.stopPropagation();
          try { await api(`/api/admin/placements/${encodeURIComponent(unitId)}`, { method: "DELETE" }); await refresh(); }
          catch (err) { showError(err); }
        });
        marker.append(remove);
        marker.addEventListener("pointerdown", (e) => startDrag(e, unitId));
      } else {
        marker.addEventListener("click", () => { location.hash = `#/room/${encodeURIComponent(unitId)}`; });
      }
      layer.append(marker);
    }
  }

  function drawTray() {
    const unplaced = unitList.filter((u) => !placedAnywhere.has(u.id) && u.status !== "revoked");
    tray.replaceChildren(el("span", { class: "hint", text: unplaced.length
      ? "Drag a unit onto its room. Drag placed units to move them; × removes one from the plan."
      : "Every unit is placed. Drag placed units to move them; × removes one from the plan." }),
      ...unplaced.map((u) => {
        const chip = el("span", { class: "chip draggable", text: u.name, role: "button", tabindex: "0" });
        chip.addEventListener("pointerdown", (e) => startDrag(e, u.id));
        return chip;
      }));
  }

  function drawHeat() {
    const rect = img.getBoundingClientRect();
    if (!rect.width) return;
    canvas.style.width = `${rect.width}px`;
    canvas.style.height = `${rect.height}px`;
    if (!showHeat) { canvas.width = canvas.height = 0; return; }
    const points = [...placed()].map(([unitId, p]) => ({ x: p.x, y: p.y, t: heat.score(metric, stats.get(unitId)?.[metric]) }));
    heat.paintHeat(canvas, points, rect.width, rect.height, Math.min(2, window.devicePixelRatio || 1));
  }

  function startDrag(event, unitId) {
    if (event.button !== 0) return;
    event.preventDefault();
    const unit = state.units.get(unitId);
    const ghost = el("div", { class: "marker ghost" }, el("span", { class: "marker-name", text: unit ? unit.name : unitId }));
    document.body.append(ghost);
    const move = (e) => { ghost.style.left = `${e.clientX}px`; ghost.style.top = `${e.clientY}px`; };
    move(event);
    const up = async (e) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      ghost.remove();
      const rect = img.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width, y = (e.clientY - rect.top) / rect.height;
      if (x < 0 || x > 1 || y < 0 || y > 1) return;  // dropped outside the plan: nothing changes
      try {
        await api(`/api/admin/placements/${encodeURIComponent(unitId)}`, { method: "PUT",
          body: JSON.stringify({ floor_id: floor.id, x: Math.round(x * 10000) / 10000, y: Math.round(y * 10000) / 10000 }) });
        await refresh();
      } catch (err) { showError(err); }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  async function refresh() {
    const fresh = await api("/api/admin/floors");
    floors.splice(0, floors.length, ...fresh.floors);
    floor = floors.find((f) => f.id === floor.id) || floors[0];
    placedAnywhere.clear();
    floors.forEach((f) => f.placements.forEach((p) => placedAnywhere.add(p.unit_id)));
    redraw();
  }

  function redraw() {
    stage.classList.toggle("editing", editing);
    editBar.hidden = tray.hidden = !editing;
    drawLegend();
    drawMarkers();
    if (editing) drawTray();
    drawHeat();
  }

  metricSel.addEventListener("change", () => { metric = metricSel.value; try { localStorage.setItem("fleet.floorMetric", metric); } catch (e) { /* ignore */ } redraw(); });
  heatBox.addEventListener("change", () => { showHeat = heatBox.checked; try { localStorage.setItem("fleet.floorHeat", showHeat ? "1" : "0"); } catch (e) { /* ignore */ } drawHeat(); });
  editBox.addEventListener("change", () => { editing = editBox.checked; redraw(); });
  img.addEventListener("load", drawHeat);
  const resize = new ResizeObserver(() => drawHeat());
  resize.observe(stage);
  state.observers = [resize];
  redraw();
  every(60000, async () => {
    if (editing) return;
    try {
      const fresh = await api(`/api/admin/rooms?window=${state.window}`);
      stats.clear();
      fresh.rooms.forEach((r) => stats.set(r.unit_id, r));
      redraw();
    } catch (err) { if (err instanceof AuthError) renderLogin(); }
  });
}

// -- labels -------------------------------------------------------------------------------------

async function labelContext() {
  const [{ template }, prov] = await Promise.all([api("/api/admin/label-template"), api("/api/admin/provisioning")]);
  return { template, network: prov.wifi.ssid };
}

function labelData(unit, network) {
  return { name: unit.name, unit_id: unit.id, url: `${location.origin}/#/room/${encodeURIComponent(unit.id)}`,
           network, date: FleetLabels.today(), mac: unit.mac };
}

async function printLabel(unit) {
  const { template, network } = await labelContext();
  FleetLabels.print(template, [labelData(unit, network)]);
}

// -- settings -------------------------------------------------------------------------------------

async function renderSettings() {
  const [prov, { template }] = await Promise.all([api("/api/admin/provisioning"), api("/api/admin/label-template")]);
  const view = $("#view");

  // Wi-Fi for new units
  const ssid = el("input", { id: "wifi-ssid", maxlength: "32", value: prov.wifi.ssid || "", autocomplete: "off" });
  const password = el("input", { id: "wifi-password", type: "password", maxlength: "63", autocomplete: "new-password",
    placeholder: prov.wifi.password_set ? "Stored (leave empty to keep)" : "" });
  const open = el("input", { type: "checkbox" });
  open.checked = Boolean(prov.wifi.ssid && prov.wifi.open_network);
  const wifiStatus = el("span", { class: "saved" });
  const wifiError = el("p", { class: "form-error", role: "alert", hidden: true });
  const syncOpen = () => { password.disabled = open.checked; };
  open.addEventListener("change", syncOpen);
  syncOpen();
  const wifiForm = el("form", {},
    el("div", { class: "form-grid" },
      el("div", { class: "field" }, el("label", { for: "wifi-ssid", text: "Network name (SSID)" }), ssid),
      el("div", { class: "field" }, el("label", { for: "wifi-password", text: "Password" }), password)),
    el("label", { class: "check" }, open, "Open network (no password)"),
    wifiError,
    el("div", { class: "actions" }, el("button", { class: "btn primary", type: "submit", text: "Save Wi-Fi" }), wifiStatus));
  wifiForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    wifiError.hidden = true;
    wifiStatus.textContent = "";
    const body = { ssid: ssid.value.trim() };
    if (open.checked) body.password = "";
    else if (password.value) body.password = password.value;
    try {
      const res = await api("/api/admin/provisioning/wifi", { method: "PUT", body: JSON.stringify(body) });
      password.value = "";
      password.placeholder = res.wifi.password_set ? "Stored (leave empty to keep)" : "";
      wifiStatus.textContent = "Saved";
    } catch (err) {
      wifiError.textContent = err.message;
      wifiError.hidden = false;
    }
  });

  // Server and firmware
  const fw = prov.firmware;
  const serverCard = el("section", { class: "card" },
    el("div", { class: "card-head" }, el("h2", { text: "Server" })),
    el("p", {}, "Units send their measurements to ", el("strong", { class: "mono", text: prov.unit_url }),
      " (plain HTTP with signed requests)."),
    fw.available
      ? el("p", { class: "ok-note", text: `MicroPython ${fw.version} for ${fw.chip} is on the server and verified.` })
      : el("p", { class: "notice", text: `MicroPython ${fw.version} is not on the server yet. Run \`fleet fetch-firmware\` there; ` +
        "until then, only boards that already have it can be programmed." }));

  // Label template
  const t = JSON.parse(JSON.stringify(template));
  const preset = el("select", { "aria-label": "Label size" }, el("option", { value: "", text: "Custom size" }),
    FleetLabels.PRESETS.map((p) => el("option", { value: p.id, text: p.label })));
  const width = el("input", { type: "number", min: "15", max: "150", step: "0.1", value: String(t.width_mm), id: "label-w" });
  const height = el("input", { type: "number", min: "10", max: "150", step: "0.1", value: String(t.height_mm), id: "label-h" });
  const scale = el("input", { type: "range", min: "0.5", max: "2", step: "0.05", value: String(t.font_scale), id: "label-scale" });
  const fieldNames = { room: "Room name", unit_id: "Unit ID", qr: "QR code to the room page", network: "Wi-Fi network",
                       date: "Date programmed", mac: "Board MAC address" };
  const fieldBoxes = Object.entries(fieldNames).map(([key, label]) => {
    const box = el("input", { type: "checkbox", "data-field": key });
    box.checked = t.fields[key];
    return el("label", { class: "check" }, box, label);
  });
  const preview = el("div", { class: "label-preview" });
  const labelStatus = el("span", { class: "saved" });
  const labelError = el("p", { class: "form-error", role: "alert", hidden: true });
  const sample = { name: "B-204", unit_id: "u3k7x2m9qpa", url: `${location.origin}/#/room/u3k7x2m9qpa`,
                   network: prov.wifi.ssid || "SchoolNet", date: FleetLabels.today(), mac: "02:00:00:c5:12:54" };
  const matchPreset = () => {
    const found = FleetLabels.PRESETS.find((p) => p.width_mm === t.width_mm && p.height_mm === t.height_mm);
    preset.value = found ? found.id : "";
  };
  const redraw = () => {
    labelStatus.textContent = "";
    preview.replaceChildren(FleetLabels.render(t, sample));
  };
  preset.addEventListener("change", () => {
    const found = FleetLabels.PRESETS.find((p) => p.id === preset.value);
    if (found) { t.width_mm = found.width_mm; t.height_mm = found.height_mm; width.value = String(t.width_mm); height.value = String(t.height_mm); }
    redraw();
  });
  width.addEventListener("input", () => { t.width_mm = Number(width.value) || t.width_mm; matchPreset(); redraw(); });
  height.addEventListener("input", () => { t.height_mm = Number(height.value) || t.height_mm; matchPreset(); redraw(); });
  scale.addEventListener("input", () => { t.font_scale = Number(scale.value); redraw(); });
  fieldBoxes.forEach((l) => l.querySelector("input").addEventListener("change", (e) => { t.fields[e.target.dataset.field] = e.target.checked; redraw(); }));
  const saveLabel = el("button", { class: "btn primary", type: "button", text: "Save label template" });
  saveLabel.addEventListener("click", async () => {
    labelError.hidden = true;
    try {
      const res = await api("/api/admin/label-template", { method: "PUT", body: JSON.stringify(t) });
      Object.assign(t, res.template);
      labelStatus.textContent = "Saved";
    } catch (err) {
      labelError.textContent = err.message;
      labelError.hidden = false;
    }
  });
  const testPrint = el("button", { class: "btn", type: "button", text: "Test print" });
  testPrint.addEventListener("click", () => FleetLabels.print(t, [sample]));
  matchPreset();
  redraw();

  view.replaceChildren(
    el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Settings" }),
      el("p", { text: "Wi-Fi and labels for programming new units." }))),
    el("section", { class: "card" },
      el("div", { class: "card-head" }, el("h2", { text: "Wi-Fi for new units" }),
        el("span", { class: "hint", text: "The password is stored encrypted and only written onto units." })),
      wifiForm),
    serverCard,
    el("section", { class: "card" },
      el("div", { class: "card-head" }, el("h2", { text: "Label" }), el("span", { class: "hint", text: "Shown at actual size" })),
      el("div", { class: "form-grid" },
        el("div", { class: "field" }, el("span", { class: "label-text-muted", text: "Label stock" }), preset),
        el("div", { class: "field" }, el("label", { for: "label-w", text: "Width (mm)" }), width),
        el("div", { class: "field" }, el("label", { for: "label-h", text: "Height (mm)" }), height),
        el("div", { class: "field" }, el("label", { for: "label-scale", text: "Text size" }), scale)),
      el("div", { class: "form-grid" }, fieldBoxes),
      el("div", { class: "actions" }, preview),
      labelError,
      el("div", { class: "actions" }, saveLabel, testPrint, labelStatus)),
  );
}

// -- add / reprogram a unit ------------------------------------------------------------------------

const STEP_TITLES = { connect: "Connect to the board", firmware: "MicroPython", register: "Register on the server",
                      files: "Copy agent and configuration", restart: "Restart", online: "Unit online" };

async function fetchBytes(path) {
  const res = await fetch(path, { credentials: "same-origin", headers: { "X-Requested-With": "fleet" } });
  if (res.status === 401) throw new AuthError("login required");
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `${res.status} ${res.statusText}`);
  return new Uint8Array(await res.arrayBuffer());
}

const provisionApi = {
  firmware: () => fetchBytes("/api/admin/provisioning/firmware.bin"),
  unitFile: (name) => fetchBytes(`/api/admin/provisioning/files/${name}`),
  provision: (body) => api("/api/admin/provision", { method: "POST", body: JSON.stringify(body) }),
  unit: async (id) => (await api(`/api/admin/units/${encodeURIComponent(id)}`)).unit,
};

let busyProgramming = false;
window.addEventListener("beforeunload", (e) => { if (busyProgramming) { e.preventDefault(); e.returnValue = ""; } });

async function renderProvision(unitId) {
  const prov = await api("/api/admin/provisioning");
  const reprogram = unitId ? (await api(`/api/admin/units/${encodeURIComponent(unitId)}`)).unit : null;
  const view = $("#view");
  let batch = false;
  try { batch = localStorage.getItem("fleet.batch") === "1"; } catch (e) { /* ignore */ }

  const problems = [];
  if (!("serial" in navigator)) problems.push("This browser cannot talk to USB devices. Use Chrome or Edge on a laptop or desktop computer.");
  if (!prov.wifi.ssid) problems.push(el("span", {}, "Set the Wi-Fi network first in ", el("a", { href: "#/settings", text: "Settings" }), "."));

  const name = el("input", { id: "unit-name", maxlength: "100", required: true, value: reprogram ? reprogram.name : (state.nextName || ""),
                             placeholder: "e.g. B-204", autocomplete: "off" });
  const notes = el("input", { id: "unit-notes", maxlength: "2000", value: reprogram?.notes || "", placeholder: "Optional, e.g. by the window" });
  const forceFlash = el("input", { type: "checkbox" });
  const batchBox = el("input", { type: "checkbox" });
  batchBox.checked = batch;
  batchBox.addEventListener("change", () => { try { localStorage.setItem("fleet.batch", batchBox.checked ? "1" : "0"); } catch (e) { /* ignore */ } });
  const start = el("button", { class: "btn primary", type: "submit", text: reprogram ? "Connect board and reprogram" : "Connect board and program",
                               disabled: problems.length > 0 });
  const formError = el("p", { class: "form-error", role: "alert", hidden: true });
  const form = el("form", {},
    el("div", { class: "form-grid" },
      el("div", { class: "field" }, el("label", { for: "unit-name", text: "Room name" }), name),
      el("div", { class: "field" }, el("label", { for: "unit-notes", text: "Notes" }), notes)),
    el("div", { class: "actions" },
      el("label", { class: "check" }, forceFlash, "Reinstall MicroPython even if it is already there"),
      reprogram ? null : el("label", { class: "check" }, batchBox, "Several boards in a row (go straight to the next one)")),
    formError,
    el("div", { class: "actions" }, start,
      el("span", { class: "hint", text: "Plug the board into this computer with a USB cable first. The browser asks which device to use." })));

  const steps = el("ol", { class: "steps" }, Object.entries(STEP_TITLES).map(([key, title]) =>
    el("li", { "data-step": key }, el("span", { class: "mark", text: "○" }), el("span", { class: "title", text: title }),
      el("span", { class: "detail" }))));
  const bar = el("div");
  const progress = el("div", { class: "progress", hidden: true }, bar);
  const stepsCard = el("section", { class: "card", hidden: true },
    el("div", { class: "card-head" }, el("h2", { text: "Programming" })), steps, progress);
  const result = el("section", { class: "card", hidden: true });

  view.replaceChildren(
    el("div", { class: "page-head" }, el("div", {},
      el("p", {}, el("a", { href: "#/units", text: "← Units" })),
      el("h1", { text: reprogram ? `Reprogram ${reprogram.name}` : "Add unit" }),
      el("p", { text: reprogram
        ? "Writes a new key and the current Wi-Fi settings onto the board. The unit's old key stops working."
        : `Installs MicroPython ${prov.firmware.version} if needed, registers the unit and copies the agent. About 30 to 60 seconds.` }))),
    ...problems.map((pr) => el("div", { class: "card notice" }, pr)),
    el("section", { class: "card" }, form),
    stepsCard, result);

  const setStep = (key, status, detail) => {
    const li = steps.querySelector(`[data-step="${key}"]`);
    li.className = status;
    li.querySelector(".mark").textContent = { active: "●", done: "✓", failed: "✕", "": "○" }[status] || "○";
    if (detail != null) li.querySelector(".detail").textContent = detail;
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    formError.hidden = true;
    if (!name.value.trim()) { formError.textContent = "Enter the room name."; formError.hidden = false; return; }
    let port;
    try {
      port = await navigator.serial.requestPort({ filters: [{ usbVendorId: 0x303a }] });
    } catch (err) {
      if (err.name !== "NotFoundError") { formError.textContent = err.message; formError.hidden = false; }
      return;  // the user closed the device chooser
    }
    const [flow, esptool] = await Promise.all([import("/static/provision-flow.js"), import("/static/vendor/esptool-js-0.7.0.bundle.js")]);
    Object.keys(STEP_TITLES).forEach((k) => setStep(k, "", ""));
    stepsCard.hidden = false;
    result.hidden = true;
    start.disabled = true;
    busyProgramming = true;
    let current = null;
    try {
      const outcome = await flow.programBoard(port, {
        name: name.value.trim(), notes: notes.value.trim() || null, unitId: reprogram?.id, forceFlash: forceFlash.checked,
        firmware: prov.firmware, api: provisionApi, esptool,
        // The board re-enumerates on USB after each full reset; Chrome usually hands it back by itself (#11).
        serial: navigator.serial,
        // The board already belongs to other units (#14): let the user decide instead of registering twice.
        resolveExisting: (existing, request) => new Promise((resolve) => {
          const names = existing.map((u) => `“${u.name}”`).join(", ");
          const newName = request.name;
          const choices = el("div", { class: "actions" });
          const note = el("div", { class: "notice" },
            el("p", { text: `This board is already registered as ${names}.` }), choices);
          const done = (value) => { note.remove(); resolve(value); };
          if (!request.unit_id) {
            existing.forEach((u) => choices.append(el("button", { class: "btn primary", type: "button",
              text: u.name === newName ? `Reprogram “${u.name}” (keeps its history)` : `Reprogram “${u.name}” as “${newName}” (keeps its history)`,
              onclick: () => done({ reprogram: u.id }) })));
          }
          choices.append(
            el("button", { class: "btn", type: "button",
              text: request.unit_id ? `Retire ${names}` : `New unit “${newName}”, retire ${names}`,
              onclick: () => done({ retire: true }) }),
            el("button", { class: "btn ghost", type: "button", text: "Cancel", onclick: () => done(null) }));
          stepsCard.append(note);
          note.scrollIntoView({ behavior: "smooth", block: "center" });
        }),
        requestReconnect: () => new Promise((resolve, reject) => {
          const pick = el("button", { class: "btn primary", type: "button", text: "Select the board again" });
          const note = el("div", { class: "notice actions" },
            "The board restarted and the browser needs you to pick it once more.", pick);
          stepsCard.append(note);
          pick.addEventListener("click", async () => {
            try {
              const again = await navigator.serial.requestPort({ filters: [{ usbVendorId: 0x303a }] });
              note.remove();
              resolve(again);
            } catch (err) {
              if (err.name !== "NotFoundError") { note.remove(); reject(err); }
            }
          });
        }),
        onStep: (step, detail) => {
          if (current) setStep(current, "done");
          current = step;
          setStep(step, "active", detail);
          progress.hidden = !(step === "firmware" || step === "files");
          bar.style.width = "0%";
        },
        onProgress: (fraction) => { bar.style.width = `${Math.round(fraction * 100)}%`; },
      });
      setStep("online", "done", `Online after ${outcome.seconds} s · AP ${outcome.unit.last_bssid || "?"} channel ${outcome.unit.last_channel ?? "?"}`);
      progress.hidden = true;
      showResult(outcome, flow);
    } catch (err) {
      if (err instanceof AuthError) { busyProgramming = false; return renderLogin(); }
      if (current) setStep(current, "failed", err.message);
      formError.textContent = `Programming failed: ${err.message.replace(/\.+$/, "")}. The board is safe to retry.`;
      formError.hidden = false;
    } finally {
      busyProgramming = false;
      start.disabled = problems.length > 0;
    }
  });

  async function showResult(outcome, flow) {
    const { template, network } = await labelContext();
    const data = labelData(outcome.unit, network);
    const next = flow.nextRoomName(outcome.unit.name);
    const printBtn = el("button", { class: "btn primary", type: "button", text: "Print label" });
    printBtn.addEventListener("click", () => FleetLabels.print(template, [data]));
    const nextBtn = el("button", { class: "btn", type: "button", text: `Next board: ${next}` });
    nextBtn.addEventListener("click", () => {
      state.nextName = next;
      if (location.hash === "#/provision") route(); else location.hash = "#/provision";
    });
    result.replaceChildren(
      el("div", { class: "card-head" }, el("h2", { text: `${outcome.unit.name} is online` }),
        el("span", { class: "hint", text: `${outcome.flashed ? "MicroPython installed · " : ""}board ${outcome.info.mac} · ${outcome.seconds} s` })),
      el("div", { class: "label-preview" }, FleetLabels.render(template, data)),
      el("div", { class: "actions" }, printBtn,
        !reprogram && batchBox.checked ? nextBtn : null,
        el("a", { class: "btn", href: `#/room/${encodeURIComponent(outcome.unit.id)}`, text: "Open room" }),
        !reprogram && !batchBox.checked ? el("a", { class: "btn ghost", href: "#/provision", text: "Add another unit" }) : null));
    result.hidden = false;
    state.nextName = next;
    if (!reprogram && batchBox.checked) nextBtn.focus();
    result.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// -- wiring ----------------------------------------------------------------------------------------

function init() {
  $$("#window-picker button").forEach((b) => {
    b.classList.toggle("active", b.dataset.window === state.window);
    b.addEventListener("click", () => {
      state.window = b.dataset.window;
      try { localStorage.setItem("fleet.window", state.window); } catch (e) { /* ignore */ }
      $$("#window-picker button").forEach((x) => x.classList.toggle("active", x === b));
      route();
    });
  });
  $("#logout").addEventListener("click", async () => {
    try { await api("/api/admin/logout", { method: "POST" }); } finally { renderLogin(); }
  });
  window.addEventListener("hashchange", route);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", route);
  route();
}

document.addEventListener("DOMContentLoaded", init);
