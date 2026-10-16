"use strict";

// -- helpers -----------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

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

async function api(path, options = {}) {
  const headers = { "X-Requested-With": "wifimon" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...options, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

const pad = (n) => String(n).padStart(2, "0");
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtDateTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + " " +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtDuration(seconds) {
  if (seconds == null || !isFinite(seconds)) return "–";
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  if (h) return `${h} h ${pad(m)} min`;
  if (m) return `${m} min ${pad(s)} s`;
  return `${s} s`;
}
const fmt = (v, digits = 0) => (v == null ? "–" : Number(v).toFixed(digits));
const MODE_NAMES = { scan: "Scan", connect: "Connection", both: "Scan + connection" };
const MODE_HINTS = {
  scan: "Scans every nearby network on 2.4 and 5 GHz. One full scan takes about 10 seconds.",
  connect: "Joins one network and pings it: latency, packet loss, signal strength and drop-outs.",
  both: "Stays connected and pings, and also scans all networks. Each scan pauses pings for about 10 seconds.",
};
const QUALITY_BARS = { excellent: 4, good: 3, fair: 2, weak: 1, poor: 1, none: 0 };
const PALETTE = ["#2563eb", "#16a34a", "#ea580c", "#9333ea", "#0891b2", "#db2777",
                 "#ca8a04", "#4f46e5", "#059669", "#dc2626", "#7c3aed", "#0d9488"];
const CH24 = Array.from({ length: 13 }, (_, i) => i + 1);
const CH5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
             149, 153, 157, 161, 165];
const MAX_LIVE_POINTS = 3000;

function quality(rssi) {
  if (rssi == null) return "none";
  if (rssi >= -50) return "excellent";
  if (rssi >= -60) return "good";
  if (rssi >= -70) return "fair";
  if (rssi >= -80) return "weak";
  return "poor";
}

function debounce(fn, ms) {
  let timer = null;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

function throttle(fn, ms) {
  let last = 0, timer = null;
  return (...args) => {
    const wait = last + ms - Date.now();
    if (wait <= 0) { last = Date.now(); fn(...args); }
    else if (!timer) timer = setTimeout(() => { timer = null; last = Date.now(); fn(...args); }, wait);
  };
}

// -- state --------------------------------------------------------------------

const state = {
  status: null,
  sessions: [],
  activeId: null,
  viewId: null,
  detail: null,
  selected: new Map(), // bssid -> colour
  userPicked: false,
  sort: { key: "mean_rssi", dir: "desc" },
  filter: "",
  band: "all",
  visibleOnly: false,
  level: "all",
  events: [],
  charts: {},
};

// -- status and header --------------------------------------------------------

function renderStatus() {
  const s = state.status;
  const pill = $("#device-pill");
  const text = pill.querySelector(".pill-text");
  pill.classList.remove("ok", "bad", "warn");
  if (!s) {
    pill.classList.add("bad");
    text.textContent = "Dashboard server offline";
  } else if (s.device.kind === "simulated") {
    pill.classList.add("warn");
    text.textContent = "Simulated device (no hardware)";
  } else if (s.device.connected) {
    const info = s.device.info || {};
    const chip = (info.machine || "ESP32").replace(/^.* with /, "");
    const port = (s.device.state.port || "").replace("/dev/", "");
    pill.classList.add("ok");
    text.textContent = [chip, port, info.fw ? `MicroPython ${info.fw}` : null].filter(Boolean).join(" · ");
    pill.title = s.device.state.detail || "";
  } else {
    pill.classList.add("bad");
    text.textContent = `Board offline: ${s.device.state.detail || "not connected"}`;
  }
  renderSessionPill();
  renderControls();
}

function renderSessionPill() {
  const s = state.status;
  const pill = $("#session-pill");
  if (!s || !s.session) { pill.hidden = true; return; }
  pill.hidden = false;
  const elapsed = Date.now() / 1000 - s.session.started_at;
  pill.querySelector(".pill-text").textContent =
    `Live #${s.session.id} · ${MODE_NAMES[s.session.mode]} · ${fmtDuration(elapsed)}`;
}

// -- control form -------------------------------------------------------------

function currentMode() {
  return $('input[name="mode"]:checked').value;
}

function applyModeDefaults() {
  const mode = currentMode();
  const defaults = state.status?.defaults;
  $("#mode-hint").textContent = MODE_HINTS[mode];
  $$(".conn-only").forEach((n) => (n.hidden = mode === "scan"));
  $$(".scan-only").forEach((n) => (n.hidden = mode === "connect"));
  if (defaults) {
    $("#scan_interval").value = defaults.scan_interval[mode];
    if (!$("#link_interval").value) $("#link_interval").value = defaults.link_interval;
    if (!$("#ssid").value && defaults.ssid) $("#ssid").value = defaults.ssid;
    if (!$("#target").value && defaults.ping_target) $("#target").placeholder = defaults.ping_target;
  }
  updatePasswordHint();
}

function updatePasswordHint() {
  const d = state.status?.defaults;
  const fromEnv = d && d.has_password && $("#ssid").value.trim() === d.ssid;
  $("#password-hint").textContent = fromEnv
    ? "Leave empty to use the password from .env. Sent to the board only."
    : "Sent to the board only. Never stored.";
}

function renderControls() {
  const live = Boolean(state.status?.session);
  $("#stop-btn").hidden = !live;
  $("#start-btn").textContent = live ? "Start new session" : "Start session";
}

function renderSsidOptions() {
  const list = $("#ssid-options");
  list.replaceChildren();
  const seen = new Set();
  for (const n of state.detail?.networks || []) {
    if (!n.ssid || seen.has(n.ssid)) continue;
    seen.add(n.ssid);
    list.append(el("option", { value: n.ssid }));
  }
}

async function startSession(event) {
  event.preventDefault();
  const error = $("#form-error");
  error.hidden = true;
  const mode = currentMode();
  const body = { mode, scan_interval: $("#scan_interval").value, note: $("#note").value };
  if (mode !== "scan") {
    Object.assign(body, {
      ssid: $("#ssid").value.trim(),
      password: $("#password").value,
      target: $("#target").value.trim(),
      link_interval: $("#link_interval").value,
    });
  }
  const button = $("#start-btn");
  button.disabled = true;
  try {
    const { session } = await api("/api/sessions", { method: "POST", body: JSON.stringify(body) });
    $("#password").value = "";
    $("#note").value = "";
    state.userPicked = false;
    state.selected.clear();
    await loadSessions();
    await viewSession(session.id);
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function stopSession() {
  try {
    await api("/api/sessions/stop", { method: "POST" });
  } catch (err) {
    alert(`Could not stop the session: ${err.message}`);
  }
}

// -- sessions list -------------------------------------------------------------

async function loadSessions() {
  const data = await api("/api/sessions");
  state.sessions = data.sessions;
  state.activeId = data.active;
  renderSessions();
}

function sessionTitle(s) {
  return `#${s.id} · ${MODE_NAMES[s.mode]}` + (s.ssid ? ` · ${s.ssid}` : "");
}

function renderSessions() {
  const list = $("#session-list");
  list.replaceChildren();
  $("#session-count").textContent = state.sessions.length ? String(state.sessions.length) : "";
  if (!state.sessions.length) {
    list.append(el("li", { class: "empty-list", text: "No sessions recorded yet." }));
  }
  for (const s of state.sessions) {
    const live = s.id === state.activeId;
    const end = live ? Date.now() / 1000 : s.ended_at ?? s.started_at;
    const counts = [s.scan_count ? `${s.scan_count} scans` : null,
                    s.link_count ? `${s.link_count} pings` : null].filter(Boolean).join(", ");
    list.append(el("li", {},
      el("button", { type: "button", class: s.id === state.viewId ? "selected" : null,
                     onclick: () => viewSession(s.id) },
        el("span", { class: "s-title", text: sessionTitle(s) }),
        live ? el("span", { class: "badge live", text: "LIVE" }) : el("span"),
        el("span", { class: "s-meta",
                     text: [fmtDateTime(s.started_at), fmtDuration(end - s.started_at), counts, s.note]
                       .filter(Boolean).join(" · ") }))));
  }
}

// -- session view ---------------------------------------------------------------

async function viewSession(id) {
  if (state.viewId !== id) {
    state.viewId = id;
    state.detail = null;
    state.events = [];
    state.selected.clear();
    state.userPicked = false;
    destroyCharts();
  }
  renderSessions();
  await Promise.all([refreshDetail(), loadEvents()]);
}

async function refreshDetail() {
  const id = state.viewId;
  if (id == null) return;
  let detail;
  try {
    detail = await api(`/api/sessions/${id}`);
  } catch (err) {
    console.warn(err);
    return;
  }
  if (id !== state.viewId) return;
  state.detail = detail;
  renderView();
  const tasks = [];
  if (detail.session.mode !== "connect") tasks.push(loadRssi());
  if (detail.link && !state.charts.link) tasks.push(loadLink());
  await Promise.all(tasks);
}

const refreshSoon = debounce(refreshDetail, 400);
const refreshThrottled = throttle(refreshDetail, 4000);

function renderView() {
  const d = state.detail;
  $("#empty-state").hidden = true;
  $("#session-view").hidden = false;
  const s = d.session;
  const hasScan = s.mode !== "connect";
  const hasLink = s.mode !== "scan";

  $("#view-title").textContent = sessionTitle(s);
  renderViewMeta();
  $("#delete-btn").hidden = d.active;

  const links = $("#export-links");
  links.replaceChildren();
  const kinds = [hasScan && ["networks", "Network statistics"], hasScan && ["observations", "Every scan sample"],
                 hasLink && ["link", "Connection samples"], ["events", "Event log"]].filter(Boolean);
  for (const [kind, label] of kinds) {
    links.append(el("a", { href: `/api/sessions/${s.id}/export/${kind}.csv`, download: "", text: label }));
  }

  $("#scan-kpis").hidden = !hasScan;
  $("#scan-charts").hidden = !hasScan;
  $("#networks-card").hidden = !hasScan;
  $("#link-kpis").hidden = !hasLink;
  $("#link-card").hidden = !hasLink;

  if (hasScan) {
    renderScanKpis();
    pickDefaultSelection();
    renderNetworks();
    renderChannelCharts();
    renderSsidOptions();
  }
  if (hasLink && d.link) renderLinkKpis();
}

function renderViewMeta() {
  const d = state.detail;
  if (!d) return;
  const s = d.session;
  const end = d.active ? Date.now() / 1000 : s.ended_at ?? s.started_at;
  const parts = [
    d.active ? "Recording" : "Finished",
    `started ${fmtDateTime(s.started_at)}`,
    `duration ${fmtDuration(end - s.started_at)}`,
    s.mode !== "connect" ? `scan every ${s.scan_interval} s` : null,
    s.mode !== "scan" ? `ping ${s.ping_target || "gateway"} every ${s.link_interval} s` : null,
    s.note,
  ];
  $("#view-meta").textContent = parts.filter(Boolean).join(" · ");
}

function kpi(label, value, unit, sub, tone) {
  return el("div", { class: `kpi ${tone || ""}` },
    el("div", { class: "label", text: label }),
    el("div", { class: "value" }, value, unit ? el("small", { text: unit }) : null),
    sub ? el("div", { class: "sub", text: sub }) : null);
}

function renderScanKpis() {
  const d = state.detail;
  const sum = d.scan_summary;
  const visible = d.networks.filter((n) => n.visible_now);
  const strongest = visible[0] || d.networks[0];
  const n24 = visible.filter((n) => n.band === "2.4 GHz").length;
  const n5 = visible.filter((n) => n.band === "5 GHz").length;
  const ssids = new Set(visible.map((n) => n.ssid || n.bssid)).size;
  const stable = d.networks.filter((n) => n.stability >= 80).length;
  $("#scan-kpis").replaceChildren(
    kpi("Visible now", String(sum.visible_now), "APs", `${ssids} network names · ${sum.ever_seen} APs seen in total`),
    kpi("By band", `${n24} / ${n5}`, null, "2.4 GHz / 5 GHz"),
    kpi("Strongest", strongest ? fmt(strongest.current_rssi ?? strongest.mean_rssi) : "–", "dBm",
        strongest ? `${strongest.ssid || "(hidden)"} · ch ${strongest.channel}` : "no data"),
    kpi("Stable networks", String(stable), `of ${d.networks.length}`, "stability score 80 or higher"),
    kpi("Scans", String(sum.scans), null,
        sum.mean_duration_ms ? `${(sum.mean_duration_ms / 1000).toFixed(1)} s per scan` : "waiting for first scan"),
  );
}

function renderLinkKpis() {
  const l = state.detail.link;
  const s = state.detail.session;
  const lossTone = l.loss_pct == null ? "" : l.loss_pct < 1 ? "good" : l.loss_pct < 5 ? "warn" : "bad";
  const upTone = l.uptime_pct == null ? "" : l.uptime_pct >= 99.5 ? "good" : l.uptime_pct >= 95 ? "warn" : "bad";
  const latTone = l.latency_median == null ? "" : l.latency_median < 20 ? "good" : l.latency_median < 80 ? "warn" : "bad";
  const q = quality(l.rssi_current ?? l.rssi_mean);
  const sigTone = { excellent: "good", good: "good", fair: "warn", weak: "bad", poor: "bad" }[q] || "";
  $("#link-kpis").replaceChildren(
    kpi("Status", l.samples ? (l.connected_now ? "Connected" : "Down") : "Waiting", null,
        s.ssid, l.samples ? (l.connected_now ? "good" : "bad") : ""),
    kpi("Signal", fmt(l.rssi_current ?? l.rssi_mean), "dBm",
        l.rssi_mean != null ? `${q} · mean ${fmt(l.rssi_mean)} · std ${fmt(l.rssi_std, 1)}` : "no data", sigTone),
    kpi("Latency", fmt(l.latency_median, 1), "ms",
        l.latency_p95 != null ? `p95 ${fmt(l.latency_p95, 1)} · jitter ${fmt(l.latency_jitter, 1)} ms` : "no replies yet", latTone),
    kpi("Packet loss", fmt(l.loss_pct, 1), "%",
        l.pings ? `${l.pings - l.pings_ok} of ${l.pings} pings lost` : "no pings yet", lossTone),
    kpi("Uptime", fmt(l.uptime_pct, 1), "%",
        l.longest_outage_s ? `longest outage ${fmtDuration(l.longest_outage_s)}` : "no outages", upTone),
    kpi("Disconnects", String(l.disconnects), null, `over ${fmtDuration(l.duration_s)}`,
        l.disconnects ? "warn" : "good"),
  );
}

// -- networks table ----------------------------------------------------------------

function pickDefaultSelection() {
  if (state.userPicked) return;
  const visible = state.detail.networks.filter((n) => n.visible_now);
  const wanted = (visible.length ? visible : state.detail.networks).slice(0, 6).map((n) => n.bssid);
  const same = wanted.length === state.selected.size && wanted.every((b) => state.selected.has(b));
  if (same) return;
  state.selected.clear();
  wanted.forEach((b) => selectNetwork(b));
}

function selectNetwork(bssid) {
  const used = new Set(state.selected.values());
  const colour = PALETTE.find((c) => !used.has(c)) || PALETTE[state.selected.size % PALETTE.length];
  state.selected.set(bssid, colour);
}

function toggleNetwork(bssid, on) {
  state.userPicked = true;
  if (on) {
    if (state.selected.size >= PALETTE.length) {
      alert(`You can plot up to ${PALETTE.length} networks at once.`);
      renderNetworks();
      return;
    }
    selectNetwork(bssid);
  } else {
    state.selected.delete(bssid);
  }
  renderNetworks();
  loadRssi();
}

function sortedNetworks() {
  const { key, dir } = state.sort;
  const q = state.filter.toLowerCase();
  const rows = state.detail.networks.filter((n) =>
    (state.band === "all" || n.band === state.band) &&
    (!state.visibleOnly || n.visible_now) &&
    (!q || n.ssid.toLowerCase().includes(q) || n.bssid.includes(q)));
  const sign = dir === "asc" ? 1 : -1;
  return rows.sort((a, b) => {
    const x = a[key], y = b[key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    if (typeof x === "string") return sign * x.localeCompare(y);
    return sign * (x - y);
  });
}

function signalCell(rssi) {
  const q = quality(rssi);
  const bars = el("span", { class: `bars q${QUALITY_BARS[q]}` }, el("i"), el("i"), el("i"), el("i"));
  return el("span", { class: `signal q-${q}` }, bars, el("span", { text: rssi == null ? "gone" : `${rssi}` }));
}

function stabilityCell(score) {
  const tone = score >= 80 ? "" : score >= 55 ? "mid" : "low";
  const fill = el("div", { class: "fill" });
  fill.style.width = `${score}%`;
  return el("span", { class: `meter ${tone}` }, el("span", { class: "track" }, fill), el("b", { text: String(score) }));
}

function renderNetworks() {
  const rows = sortedNetworks();
  const body = $("#network-rows");
  body.replaceChildren();
  $("#network-count").textContent = `${rows.length} of ${state.detail.networks.length}`;
  for (const n of rows) {
    const colour = state.selected.get(n.bssid);
    const box = el("input", { type: "checkbox", class: "plot-box", "aria-label": `Plot ${n.ssid || n.bssid}`,
                              onchange: (e) => toggleNetwork(n.bssid, e.target.checked) });
    box.checked = Boolean(colour);
    const swatch = colour ? el("span", { class: "swatch" }) : null;
    if (swatch) swatch.style.background = colour;
    body.append(el("tr", { class: n.visible_now ? null : "gone" },
      el("td", {}, box),
      el("td", {},
        el("div", { class: `ssid ${n.ssid ? "" : "hidden-ssid"}`, title: n.ssid || "" }, n.ssid || "(hidden network)", swatch),
        el("div", { class: "bssid", text: n.bssid })),
      el("td", { text: `${n.band} · ${n.channel}` }),
      el("td", { text: n.security }),
      el("td", { class: "num" }, signalCell(n.current_rssi)),
      el("td", { class: "num", text: fmt(n.mean_rssi, 1) }),
      el("td", { class: "num", text: `${n.min_rssi} / ${n.max_rssi}` }),
      el("td", { class: "num", text: fmt(n.std_rssi, 1) }),
      el("td", { class: "num", text: `${fmt(n.seen_pct)}%` }),
      el("td", { class: "num", text: String(n.dropouts) }),
      el("td", { class: "num" }, stabilityCell(n.stability))));
  }
  $$(".networks th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === state.sort.key);
    th.classList.toggle("asc", th.dataset.sort === state.sort.key && state.sort.dir === "asc");
  });
}

// -- charts -----------------------------------------------------------------------------

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function applyChartTheme() {
  if (!window.Chart) return;
  Chart.defaults.color = cssVar("--muted");
  Chart.defaults.borderColor = cssVar("--border");
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 11.5;
  Chart.defaults.animation = false;
  Chart.defaults.maintainAspectRatio = false;
  Object.values(state.charts).forEach((c) => c.update());
}

function destroyCharts() {
  Object.values(state.charts).forEach((c) => c.destroy());
  state.charts = {};
}

const timeAxis = () => ({
  type: "linear",
  ticks: { callback: (v) => fmtTime(v / 1000), maxRotation: 0, autoSkipPadding: 24 },
  grid: { display: false },
});
const timeTitle = (items) => (items.length ? fmtTime(items[0].parsed.x / 1000) : "");

async function loadRssi() {
  const id = state.viewId;
  const bssids = Array.from(state.selected.keys());
  const data = bssids.length
    ? await api(`/api/sessions/${id}/rssi?bssids=${encodeURIComponent(bssids.join(","))}`)
    : { ts: [], series: {} };
  if (id !== state.viewId) return;
  const byBssid = new Map(state.detail.networks.map((n) => [n.bssid, n]));
  const many = data.ts.length > 120;
  const datasets = bssids.map((b) => {
    const n = byBssid.get(b);
    const colour = state.selected.get(b);
    return {
      label: n ? `${n.ssid || "(hidden)"} · ch ${n.channel}` : b,
      data: data.ts.map((t, i) => ({ x: t * 1000, y: data.series[b]?.[i] ?? null })),
      borderColor: colour,
      backgroundColor: colour,
      borderWidth: 2,
      pointRadius: many ? 0 : 2.5,
      pointHoverRadius: 4,
      tension: 0.25,
      spanGaps: false,
    };
  });
  let chart = state.charts.rssi;
  if (!chart) {
    chart = state.charts.rssi = new Chart($("#rssi-chart"), {
      type: "line",
      data: { datasets },
      options: {
        interaction: { mode: "nearest", axis: "x", intersect: false },
        scales: {
          x: timeAxis(),
          y: { suggestedMin: -95, suggestedMax: -30, title: { display: true, text: "RSSI (dBm)" } },
        },
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
          tooltip: { callbacks: { title: timeTitle, label: (c) => ` ${c.dataset.label}: ${c.parsed.y} dBm` } },
        },
      },
    });
  } else {
    chart.data.datasets = datasets;
    chart.update();
  }
}

function channelData(band, channels) {
  const visible = state.detail.networks.filter((n) => n.visible_now && n.band === band);
  const labels = Array.from(new Set([...channels, ...visible.map((n) => n.channel)])).sort((a, b) => a - b);
  const names = new Map(labels.map((c) => [c, []]));
  visible.forEach((n) => names.get(n.channel).push(`${n.ssid || "(hidden)"} ${n.current_rssi} dBm`));
  return { labels, counts: labels.map((c) => names.get(c).length), names };
}

function renderChannelChart(key, canvas, band, channels, colour) {
  const { labels, counts, names } = channelData(band, channels);
  let chart = state.charts[key];
  if (!chart) {
    chart = state.charts[key] = new Chart(canvas, {
      type: "bar",
      data: { labels, datasets: [{ data: counts, backgroundColor: colour, borderRadius: 4, maxBarThickness: 22 }] },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => `Channel ${items[0].label}`,
              label: (c) => ` ${c.parsed.y} access point${c.parsed.y === 1 ? "" : "s"}`,
              afterBody: (items) => chart.$names.get(Number(items[0].label))?.slice(0, 10) || [],
            },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { autoSkip: false, maxRotation: 0, font: { size: 10 } } },
          y: { beginAtZero: true, ticks: { precision: 0 } },
        },
      },
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = counts;
    chart.update();
  }
  chart.$names = names;
}

function renderChannelCharts() {
  renderChannelChart("ch24", $("#ch24-chart"), "2.4 GHz", CH24, PALETTE[0]);
  renderChannelChart("ch5", $("#ch5-chart"), "5 GHz", CH5, PALETTE[3]);
}

function linkPoint(sample) {
  const x = sample.ts * 1000;
  return {
    latency: { x, y: sample.connected && sample.ok ? sample.latency_ms : null },
    rssi: { x, y: sample.connected ? sample.rssi : null },
    lost: sample.connected && !sample.ok ? { x, y: 1 } : null,
    down: !sample.connected ? { x, y: 1 } : null,
  };
}

async function loadLink() {
  const id = state.viewId;
  const data = await api(`/api/sessions/${id}/link`);
  if (id !== state.viewId) return;
  const accent = cssVar("--accent"), good = cssVar("--good"), bad = cssVar("--bad"), faint = cssVar("--faint");
  const sets = { latency: [], rssi: [], lost: [], down: [] };
  for (const s of data.samples) {
    const p = linkPoint(s);
    sets.latency.push(p.latency);
    sets.rssi.push(p.rssi);
    if (p.lost) sets.lost.push(p.lost);
    if (p.down) sets.down.push(p.down);
  }
  state.charts.link?.destroy();
  state.charts.link = new Chart($("#link-chart"), {
    data: {
      datasets: [
        { type: "line", label: "Latency (ms)", data: sets.latency, yAxisID: "y", borderColor: accent,
          backgroundColor: accent, borderWidth: 1.5, pointRadius: 0, spanGaps: false },
        { type: "line", label: "Signal (dBm)", data: sets.rssi, yAxisID: "rssi", borderColor: good,
          backgroundColor: good, borderWidth: 1.5, borderDash: [4, 3], pointRadius: 0, spanGaps: false },
        { type: "scatter", label: "Lost ping", data: sets.lost, yAxisID: "flag", pointStyle: "crossRot",
          pointRadius: 5, borderColor: bad, backgroundColor: bad, borderWidth: 2 },
        { type: "scatter", label: "Not connected", data: sets.down, yAxisID: "flag", pointStyle: "rect",
          pointRadius: 3, borderColor: faint, backgroundColor: faint },
      ],
    },
    options: {
      interaction: { mode: "nearest", axis: "x", intersect: false },
      scales: {
        x: timeAxis(),
        y: { beginAtZero: true, title: { display: true, text: "Latency (ms)" } },
        rssi: { position: "right", suggestedMin: -90, suggestedMax: -30, grid: { display: false },
                title: { display: true, text: "Signal (dBm)" } },
        flag: { display: false, min: 0, max: 1.08 },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: timeTitle,
            label: (c) => c.dataset.yAxisID === "flag" ? ` ${c.dataset.label}` : ` ${c.dataset.label}: ${c.parsed.y}`,
          },
        },
      },
    },
  });
  renderLinkLegend([[accent, "Latency"], [good, "Signal"], [bad, "Lost ping"], [faint, "Not connected"]]);
}

