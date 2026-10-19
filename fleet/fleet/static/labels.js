"use strict";

// Unit labels (#4): adjustable size and fields, previewed on screen and printed at exact size.
// Uses the vendored qrcode-generator (global `qrcode`). Everything is built with DOM APIs; the page
// size for printing goes into a constructable stylesheet, so the strict CSP stays intact.

(function () {
  const SVG_NS = "http://www.w3.org/2000/svg";

  const PRESETS = [
    { id: "brother-62x29", label: "Brother DK-11209 · 62 × 29 mm", width_mm: 62, height_mm: 29 },
    { id: "dymo-89x36", label: "Dymo 99012 · 89 × 36 mm", width_mm: 89, height_mm: 36 },
    { id: "dymo-54x25", label: "Dymo 11352 · 54 × 25 mm", width_mm: 54, height_mm: 25 },
    { id: "zebra-50x25", label: "Thermal roll · 50 × 25 mm", width_mm: 50, height_mm: 25 },
    { id: "avery-38x21", label: "Avery L7651 sheet label · 38 × 21 mm", width_mm: 38.1, height_mm: 21.2 },
  ];

  function qrSvg(text, sizeMm) {
    const qr = qrcode(0, "M");
    qr.addData(text);
    qr.make();
    const n = qr.getModuleCount();
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `-1 -1 ${n + 2} ${n + 2}`);
    svg.setAttribute("shape-rendering", "crispEdges");
    svg.setAttribute("class", "label-qr");
    svg.style.width = svg.style.height = `${sizeMm}mm`;
    let d = "";
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) if (qr.isDark(r, c)) d += `M${c} ${r}h1v1h-1z`;
    }
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
    return svg;
  }

  function line(cls, text, sizeMm) {
    const div = document.createElement("div");
    div.className = cls;
    div.textContent = text;
    div.style.fontSize = `${sizeMm}mm`;
    return div;
  }

  /**
   * One label element. data: { name, unit_id, url, network, date, mac }.
   * template: { width_mm, height_mm, font_scale, fields: { room, unit_id, qr, network, date, mac } }.
   */
  function render(template, data) {
    const w = template.width_mm, h = template.height_mm, f = template.fields, scale = template.font_scale;
    const pad = Math.max(1.2, Math.min(w, h) * 0.06);
    const label = document.createElement("div");
    label.className = "unit-label";
    label.style.width = `${w}mm`;
    label.style.height = `${h}mm`;
    label.style.padding = `${pad}mm`;
    label.style.gap = `${pad}mm`;
    const inner = h - 2 * pad;
    if (f.qr && data.url) label.append(qrSvg(data.url, Math.min(inner, w * 0.45)));
    const text = document.createElement("div");
    text.className = "label-text";
    const small = Math.max(1.6, inner * 0.13) * scale;
    if (f.room) {
      const room = line("label-room", data.name, Math.max(2.4, inner * 0.3) * scale);
      text.append(room);
    }
    if (f.unit_id && data.unit_id) text.append(line("label-mono", data.unit_id, small));
    if (f.network && data.network) text.append(line("label-small", `Wi-Fi: ${data.network}`, small));
    if (f.date && data.date) text.append(line("label-small", data.date, small));
    if (f.mac && data.mac) text.append(line("label-mono", data.mac, small));
    label.append(text);
    return label;
  }

  let pageSheet = null;

  /** Print labels at exact size: one label per page, page size = label size. */
  function print(template, items) {
    const root = document.createElement("div");
    root.id = "print-root";
    items.forEach((data) => root.append(render(template, data)));
    document.body.append(root);
    if (!pageSheet) {
      pageSheet = new CSSStyleSheet();
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, pageSheet];
    }
    pageSheet.replaceSync(`@page { size: ${template.width_mm}mm ${template.height_mm}mm; margin: 0; }`);
    const cleanup = () => { root.remove(); window.removeEventListener("afterprint", cleanup); };
    window.addEventListener("afterprint", cleanup);
    window.print();
    setTimeout(cleanup, 60000);  // in case afterprint never fires
  }

  function today() {
    return new Date().toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }

  window.FleetLabels = { PRESETS, render, print, today };
})();