function renderLinkLegend(items) {
  const legend = $("#link-legend");
  legend.replaceChildren(...items.map(([colour, label]) => {
    const swatch = el("i");
    swatch.style.background = colour;
    return el("span", {}, swatch, label);
  }));
}

const flushLinkChart = throttle(() => state.charts.link?.update("none"), 1000);

function appendLinkSample(sample) {
  const chart = state.charts.link;
  if (!chart) return;
  const p = linkPoint(sample);
  const [lat, rssi, lost, down] = chart.data.datasets;
  lat.data.push(p.latency);
  rssi.data.push(p.rssi);
  if (p.lost) lost.data.push(p.lost);
  if (p.down) down.data.push(p.down);
  for (const ds of chart.data.datasets) {
    if (ds.data.length > MAX_LIVE_POINTS) ds.data.splice(0, ds.data.length - MAX_LIVE_POINTS);
  }
  flushLinkChart();
}

// -- event log ------------------------------------------------------------------------

async function loadEvents() {
  const id = state.viewId;
  const data = await api(`/api/sessions/${id}/events?limit=300`);
  if (id !== state.viewId) return;
  state.events = data.events;
  renderEvents();
}

function eventVisible(e) {
  if (state.level === "all") return true;
  if (state.level === "warning") return e.level === "warning" || e.level === "error";
  return e.level === state.level;
}

function eventRow(e, fresh) {
  return el("li", { class: fresh ? "fresh" : null },
    el("time", { text: fmtTime(e.ts), title: new Date(e.ts * 1000).toLocaleString() }),
    el("span", { class: `lvl ${e.level}`, text: e.level }),
    el("span", { class: "msg", text: e.message }));
}

function renderEvents() {
  const log = $("#event-log");
  const rows = state.events.filter(eventVisible);
  log.replaceChildren(...rows.map((e) => eventRow(e, false)));
  if (!rows.length) log.append(el("li", { class: "empty-list", text: "No events." }));
}

function addEvent(e) {
  const forView = e.session_id === state.viewId || (e.session_id == null && state.viewId === state.activeId);
  if (!forView) return;
  state.events.unshift(e);
  if (state.events.length > 1000) state.events.length = 1000;
  if (!eventVisible(e)) return;
  const log = $("#event-log");
  log.querySelector(".empty-list")?.remove();
  log.prepend(eventRow(e, true));
}

// -- live stream ------------------------------------------------------------------------

function connectStream() {
  const source = new EventSource("/api/stream");
  source.addEventListener("open", () => { loadSessions().catch(console.warn); });
  source.addEventListener("error", () => {
    if (source.readyState !== EventSource.OPEN) { state.status = null; renderStatus(); }
  });
  source.addEventListener("status", (msg) => {
    const first = !state.status;
    const prevSession = state.status?.session?.id ?? null;
    state.status = JSON.parse(msg.data);
    renderStatus();
    if (first) applyModeDefaults();
    if ((state.status.session?.id ?? null) !== prevSession) loadSessions().catch(console.warn);
  });
  source.addEventListener("session", async (msg) => {
    const session = JSON.parse(msg.data);
    state.status = await api("/api/status").catch(() => state.status);
    renderStatus();
    await loadSessions();
    if (session.id === state.viewId) refreshSoon();
  });
  source.addEventListener("scan", (msg) => {
    const data = JSON.parse(msg.data);
    if (data.session_id === state.viewId) refreshSoon();
    if (state.status) state.status.last_scan = data;
  });
  source.addEventListener("link", (msg) => {
    const data = JSON.parse(msg.data);
    if (data.session_id !== state.viewId) return;
    appendLinkSample(data);
    refreshThrottled();
  });
  source.addEventListener("event", (msg) => addEvent(JSON.parse(msg.data)));
}

// -- wiring ----------------------------------------------------------------------------

function wire() {
  $("#session-form").addEventListener("submit", startSession);
  $("#stop-btn").addEventListener("click", stopSession);
  $$('input[name="mode"]').forEach((r) => r.addEventListener("change", applyModeDefaults));
  $("#ssid").addEventListener("input", updatePasswordHint);

  $("#network-filter").addEventListener("input", (e) => { state.filter = e.target.value.trim(); renderNetworks(); });
  $("#visible-only").addEventListener("change", (e) => { state.visibleOnly = e.target.checked; renderNetworks(); });
  $$(".chip[data-band]").forEach((chip) => chip.addEventListener("click", () => {
    state.band = chip.dataset.band;
    $$(".chip[data-band]").forEach((c) => c.classList.toggle("active", c === chip));
    renderNetworks();
  }));
  $$(".chip[data-level]").forEach((chip) => chip.addEventListener("click", () => {
    state.level = chip.dataset.level;
    $$(".chip[data-level]").forEach((c) => c.classList.toggle("active", c === chip));
    renderEvents();
  }));
  $$(".networks th[data-sort]").forEach((th) => th.addEventListener("click", () => {
    const key = th.dataset.sort;
    const textual = key === "ssid" || key === "security";
    state.sort = state.sort.key === key
      ? { key, dir: state.sort.dir === "asc" ? "desc" : "asc" }
      : { key, dir: textual || key === "channel" ? "asc" : "desc" };
    renderNetworks();
  }));
  $("#delete-btn").addEventListener("click", async () => {
    const id = state.viewId;
    if (id == null || !confirm(`Delete session #${id} and all its measurements?`)) return;
    try {
      await api(`/api/sessions/${id}`, { method: "DELETE" });
    } catch (err) {
      alert(err.message);
      return;
    }
    state.viewId = null;
    destroyCharts();
    await loadSessions();
    showInitialView();
  });
  document.addEventListener("click", (e) => {
    $$("details.menu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyChartTheme);
  setInterval(() => { renderSessionPill(); if (state.detail?.active) renderViewMeta(); }, 1000);
}

function showInitialView() {
  const target = state.activeId ?? state.sessions[0]?.id;
  if (target != null) {
    viewSession(target);
  } else {
    $("#session-view").hidden = true;
    $("#empty-state").hidden = false;
  }
}

async function init() {
  applyChartTheme();
  wire();
  applyModeDefaults();
  try {
    state.status = await api("/api/status");
    renderStatus();
    applyModeDefaults();
    await loadSessions();
  } catch (err) {
    console.warn(err);
  }
  showInitialView();
  connectStream();
}

document.addEventListener("DOMContentLoaded", init);
