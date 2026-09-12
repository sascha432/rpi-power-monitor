/* Power-monitor dashboard UI.
 *
 * Single WebSocket to the dashboard server (/ws). Messages:
 *   hello   -> UI catalog (channels, metrics, units, theme, energy unit, cadence)
 *   history -> per-channel point arrays to seed the chart
 *   sample  -> latest reading per channel (+ Pi connection state) every update_ms
 *
 * The user's UI settings (metric, time window, energy unit, theme, hidden
 * channels) are applied here and persisted in a "pwm_settings" cookie.
 */
"use strict";

const COOKIE_NAME = "pwm_settings";
const ARR = { voltage_v: "v", current_a: "a", power_w: "w" }; // buffer per metric
const KIDX = { voltage_v: 1, current_a: 2, power_w: 3, session_wh: 4, total_wh: 5 };
const SEED_TOL_S = 1.0; // history align tolerance
const WINDOWS = [60, 300, 900, 1800, 3600];
const WINDOW_LABEL = (s) =>
  s < 60 ? s + "s" : s % 3600 === 0 ? s / 3600 + "h" : s / 60 + "m";
const PALETTE = {
  dark: ["#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#4dd0e1", "#fff176", "#a1887f"],
  light: ["#0277bd", "#ef6c00", "#2e7d32", "#c62828", "#6a1b9a", "#00838f", "#f9a825", "#6d4c41"],
};

// Default colours per metric (user-selectable in Settings). Metric colours are
// used for single-metric series (metric tiles, the focused channel graph) and
// the energy history bars; the dashboard's multi-channel graph keeps per-channel
// colours so the lines stay distinguishable.
const METRIC_DEFAULT_COLORS = {
  voltage_v: "#22c55e", // green
  current_a: "#eab308", // yellow
  power_w: "#ec4899", // pink
  energy: "#3b82f6", // blue
};
const CURRENT_UNITS = ["A", "mA"];
// Browser-local UI defaults. The server's catalog only ships server-derived
// facts, so these live here; a catalog value still wins when present, and each
// visitor's choices are persisted in the pwm_settings cookie.
const DEFAULT_METRIC = "power_w";
const DEFAULT_THEME = "dark";
const DEFAULT_ENERGY_UNIT = "kWh";
const THEMES = ["dark", "light"];
const ENERGY_UNITS = ["Wh", "kWh"];
const ENERGY_DAYS_MIN = 7;
const ENERGY_DAYS_MAX = 90;

// The four dashboard graphs. V/A/P are live time-series of the selected
// channels; "energy" plots each selected channel's daily totals (Wh).
const DASH_METRICS = [
  { key: "voltage_v", label: "Voltage" },
  { key: "current_a", label: "Current" },
  { key: "power_w", label: "Power" },
  { key: "energy", label: "Energy · daily totals" },
];

// ---- central state ---------------------------------------------------------
const S = {
  catalog: null,
  ws: null,
  piConnected: false,
  lastSample: 0,
  timeline: [], // shared x axis (epoch s), one entry per sample tick
  channels: [], // ordered channel list from the catalog (derived from server.yaml)
  meta: {},     // id -> { name,label,kind,metrics,color, v:[],a:[],w:[], last:{...} }
  el: {},       // id -> card DOM refs
  settings: {
    metric: "power_w", // graph metric (Power default)
    theme: "dark", // GUI color mode: dark | light
    energyUnit: "kWh", // energy unit: Wh | kWh
    currentUnit: "A", // current unit: A | mA
    dashWindowSec: 60, // dashboard graph window (1m default)
    chanWindowSec: 300, // channel graph window (5m default)
    energyDays: 7, // energy-total bars: ENERGY_DAYS_MIN..ENERGY_DAYS_MAX
    colors: { ...METRIC_DEFAULT_COLORS },
    hidden: [],
    channelMetrics: {}, // channel id -> last chosen graph metric (V/A/P)
  },
  sig: "",      // chart signature (rebuild when it changes)
  dashU: {},    // dashboard graphs: metricKey -> uPlot (4 panels)
  u: null,      // (legacy single dashboard chart - unused)
  table: null,  // channel focus (single selected channel): { cells, charts, activeId }
  view: { name: "dashboard", id: null }, // active sidebar view
  _piWas: false,
};

const $ = (id) => document.getElementById(id);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ---- cookie helpers ----------------------------------------------------------
function readCookie() {
  try {
    const raw = decodeURIComponent(document.cookie)
      .split(";")
      .map((s) => s.trim())
      .find((s) => s.startsWith(COOKIE_NAME + "="));
    if (!raw) return null;
    return JSON.parse(raw.slice(COOKIE_NAME.length + 1));
  } catch (_e) {
    return null;
  }
}
function writeCookie() {
  const val = encodeURIComponent(JSON.stringify(S.settings));
  document.cookie = COOKIE_NAME + "=" + val + "; path=/; max-age=31536000; SameSite=Lax";
}
function clearCookie() {
  document.cookie = COOKIE_NAME + "=; path=/; max-age=0";
}

// ---- number formatting -------------------------------------------------------
function fmtWParts(w) {
  const a = Math.abs(w);
  if (a < 1) return { num: w.toFixed(3), unit: "W" };
  if (a < 100) return { num: w.toFixed(2), unit: "W" };
  if (a < 10000) return { num: w.toFixed(1), unit: "W" };
  return { num: (w / 1000).toFixed(2), unit: "kW" };
}
function fmtW(w) {
  const parts = fmtWParts(w);
  return parts.num + " " + parts.unit;
}
function fmtAParts(a) {
  const v = S.settings.currentUnit === "mA" ? a * 1000 : a;
  if (S.settings.currentUnit === "mA") {
    const x = Math.abs(v);
    return { num: (x < 100 ? v.toFixed(1) : String(Math.round(v))), unit: "mA" };
  }
  const x = Math.abs(v);
  return { num: x < 0.1 ? v.toFixed(3) : v.toFixed(2), unit: "A" };
}
function fmtEnergy(wh) {
  if (S.settings.energyUnit === "kWh") {
    const v = wh / 1000;
    return (Math.abs(v) < 10 ? v.toFixed(3) : v.toFixed(1)) + " kWh";
  }
  return (Math.abs(wh) < 100 ? wh.toFixed(2) : String(Math.round(wh))) + " Wh";
}
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Colours for the non-data chart lines (axis spline + tick marks + grid). The
// CSS variables normally supply these, but if one resolves to "" uPlot strokes
// the line with the canvas default - i.e. BLACK, which looks wrong in the light
// theme - so fall back to an explicit pair per theme.
function chartLineColors() {
  const light = document.documentElement.dataset.theme === "light";
  return {
    axis: cssVar("--axis") || (light ? "#9aa2b4" : "#5a6172"),
    grid: cssVar("--grid") || (light ? "#dde2ec" : "#2b3140"),
  };
}

// ---- settings ------------------------------------------------------------------
function applyCatalogDefaults(cat) {
  S.catalog = cat;
  document.title = cat.title;
  const appTitle = $("appTitle");
  if (appTitle) appTitle.textContent = cat.title;
  const energyUnits = (cat.energy_units || ENERGY_UNITS).slice();
  const def = {
    metric: cat.default_metric || DEFAULT_METRIC,
    theme: cat.theme || DEFAULT_THEME,
    energyUnit: energyUnits.includes(DEFAULT_ENERGY_UNIT)
      ? DEFAULT_ENERGY_UNIT
      : (energyUnits[0] || DEFAULT_ENERGY_UNIT),
    currentUnit: "A",
    dashWindowSec: 60, // dashboard graph window: 1m default
    chanWindowSec: 300, // channel graph window: 5m default
    energyDays: 7,
    colors: { ...METRIC_DEFAULT_COLORS },
    hidden: [],
    channelMetrics: {},
  };
  const saved = readCookie();
  Object.assign(S.settings, def, saved || {});
  // Merge metric colors with defaults (older cookies lack them), then clamp.
  S.settings.colors = Object.assign({}, METRIC_DEFAULT_COLORS, S.settings.colors || {});
  if (!(S.settings.metric in (cat.metrics || {}))) S.settings.metric = def.metric;
  if (!(cat.themes || THEMES).includes(S.settings.theme)) S.settings.theme = def.theme;
  if (!(cat.energy_units || ENERGY_UNITS).includes(S.settings.energyUnit)) S.settings.energyUnit = def.energyUnit;
  if (!CURRENT_UNITS.includes(S.settings.currentUnit)) S.settings.currentUnit = def.currentUnit;
  S.settings.dashWindowSec = clampWindow(S.settings.dashWindowSec, def.dashWindowSec);
  S.settings.chanWindowSec = clampWindow(S.settings.chanWindowSec, def.chanWindowSec);
  S.settings.energyDays = clampInt(S.settings.energyDays, ENERGY_DAYS_MIN, ENERGY_DAYS_MAX, def.energyDays);
  S.settings.hidden = (S.settings.hidden || []).filter((h) =>
    (cat.channels || []).some((c) => c.id === h)
  );
  // Keep per-channel metric memories that are still valid for that channel
  // kind (aggregates only support power).
  const memories = {};
  (cat.channels || []).forEach((ch) => {
    const mem = (S.settings.channelMetrics || {})[ch.id];
    const allowed = (ch.metrics && ch.metrics.length) ? ch.metrics
      : (ch.kind === "aggregate" ? ["power_w"] : ["power_w", "voltage_v", "current_a"]);
    if (mem && allowed.includes(mem)) memories[ch.id] = mem;
  });
  S.settings.channelMetrics = memories;
  writeCookie();
}

function clampWindow(v, fallback) {
  v = Number(v);
  return WINDOWS.includes(v) ? v : fallback;
}
function clampInt(v, lo, hi, fallback) {
  v = parseInt(v, 10);
  if (Number.isNaN(v)) return fallback;
  return Math.min(hi, Math.max(lo, v));
}
// Colour used for a single-metric series / the energy history chart.
function metricColor(key) {
  return (S.settings.colors && S.settings.colors[key]) || METRIC_DEFAULT_COLORS[key] || "#888888";
}

// Metrics a given channel can actually plot (aggregates: power only).
function channelAllowedMetrics(ch) {
  if (!ch) return ["power_w"];
  const list = (ch.metrics && ch.metrics.length) ? ch.metrics : [];
  if (list.length) return list;
  return ch.kind === "aggregate" ? ["power_w"] : ["power_w", "voltage_v", "current_a"];
}

// Best metric for a channel: its remembered one if valid, else the current
// dashboard metric if valid for that channel, else power.
function channelMetricFor(id) {
  const ch = (S.channels || []).find((c) => c.id === id);
  if (!ch) return "power_w";
  const allowed = channelAllowedMetrics(ch);
  const mem = (S.settings.channelMetrics || {})[id];
  if (mem && allowed.includes(mem)) return mem;
  if (allowed.includes(S.settings.metric)) return S.settings.metric;
  return allowed.includes("power_w") ? "power_w" : (allowed[0] || "power_w");
}

// Metric currently graphed: per-channel while a channel is focused, otherwise
// the dashboard metric chosen in Settings.
function currentMetric() {
  if (S.view.name === "channel" && S.activeChannelMetric) return S.activeChannelMetric;
  return S.settings.metric;
}

// Remember the metric the user last used for ``id`` (persisted in the cookie).
function rememberChannelMetric(id, key) {
  if (!S.settings.channelMetrics) S.settings.channelMetrics = {};
  S.settings.channelMetrics[id] = key;
  writeCookie();
}

function applyTheme() {
  document.documentElement.dataset.theme = S.settings.theme;
}

// ---- cards ------------------------------------------------------------------
function createCards() {
  const wrap = $("cards");
  wrap.innerHTML = "";
  S.channels = (S.catalog.channels || []).slice();
  S.channels.forEach((ch, i) => {
    const pal = PALETTE[S.settings.theme] || PALETTE.dark;
    const color = pal[i % pal.length];
    S.meta[ch.id] = {
      name: ch.name,
      label: ch.label || ch.name,
      kind: ch.kind,
      metrics: ch.metrics || [],
      color,
      v: [], a: [], w: [], e: [],
      last: { v: null, a: null, w: null, s: 0, t: 0 },
      daily: null, // last-7-days Wh bars: { prev:[6], base, anchor, today }
    };

    const card = document.createElement("article");
    card.className = "card";
    card.dataset.id = ch.id;
    card.innerHTML = `
      <header>
        <h3 class="card-name" title="${escapeHtml(ch.name)}">${escapeHtml(ch.label || ch.name)}</h3>
        <span class="badge">${ch.kind === "aggregate" ? "aggregate" : "rail"}</span>
        <label class="plot-toggle" title="Show on chart">
          <input type="checkbox" ${S.settings.hidden.includes(ch.id) ? "" : "checked"}>
        </label>
      </header>
      <div class="stats-line">
        <div class="subrow"></div>
        <div class="power"><span class="val">--</span><span class="unit">W</span></div>
      </div>
      <div class="energy">
        <span>total <b class="total">--</b></span>
      </div>`;
    wrap.appendChild(card);

    const cb = card.querySelector(".plot-toggle input");
    cb.addEventListener("change", () => setHidden(ch.id, !cb.checked));

    card.querySelector(".card-name").addEventListener("click", () => setView("channel", ch.id));

    S.el[ch.id] = {
      power: card.querySelector(".power .val"),
      unit: card.querySelector(".power .unit"),
      subrow: card.querySelector(".subrow"),
      total: card.querySelector(".total"),
    };
  });
}

function setHidden(id, hidden) {
  const arr = S.settings.hidden.filter((h) => h !== id);
  if (hidden) arr.push(id);
  S.settings.hidden = arr;
  writeCookie();
  S.sig = "";
  renderChart();
}

function renderCard(id) {
  const meta = S.meta[id];
  if (!meta) return;
  if (S.view.name === "dashboard") {
    if (S.el[id]) updateCardEls(S.el[id], meta);
  } else if (S.view.name === "channel" && S.table && S.table.cells[id]) {
    updateCellEls(S.table.cells[id], meta);
  }
}
function updateCardEls(ref, meta) {
  if (!ref || !meta) return;
  const last = meta.last;
  const powerParts = last.w === null ? { num: "--", unit: "W" } : fmtWParts(last.w);
  ref.power.textContent = powerParts.num;
  ref.power.style.color = metricColor("power_w");
  ref.unit.textContent = powerParts.unit;

  if (meta.kind === "rail") {
    const v = last.v === null ? "--" : last.v.toFixed(2);
    const aParts = last.a === null ? { num: "--", unit: S.settings.currentUnit === "mA" ? "mA" : "A" } : fmtAParts(last.a);
    ref.subrow.innerHTML = `
      <span class="metric-group"><span class="metric-value" style="color:${metricColor("voltage_v")}">${v}</span><span class="metric-unit">V</span></span>
      <span class="metric-group"><span class="metric-value" style="color:${metricColor("current_a")}">${aParts.num}</span><span class="metric-unit">${aParts.unit}</span></span>`;
  } else {
    ref.subrow.innerHTML = "";
  }
  ref.total.textContent = fmtEnergy(last.t || 0);
}
// Format current honouring the user's current unit (A default | mA).
function fmtA(a) {
  const parts = fmtAParts(a);
  return parts.num + " " + parts.unit;
}
function metricUnit(key) {
  const m = (S.catalog && S.catalog.metrics) ? S.catalog.metrics[key] : null;
  return (m && m.unit) || "";
}
// Current-metric value shown in a focus-table cell overlay: { num, unit }.
function fmtMetricVal(key, raw) {
  if (raw === null || raw === undefined || Number.isNaN(raw)) {
    return { num: "--", unit: metricUnit(key) };
  }
  if (key === "power_w") {
    const a = Math.abs(raw);
    if (a >= 10000) return { num: (raw / 1000).toFixed(2), unit: "kW" };
    const num = a < 1 ? raw.toFixed(3) : a < 100 ? raw.toFixed(2) : raw.toFixed(1);
    return { num, unit: "W" };
  }
  if (key === "current_a") {
    const s = fmtA(raw);
    const sp = s.lastIndexOf(" ");
    return { num: s.slice(0, sp), unit: s.slice(sp + 1) };
  }
  if (key === "voltage_v") return { num: raw.toFixed(2), unit: "V" };
  return { num: Number(raw).toFixed(2), unit: metricUnit(key) };
}
// Update the focused channel: big value stat, and every metric-tile readout.
function updateCellEls(cell, meta) {
  if (!cell || !meta) return;
  const key = currentMetric();
  const last = meta.last || {};
  const raw = key in ARR ? last[ARR[key]] : last.w;
  const fmt = fmtMetricVal(key, raw);
  cell.val.textContent = fmt.num;
  cell.unit.textContent = fmt.unit;
  Object.keys(cell.tiles || {}).forEach((k) => {
    const t = cell.tiles[k];
    if (t && t.value) t.value.textContent = fmtTileValue(k, last);
  });
}

// ---- sidebar navigation / views -------------------------------------------------
function buildNav() {
  const wrap = $("navChannels");
  wrap.innerHTML = "";
  S.channels.forEach((ch) => {
    // Sidebar shows a short name (drop a trailing " (total)" from aggregate
    // labels); the full label is still used on cards/detail headers.
    const short = String(ch.label || ch.name).replace(/\s*\(total\)\s*$/i, "");
    const b = document.createElement("button");
    b.type = "button";
    b.className = "nav-item nav-channel";
    b.dataset.view = "channel";
    b.dataset.channel = ch.id;
    b.title = "Show " + (ch.label || ch.name);
    const label = document.createElement("span");
    label.className = "nav-label";
    label.textContent = short;
    b.append(label);
    b.addEventListener("click", () => setView("channel", ch.id));
    const li = document.createElement("li");
    li.appendChild(b);
    wrap.appendChild(li);
  });
}

function setShown(el, on) {
  el.hidden = !on;
  el.style.display = on ? "" : "none";
}

function applyViewVisibility() {
  const v = S.view.name;
  setShown($("cards"), v === "dashboard");
  setShown($("channelView"), v === "channel");
  setShown($("chartBox"), v === "dashboard"); // channel charts live in #channelFocus
  setShown($("settingsView"), v === "settings");
}

function setNav() {
  $$(".nav-item").forEach((b) => {
    const active =
      (S.view.name === "dashboard" && b.id === "navDashboard") ||
      (S.view.name === "settings" && b.id === "navSettings") ||
      (S.view.name === "channel" &&
        b.dataset.view === "channel" &&
        String(b.dataset.channel) === String(S.view.id));
    b.classList.toggle("is-active", active);
  });
}

function renderChannelHeader(id) {
  const ch = S.channels.find((c) => c.id === id);
  if (!ch) return;
  $("detailTitle").textContent =
    (ch.label || ch.name) + " · Channel " + (S.channels.indexOf(ch) + 1);
  $("detailBadge").textContent = ch.kind === "aggregate" ? "aggregate" : "rail";
}

// ---- channel focus (single panel for the selected channel) ------------------
function teardownTable() {
  if (S.table) {
    (S.table.charts || []).forEach((u) => {
      if (u) { try { u.destroy(); } catch (_e) {} }
    });
    S.table = null;
  }
  const host = $("channelFocus");
  if (host) host.innerHTML = "";
}

// Metric buffers live on the S.meta[id] entry: v/a/w arrays, plus `e` for the
// cumulative total-Wh series (used by the read-only Energy tile).
function bufferForMetric(meta, metricKey) {
  if (metricKey === "energy") return meta.e || [];
  return meta[ARR[metricKey] || "w"] || [];
}

// Chartable metric tiles for this channel kind (can become the main graph).
function metricTileKeys(kind) {
  const keys = kind === "rail" ? ["voltage_v", "current_a"] : [];
  keys.push("power_w");
  return keys;
}

function metricTileLabel(key) {
  const m = (S.catalog && S.catalog.metrics) ? S.catalog.metrics[key] : null;
  return (m && m.label) || key;
}

// Current readout shown on a metric tile.
function fmtTileValue(key, last) {
  if (key === "voltage_v") return last.v === null || last.v === undefined ? "--" : last.v.toFixed(2) + " V";
  if (key === "current_a") return last.a === null || last.a === undefined ? "--" : fmtA(last.a);
  if (key === "power_w") return last.w === null || last.w === undefined ? "--" : fmtW(last.w);
  if (key === "energy") return fmtEnergy(last.t || 0);
  return "--";
}

// Live value text shown in the main-chart legend for a raw data point.
function fmtLegend(key, v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  if (key === "voltage_v") return v.toFixed(2) + " V";
  if (key === "current_a") return fmtA(v);
  if (key === "power_w") return fmtW(v);
  return String(v);
}

function buildFocusCell(ch) {
  const meta = S.meta[ch.id];
  const el = document.createElement("article");
  el.className = "tcell";
  el.dataset.id = ch.id;
  const activeKey = currentMetric();
  const tilesHtml = metricTileKeys(meta.kind)
    .map((key) => {
      const on = key === activeKey ? " is-active" : "";
      return `<button type="button" class="metric-tile${on}" data-metric="${key}">` +
        `<span class="mt-top"><span class="mt-label">${metricTileLabel(key)}</span><span class="mt-value">--</span></span>` +
        `<span class="mt-plot"></span></button>`;
    })
    .join("") +
    `<div class="metric-tile energy" data-metric="energy" title="Energy total (read-only)">` +
    `<span class="mt-top"><span class="mt-label">Energy total</span><span class="mt-value">--</span></span>` +
    `<span class="mt-bars" aria-hidden="true"></span></div>`;

  el.innerHTML = `
    <div class="main-stat"><span class="ms-val">--</span><span class="ms-unit"></span></div>
    <div class="plot-wrap">
      <div class="cell-plot"></div>
    </div>
    <div class="metric-grid">${tilesHtml}</div>`;

  const cell = {
    meta,
    root: el,
    box: el.querySelector(".cell-plot"),
    val: el.querySelector(".ms-val"),
    unit: el.querySelector(".ms-unit"),
    tiles: {},
    charts: [], // every uPlot owned by this cell (main + tiles)
  };
  el.querySelectorAll(".metric-tile").forEach((t) => {
    const key = t.dataset.metric;
    cell.tiles[key] = { key, root: t, value: t.querySelector(".mt-value"), plot: t.querySelector(".mt-plot"), chart: null };
    if (key !== "energy") t.addEventListener("click", () => selectTileMetric(key));
  });
  // Energy tile hosts the N-day daily-consumption bar strip.
  cell.barsEl = el.querySelector(".mt-bars") || null;
  cell.barsN = 0;
  return cell;
}

// Single-series uPlot used for the main (big) chart and each metric tile.
// The big chart is a full chart (legend + x/y axes + grid) like the Dashboard;
// tiles stay minimal sparklines (no axes/legend).
function makeFocusPlot(box, meta, metricKey, big) {
  if (!box) return null;
  const width = Math.max(big ? 320 : 120, box.clientWidth || (big ? 640 : 200));
  const height = big ? 320 : 56;
  const color = metricColor(metricKey === "energy" ? "energy" : metricKey);

  const opts = {
    width,
    height,
    legend: { show: false },
    scales: { x: { time: true }, y: { auto: true } },
    cursor: { show: false },
  };

  if (big) {
    const { axis, grid } = chartLineColors();
    opts.axes = [
      { stroke: axis, grid: { stroke: grid }, ticks: { stroke: axis } },
      {
        stroke: axis,
        grid: { stroke: grid },
        ticks: { stroke: axis },
        size: 56,
      },
    ];
  } else {
    opts.axes = [
      { scale: "x", show: false },
      { scale: "y", show: false },
    ];
  }

  opts.series = [
    { label: "time" },
    {
      label: big ? meta.label : metricTileLabel(metricKey),
      stroke: color,
      width: big ? 1.8 : 1.3,
      points: { show: false },
      fill: (!big && metricKey !== "energy") ? color + "22" : undefined,
      value: (u, v) => fmtLegend(metricKey, v),
    },
  ];

  const u = new uPlot(opts, [S.timeline, bufferForMetric(meta, metricKey)], box);
  return u;
}

function buildChannelFocus(activeId) {
  teardownTable();
  const ch = S.channels.find((c) => c.id === activeId);
  if (!ch) return;
  S.activeChannelMetric = channelMetricFor(activeId);
  const cell = buildFocusCell(ch);
  $("channelFocus").appendChild(cell.root);
  // Main (hero) chart shows this channel's metric: remembered per channel,
  // falling back to power for aggregates / the dashboard metric for rails.
  const mainU = makeFocusPlot(cell.box, cell.meta, currentMetric(), true);
  cell.chart = mainU;
  cell.charts.push(mainU);
  // A mini sparkline for every chartable metric tile (not the Energy tile).
  Object.keys(cell.tiles).forEach((key) => {
    const t = cell.tiles[key];
    if (!t.plot) return;
    const u = makeFocusPlot(t.plot, cell.meta, key, false);
    t.chart = u;
    cell.charts.push(u);
  });
  S.table = { cells: { [ch.id]: cell }, charts: cell.charts, activeId };
  renderCard(ch.id); // populate the hero overlay + every tile readout
  renderEnergyBars(cell); // N-day daily-consumption bars in the Energy tile
  syncTable();
}

// A metric tile becomes this channel's big main graph; the choice is stored
// per channel (aggregates only offer power, so V/A tile clicks are ignored).
function selectTileMetric(key) {
  if (!key || key === "energy" || S.view.name !== "channel") return;
  const ch = (S.channels || []).find((c) => c.id === S.view.id);
  if (!ch || !channelAllowedMetrics(ch).includes(key)) return;
  if (S.activeChannelMetric === key) return;
  S.activeChannelMetric = key;
  rememberChannelMetric(ch.id, key);
  S.sig = "";
  renderChart();
}

function destroyChart() {
  destroyDash(); // dashboard's four graphs
  if (S.u) { try { S.u.destroy(); } catch (_e) {} S.u = null; }
  S.sig = "";
}

function setView(name, id) {
  if (name === "channel" && id == null) return;
  const wasChannel = S.view.name === "channel";
  S.view = { name, id: name === "channel" ? id : null };
  applyViewVisibility();
  setNav();
  if (name === "channel") {
    // The channel-focus panel owns the chart here; free the shared dashboard chart.
    destroyChart();
    renderChannelHeader(id);
    S.sig = "";
    renderChart(); // builds / rebuilds the channel focus panel
    return;
  }
  if (wasChannel) teardownTable(); // release the channel-focus chart on the way out
  if (name === "dashboard" && S.lastSample) {
    S.channels.forEach((ch) => updateCardEls(S.el[ch.id], S.meta[ch.id]));
  } else if (name === "settings") {
    destroyChart();
    populateSettings(); // reflect current values (e.g. a tile-clicked metric)
    return; // the settings view has no chart
  }
  S.sig = "";
  renderChart();
}

function wireNav() {
  $("navDashboard").addEventListener("click", () => setView("dashboard"));
  $("navSettings").addEventListener("click", () => setView("settings"));
  $("btnBack").addEventListener("click", () => setView("dashboard"));
  const navToggle = $("navToggle");
  if (navToggle) navToggle.addEventListener("click", () => setNavOpen(!navOpen()));
}

// ---- connection / websocket -----------------------------------------------------
function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + location.host + "/ws";
}
function connect() {
  let ws;
  try {
    ws = new WebSocket(wsUrl());
  } catch (_e) {
    scheduleReconnect();
    return;
  }
  S.ws = ws;
  ws.onopen = () => updatePill();
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_e) { return; }
    handle(msg);
  };
  ws.onclose = () => {
    S.piConnected = false;
    updatePill();
    scheduleReconnect();
  };
  ws.onerror = () => { try { ws.close(); } catch (_e) {} };
}
let reconnectTimer = null;
function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 1500);
}

function handle(msg) {
  if (!msg || !msg.type) return;
  if (msg.type === "hello") onHello(msg);
  else if (msg.type === "history") onHistory(msg.history || {});
  else if (msg.type === "daily") onDaily(msg);
  else if (msg.type === "sample") onSample(msg);
}

function onHello(msg) {
  applyCatalogDefaults(msg.catalog);
  applyTheme();
  createCards();
  buildNav();
  buildSettings(); // populate the Settings page form from the catalog
  populateSettings();
  S.view = { name: "dashboard", id: null };
  setView("dashboard"); // build the sidebar and show the default dashboard view
  // reflect the Pi connection state carried by hello
  S.piConnected = !!(msg.state && msg.state.connected);
  updatePill();
}

function onHistory(history) {
  resetBuffers();
  seedFromHistory(history);
  S.sig = "";
  renderChart();
}

// ---- daily energy (N-day history on the focused channel) --------------------
// The server pushes a one-shot daily block once per Pi connection. ``vals`` is
// a Wh array, oldest -> today (today = the value at block build), paired with
// ``anchor`` = the all-time total at that same instant. Today's value is kept
// live as base + (last.t - anchor), using the cumulative total that already
// streams in every sample, so the last bar grows without re-fetching.
const WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

function onDaily(msg) {
  if (!S.catalog) return; // channels not built yet (hello precedes daily)
  const days = msg.days || null;
  const today = msg.today || null;
  S.channels.forEach((ch) => {
    const m = S.meta[ch.id];
    if (!m) return;
    const d = days ? days[String(ch.id)] : null;
    if (d && Array.isArray(d.vals) && d.vals.length) {
      const vals = d.vals.map((v) => Number(v) || 0);
      m.daily = {
        vals, // full Wh history, oldest -> today(base)
        base: vals[vals.length - 1] || 0,
        anchor: typeof d.anchor === "number" ? d.anchor : null,
        today,
      };
    } else {
      m.daily = null;
    }
  });
  renderEnergyBarsActive();
  if (S.view.name === "dashboard") { S.sig = ""; renderChart(); } // rebuild energy panel
}

// Live Wh consumed today: today's base + the streamed total delta since the
// block's anchor. Both base and anchor were captured at the same instant.
function todayLiveWh(m) {
  const d = m && m.daily;
  if (!d) return 0;
  const nowT = m.last && typeof m.last.t === "number" ? m.last.t : d.anchor;
  const anchor = typeof d.anchor === "number" ? d.anchor : nowT;
  return d.base + (nowT - anchor);
}

// Date + weekday + short M/D for a bar ``daysAgo`` before the (Pi-local) iso.
function dateAt(iso, daysAgo) {
  const parts = String(iso || "").split("-").map(Number);
  const base = parts.length === 3 && parts.every(Number.isFinite)
    ? new Date(parts[0], parts[1] - 1, parts[2])
    : new Date();
  base.setDate(base.getDate() - daysAgo);
  const wd = WEEKDAYS[base.getDay()] || "";
  return { wd, date: base.toDateString(), md: (base.getMonth() + 1) + "/" + base.getDate() };
}

// Which bars to show: the last ``settings.energyDays`` days of the received
// history (clamped to the ENERGY_DAYS_MAX / what the server actually sent),
// oldest -> today, where today is the live value.
function energyHistory(cell) {
  const meta = cell && cell.meta;
  const d = meta && meta.daily;
  if (!d || !Array.isArray(d.vals) || !d.vals.length) return null;
  const want = clampInt(S.settings.energyDays, ENERGY_DAYS_MIN, ENERGY_DAYS_MAX, ENERGY_DAYS_MIN);
  const n = Math.min(want, d.vals.length);
  const start = d.vals.length - n;
  const bars = [];
  for (let j = start; j <= d.vals.length - 2; j++) {
    bars.push({ val: d.vals[j] || 0, daysAgo: d.vals.length - 1 - j });
  }
  bars.push({ val: Math.max(0, todayLiveWh(meta)), daysAgo: 0 }); // today (live)
  return { n, today: d.today, bars };
}

// Build the N bar slots of the Energy-total tile (rebuilt only when N changes).
function buildTileBars(box, n, color) {
  box.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const bar = document.createElement("span");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.background = color;
    const lab = document.createElement("b");
    bar.append(fill, lab);
    box.appendChild(bar);
  }
}

// Refresh the N-day bar strip inside the Energy total tile of one channel cell.
function renderEnergyBars(cell) {
  const meta = cell && cell.meta;
  const box = cell && cell.barsEl;
  if (!meta || !box) return;
  const H = energyHistory(cell);
  if (!H) {
    box.innerHTML = "";
    if (cell) cell.barsN = 0;
    return;
  }
  const color = metricColor("energy");
  if (!box.childElementCount || cell.barsN !== H.bars.length) {
    buildTileBars(box, H.bars.length, color);
    cell.barsN = H.bars.length;
  }
  const bars = Array.from(box.children);
  const peak = H.bars.reduce((hi, b) => (b.val > hi ? b.val : hi), 0) || 1;
  const barH = Math.max(24, box.clientHeight || 56);
  const showLabels = H.bars.length <= 7; // single-letter weekday labels fit ~7
  const lastIdx = bars.length - 1;
  bars.forEach((bar, i) => {
    const b = H.bars[i];
    const fill = bar.querySelector("i");
    const lab = bar.querySelector("b");
    if (fill) {
      fill.style.background = color;
      fill.style.height = Math.max(2, (Math.max(0, b.val) / peak) * (barH - 10)) + "px";
    }
    bar.classList.toggle("today", i === lastIdx);
    const dt = dateAt(H.today, b.daysAgo);
    if (lab) lab.textContent = showLabels ? dt.wd : "";
    bar.title = dt.date + " · " + fmtEnergy(Math.max(0, b.val));
  });
}

// Re-render the energy bars of whichever channel cell is on screen (if any).
function renderEnergyBarsActive() {
  if (S.view.name === "channel" && S.table && S.table.cells) {
    Object.keys(S.table.cells).forEach((id) => renderEnergyBars(S.table.cells[id]));
  }
}

// ---- data buffers ---------------------------------------------------------------
function resetBuffers() {
  S.timeline = [];
  Object.keys(S.meta).forEach((k) => {
    const m = S.meta[k];
    m.v = []; m.a = []; m.w = []; m.e = [];
    m.last = { v: null, a: null, w: null, s: 0, t: 0 };
  });
}

function seedFromHistory(history) {
  // Pick a reference timeline: the channel with the most history rows.
  let ref = null;
  Object.keys(history).forEach((key) => {
    const rows = history[key];
    if (rows && rows.length && (!ref || rows.length > ref.rows.length)) {
      ref = { id: Number(key), rows };
    }
  });
  if (!ref) return;
  const axis = ref.rows.map((r) => r[0]);
  const n = axis.length;
  // Give every channel a full-length null column first.
  Object.keys(S.meta).forEach((key) => {
    const m = S.meta[key];
    m.v = new Array(n).fill(null);
    m.a = new Array(n).fill(null);
    m.w = new Array(n).fill(null);
    m.e = new Array(n).fill(null);
  });
  // Then write each channel's rows onto the axis (nearest timestamp match).
  Object.keys(history).forEach((key) => {
    const m = S.meta[Number(key)];
    if (!m) return;
    const rows = history[key];
    let j = 0;
    for (let i = 0; i < n; i++) {
      const t = axis[i];
      while (j < rows.length - 1 && rows[j + 1][0] <= t) j++;
      let pick = j;
      if (j + 1 < rows.length && Math.abs(rows[j + 1][0] - t) < Math.abs(rows[j][0] - t)) pick = j + 1;
      const r = rows[pick];
      if (Math.abs(r[0] - t) <= SEED_TOL_S) {
        m.v[i] = r[KIDX.voltage_v];
        m.a[i] = r[KIDX.current_a];
        m.w[i] = r[KIDX.power_w];
        m.e[i] = r[KIDX.total_wh];
        m.last = { v: r[KIDX.voltage_v], a: r[KIDX.current_a], w: r[KIDX.power_w], s: r[4], t: r[5] };
      }
    }
  });
  S.timeline = axis;
}

function onSample(msg) {
  const rows = msg.channels || {};
  const st = msg.state || {};
  const pi = !!st.connected;

  // A fresh Pi connection means a new server run: drop stale history so the
  // chart does not bridge across a restart gap.
  if (pi && !S._piWas && S.timeline.length > 0) {
    resetBuffers();
    S.sig = "";
  }
  S._piWas = pi;
  S.piConnected = pi;
  S.lastSample = msg.ts || Date.now() / 1000;

  S.channels.forEach((ch) => {
    const m = S.meta[ch.id];
    const row = rows[String(ch.id)];
    if (row) {
      m.last.v = row[1]; m.last.a = row[2]; m.last.w = row[3];
      m.last.s = row[4]; m.last.t = row[5];
      renderCard(ch.id);
    }
    // Hold-last-value so every series stays aligned with the shared timeline.
    m.v.push(m.last.v); m.a.push(m.last.a); m.w.push(m.last.w);
    m.e.push(m.last.t);
  });
  S.timeline.push(S.lastSample);
  trim();

  const ageEl = $("ageText");
  if (ageEl) ageEl.textContent = "age " + ageText() + "s";
  updatePill();
  renderChart();
}

function ageText() {
  const age = S.lastSample ? Date.now() / 1000 - S.lastSample : -1;
  return age < 0 ? "--" : age.toFixed(1);
}

function trim() {
  const cap = Math.max(300, (S.catalog && S.catalog.history_points) || 3600);
  if (S.timeline.length <= cap) return;
  const drop = S.timeline.length - cap;
  S.timeline.splice(0, drop);
  S.channels.forEach((ch) => {
    const m = S.meta[ch.id];
    if (m.v.length > cap) m.v.splice(0, m.v.length - cap);
    if (m.a.length > cap) m.a.splice(0, m.a.length - cap);
    if (m.w.length > cap) m.w.splice(0, m.w.length - cap);
    if (m.e.length > cap) m.e.splice(0, m.e.length - cap);
  });
}

// ---- chart -----------------------------------------------------------------------
function updatePill() {
  const pill = $("connPill");
  if (!pill) return; // connection pill removed from the header
  const wsOpen = S.ws && S.ws.readyState === WebSocket.OPEN;
  if (!wsOpen) {
    pill.textContent = "dashboard offline";
    pill.className = "bad";
  } else if (S.piConnected) {
    pill.textContent = "Pi connected · " + ageText() + "s";
    pill.className = "ok";
  } else {
    pill.textContent = "connecting to Pi…";
    pill.className = "bad";
  }
}

function chartActive() {
  return S.view.name === "dashboard" || S.view.name === "channel";
}

// --- dashboard: four graphs (voltage / current / power / energy-daily) ---------
// Each graph plots only the channels whose card "Show on chart" toggle is on.
function dashSelectedIds() {
  return S.channels.filter((ch) => !S.settings.hidden.includes(ch.id)).map((ch) => ch.id).sort();
}
function dashChannelsFor(metricKey) {
  return S.channels.filter((ch) => {
    if (S.settings.hidden.includes(ch.id)) return false;
    if (metricKey === "energy") return true; // rails + aggregates have energy
    return (ch.metrics || []).includes(metricKey); // V/A are rails-only
  });
}
function dashSig() {
  return S.settings.theme + "|" + S.settings.energyDays + "|" + dashSelectedIds().join(",");
}

function destroyDash() {
  Object.keys(S.dashU || {}).forEach((k) => {
    const u = S.dashU[k];
    if (u) { try { u.destroy(); } catch (_e) {} }
  });
  S.dashU = {};
  const host = $("dashGraphs");
  if (host) host.innerHTML = "";
}

// Time-series columns for one metric: shared timeline + one series per shown
// channel that publishes that metric.
function dashTimeData(metricKey) {
  const arrName = ARR[metricKey] || "w";
  const data = [S.timeline];
  dashChannelsFor(metricKey).forEach((ch) => data.push(S.meta[ch.id][arrName] || []));
  return data;
}

// Daily totals for the energy BAR graph: the last ``energyDays`` days, today
// live-updated, one series per selected channel. Returns
// { n, xs, last, series:[{key,color,vals}] } or null until daily data arrives.
function dashEnergySummary() {
  const all = dashChannelsFor("energy");
  const withDaily = all.filter((ch) => {
    const d = S.meta[ch.id] && S.meta[ch.id].daily;
    return d && Array.isArray(d.vals) && d.vals.length;
  });
  if (!withDaily.length) return null;
  const want = clampInt(S.settings.energyDays, ENERGY_DAYS_MIN, ENERGY_DAYS_MAX, ENERGY_DAYS_MIN);
  const n = Math.min(want, Math.min.apply(null, withDaily.map((ch) => S.meta[ch.id].daily.vals.length)));
  const first = S.meta[withDaily[0].id].daily;
  const parts = String(first.today || "").split("-").map(Number);
  const last = parts.length === 3 ? (Date.UTC(parts[0], parts[1] - 1, parts[2]) / 1000) : (Date.now() / 1000);
  const xs = [];
  for (let i = n - 1; i >= 0; i--) xs.push(last - i * 86400);
  const pal = PALETTE[S.settings.theme] || PALETTE.dark;
  const series = all.map((ch) => {
    const m = S.meta[ch.id];
    const d = m && m.daily;
    let vals;
    if (d && Array.isArray(d.vals) && d.vals.length >= n) {
      vals = d.vals.slice(d.vals.length - n, d.vals.length - 1);
      vals.push(todayLiveWh(m)); // live today replaces the stored base
    } else {
      vals = new Array(n).fill(0);
    }
    return { key: ch.id, color: pal[channelIndexFor(ch) % pal.length], vals };
  });
  return { n, xs, last, series };
}

// (Re)build the DOM grouped-bar chart in the energy dashboard panel. Each day
// is one group with a coloured bar per selected channel.
function buildDashEnergyBars(panel) {
  const box = panel && panel.querySelector(".dg-plot");
  if (!box) return;
  box.innerHTML = "";
  box.__dgb = null;
  const sum = dashEnergySummary();
  if (!sum) {
    box.innerHTML = '<div class="dgb-empty">waiting for energy history…</div>';
    return;
  }
  const tickEvery = Math.max(1, Math.ceil(sum.n / 8)); // ~8 axis labels
  const wrap = document.createElement("div");
  wrap.className = "dgb";
  for (let i = 0; i < sum.n; i++) {
    const day = document.createElement("div");
    day.className = "dgb-day";
    const bars = document.createElement("div");
    bars.className = "dgb-bars";
    sum.series.forEach(() => bars.appendChild(document.createElement("i")));
    const lab = document.createElement("b");
    const show = i === 0 || i === sum.n - 1 || i === sum.n - 2 || (i % tickEvery) === 0;
    if (show) {
      const dt = new Date(sum.xs[i] * 1000);
      lab.textContent = (dt.getUTCMonth() + 1) + "/" + dt.getUTCDate();
    }
    day.append(bars, lab);
    wrap.appendChild(day);
  }
  box.appendChild(wrap);
  box.__dgb = { n: sum.n, cols: sum.series.length };
  updateDashEnergyBars();
}

// Refresh the grouped-bar heights (today's group grows live; rebuild when N or
// the selected-channel count changed since the panel was built).
function updateDashEnergyBars() {
  const host = $("dashGraphs");
  if (!host) return;
  const panel = host.querySelector('.dash-panel[data-metric="energy"]');
  const box = panel && panel.querySelector(".dg-plot");
  if (!box) return;
  const sum = dashEnergySummary();
  if (!sum) return; // nothing yet - the placeholder stays until daily data lands
  if (!box.__dgb || box.__dgb.n !== sum.n || box.__dgb.cols !== sum.series.length) {
    buildDashEnergyBars(panel);
    return;
  }
  const peak = sum.series.reduce((hi, s) => Math.max(hi, Math.max.apply(null, s.vals)), 0) || 1;
  const area = box.querySelector(".dgb-bars");
  const areaH = area ? Math.max(10, area.clientHeight || 150) : 150;
  const days = Array.from(box.querySelectorAll(".dgb-day"));
  days.forEach((day, di) => {
    const bars = day.querySelectorAll(".dgb-bars i");
    sum.series.forEach((s, ci) => {
      const v = Math.max(0, s.vals[di] || 0);
      const el = bars[ci];
      if (el) {
        el.style.background = s.color;
        el.style.height = Math.max(2, (v / peak) * (areaH - 4)) + "px";
      }
    });
    day.classList.toggle("today", di === sum.n - 1);
  });
}

function metricLabelUnit(key) {
  if (key === "energy") return "Wh · day";
  if (key === "current_a") return "Current (" + S.settings.currentUnit + ")";
  const mm = (S.catalog && S.catalog.metrics && S.catalog.metrics[key]) || null;
  return mm ? mm.label + " (" + mm.unit + ")" : key;
}

function channelIndexFor(ch) {
  return Math.max(0, S.channels.findIndex((c) => c.id === ch.id));
}

function buildDashGraphs() {
  destroyDash();
  const host = $("dashGraphs");
  if (!host) return;
  DASH_METRICS.forEach((m) => {
    const panel = document.createElement("div");
    panel.className = "dash-panel";
    panel.dataset.metric = m.key;
    panel.innerHTML =
      `<div class="dg-head"><span class="dg-title">${m.label}</span><span class="dg-sub"></span></div>` +
      `<div class="dg-wrap"><div class="dg-plot"></div></div>`;
    host.appendChild(panel);
    S.dashU[m.key] = null;
  });
  // Build each plot only after every panel is in the DOM (width must be known).
  DASH_METRICS.forEach((m) => {
    const panel = host.querySelector('.dash-panel[data-metric="' + m.key + '"]');
    S.dashU[m.key] = makeDashPlot(m, panel);
  });
  updateDashGraphs();
}

function makeDashPlot(m, panel) {
  if (!panel) return null;
  const chs = dashChannelsFor(m.key);
  const box = panel.querySelector(".dg-plot");
  const sub = panel.querySelector(".dg-sub");
  if (!box) return null;
  const pal = PALETTE[S.settings.theme] || PALETTE.dark;
  if (sub && chs.length) {
    sub.innerHTML = chs.map((ch) => {
      const color = pal[channelIndexFor(ch) % pal.length];
      return `<span class="dg-chip"><i style="background:${color}"></i>${escapeHtml(ch.label || ch.name)}</span>`;
    }).join("");
  } else if (sub) {
    sub.textContent = "no channels selected";
  }
  // Energy-daily is a grouped BAR chart (not a time-series line chart).
  if (m.key === "energy") {
    buildDashEnergyBars(panel);
    return null;
  }
  if (!chs.length) return null;
  const width = Math.max(240, box.clientWidth || 320);
  const height = 210;
  const { axis, grid } = chartLineColors();
  const series = [{ label: "time" }];
  chs.forEach((ch) => {
    series.push({
      label: ch.label || ch.name,
      stroke: pal[channelIndexFor(ch) % pal.length],
      width: 1.6,
      points: { show: false },
      value: (u, v) => fmtLegend(m.key, v),
    });
  });
  return new uPlot({
    width,
    height,
    legend: { show: false },
    scales: { x: { time: true }, y: { auto: true } },
    cursor: { x: true, y: true },
    axes: [
      { stroke: axis, grid: { stroke: grid }, ticks: { stroke: axis } },
      { stroke: axis, grid: { stroke: grid }, ticks: { stroke: axis }, label: metricLabelUnit(m.key), size: 46 },
    ],
    series,
  }, dashTimeData(m.key), box);
}

// Refresh the four dashboard graphs: the V/A/P time-series (dashboard window)
// and the energy grouped-bar chart (live today group).
function updateDashGraphs() {
  if (!chartActive() || S.view.name !== "dashboard") return;
  const now = S.lastSample || Date.now() / 1000;
  DASH_METRICS.forEach((m) => {
    if (m.key === "energy") { updateDashEnergyBars(); return; }
    const u = S.dashU && S.dashU[m.key];
    if (!u) return;
    u.setData(dashTimeData(m.key));
    u.setScale("x", { min: now - curWindowSec(), max: now + 0.5 });
  });
}

function renderChart() {
  if (!S.catalog || !chartActive()) return; // no catalog yet / chart hidden on settings
  if (S.view.name === "channel") {
    // Single focus chart: rebuild when this channel's metric/theme change.
    S.activeChannelMetric = channelMetricFor(S.view.id);
    const sig = currentMetric() + "|" + S.settings.theme + "|" + S.view.id;
    if (sig !== S.sig) {
      buildChannelFocus(S.view.id);
      S.sig = sig;
    } else {
      syncTable();
    }
    return;
  }
  // Dashboard: four graphs (V / A / P time-series + energy daily totals).
  if (dashSig() !== S.sig) {
    buildDashGraphs();
    S.sig = dashSig();
  } else {
    updateDashGraphs();
  }
}

function curWindowSec() {
  return (S.view.name === "dashboard" ? S.settings.dashWindowSec : S.settings.chanWindowSec) || 300;
}

// Push the latest buffered points into the main chart and every metric tile.
function syncTable() {
  if (!S.table) return;
  Object.keys(S.table.cells).forEach((id) => {
    const cell = S.table.cells[id];
    const now = S.lastSample || Date.now() / 1000;
    const range = { min: now - curWindowSec(), max: now + 0.5 };
    if (cell.chart) {
      cell.chart.setData([S.timeline, bufferForMetric(cell.meta, currentMetric())]);
      cell.chart.setScale("x", range);
    }
    Object.keys(cell.tiles || {}).forEach((key) => {
      const t = cell.tiles[key];
      if (!t.chart) return;
      t.chart.setData([S.timeline, bufferForMetric(cell.meta, key)]);
      t.chart.setScale("x", range);
    });
    renderEnergyBars(cell); // keep today's bar growing from the live total
  });
}

// Re-anchor the x window on whichever chart set is active.
function scrollAllCharts() {
  if (S.view.name === "channel") {
    if (!S.table) return;
    Object.keys(S.table.cells).forEach((id) => {
      const cell = S.table.cells[id];
      const now = S.lastSample || Date.now() / 1000;
      const range = { min: now - curWindowSec(), max: now + 0.5 };
      if (cell.chart) cell.chart.setScale("x", range);
      Object.keys(cell.tiles || {}).forEach((key) => {
        const u = cell.tiles[key].chart;
        if (u) u.setScale("x", range);
      });
    });
  } else {
    updateDashGraphs(); // dashboard graphs follow the dashboard window
  }
}

// ---- sidebar collapse / layout ------------------------------------------------
// The whole sidebar can collapse to a single fixed menu icon at the top-left
// (body gets .nav-closed); re-fit any live chart to the new main width.
function fitCharts() {
  if (S.view.name === "channel" && S.table) {
    Object.keys(S.table.cells).forEach((id) => {
      const cell = S.table.cells[id];
      if (cell.chart && cell.box) {
        cell.chart.setSize({ width: Math.max(320, cell.box.clientWidth || 640), height: 300 });
      }
      Object.keys(cell.tiles || {}).forEach((key) => {
        const t = cell.tiles[key];
        if (t.chart && t.plot) {
          t.chart.setSize({ width: Math.max(120, t.plot.clientWidth || 200), height: 56 });
        }
      });
      renderEnergyBars(cell);
    });
  } else if (chartActive() && S.dashU) {
    // Re-fit every dashboard graph to its panel's current width.
    Object.keys(S.dashU).forEach((k) => {
      const u = S.dashU[k];
      if (!u) return;
      const box = document.querySelector('.dash-panel[data-metric="' + k + '"] .dg-plot');
      if (box) {
        const h = k === "energy" ? 200 : 210;
        u.setSize({ width: Math.max(240, box.clientWidth || 320), height: h });
      }
    });
  }
}

function navOpen() {
  return !document.body.classList.contains("nav-closed");
}

function setNavOpen(open) {
  document.body.classList.toggle("nav-closed", !open);
  const toggle = $("navToggle");
  if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
  requestAnimationFrame(fitCharts);
}

// ---- wiring ----------------------------------------------------------------------
function cap(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

// (Re)build the Settings view content from the catalog. Each field persists to
// the "pwm_settings" cookie on change.
function buildSettings() {
  const host = $("settingsView");
  if (!host) return;
  const cat = S.catalog || {};
  const themes = (cat.themes || THEMES);
  const metrics = cat.metrics || {};
  const metricOptions = Object.keys(metrics)
    .map((k) => `<option value="${k}">${(metrics[k] && metrics[k].label) || k}</option>`)
    .join("");
  const themeSeg = themes.map((t) =>
    `<button type="button" class="seg-btn" data-theme="${t}">${cap(t)}</button>`).join("");
  host.innerHTML = `
    <h2>Settings</h2>
    <p class="muted">Choices are saved in a <code>pwm_settings</code> cookie on this browser.</p>

    <fieldset class="set"><legend>Appearance</legend>
      <div class="row"><span class="row-label">Color mode</span>
        <div class="seg" id="setThemeSeg">${themeSeg}</div></div>
    </fieldset>

    <fieldset class="set"><legend>Graph</legend>
      <div class="row"><span class="row-label">Metric</span>
        <select id="setMetric">${metricOptions || `<option value="power_w">Power</option>`}</select></div>
      <div class="row"><span class="row-label">Dashboard window</span>
        <select id="setDashWin"></select></div>
      <div class="row"><span class="row-label">Channel window</span>
        <select id="setChanWin"></select></div>
    </fieldset>

    <fieldset class="set"><legend>Units</legend>
      <div class="row"><span class="row-label">Energy</span><select id="setEnergy"></select></div>
      <div class="row"><span class="row-label">Current</span><select id="setCurrent"></select></div>
    </fieldset>

    <fieldset class="set"><legend>Energy total</legend>
      <div class="row"><span class="row-label">Days shown</span>
        <input id="setDays" type="number" min="${ENERGY_DAYS_MIN}" max="${ENERGY_DAYS_MAX}" step="1">
        <span class="hint">${ENERGY_DAYS_MIN}–${ENERGY_DAYS_MAX} days</span></div>
    </fieldset>

    <fieldset class="set"><legend>Metric colors</legend>
      <div class="row"><span class="row-label">Voltage</span><input id="colVoltage" type="color"></div>
      <div class="row"><span class="row-label">Current</span><input id="colCurrent" type="color"></div>
      <div class="row"><span class="row-label">Power</span><input id="colPower" type="color"></div>
      <div class="row"><span class="row-label">Energy</span><input id="colEnergy" type="color"></div>
    </fieldset>

    <div class="set-actions">
      <button id="btnResetS" type="button" title="Reset all settings (clears the cookie)">Reset settings</button>
    </div>`;

  const fillWin = (id, val) => {
    const s = $(id);
    if (!s) return;
    WINDOWS.forEach((w) => {
      const o = document.createElement("option");
      o.value = w;
      o.textContent = WINDOW_LABEL(w);
      s.appendChild(o);
    });
    s.value = String(val);
  };
  fillWin("setDashWin", S.settings.dashWindowSec);
  fillWin("setChanWin", S.settings.chanWindowSec);

  const fillOpts = (id, values, val) => {
    const s = $(id);
    if (!s) return;
    values.forEach((u) => {
      const o = document.createElement("option");
      o.value = u;
      o.textContent = u;
      s.appendChild(o);
    });
    s.value = val;
  };
  fillOpts("setEnergy", (cat.energy_units || ENERGY_UNITS), S.settings.energyUnit);
  fillOpts("setCurrent", CURRENT_UNITS, S.settings.currentUnit);

  populateSettings();

  const bind = (id, evt, fn) => { const el = $(id); if (el) el.addEventListener(evt, fn); };
  bind("setMetric", "change", (e) => {
    S.settings.metric = e.target.value;
    writeCookie();
    S.sig = "";
    renderChart();
  });
  bind("setDashWin", "change", (e) => {
    S.settings.dashWindowSec = Number(e.target.value);
    writeCookie();
    scrollAllCharts();
  });
  bind("setChanWin", "change", (e) => {
    S.settings.chanWindowSec = Number(e.target.value);
    writeCookie();
    scrollAllCharts();
  });
  bind("setEnergy", "change", (e) => {
    S.settings.energyUnit = e.target.value;
    writeCookie();
    refreshTextViews();
  });
  bind("setCurrent", "change", (e) => {
    S.settings.currentUnit = e.target.value;
    writeCookie();
    refreshTextViews();
  });
  bind("setDays", "input", (e) => {
    S.settings.energyDays = clampInt(e.target.value, ENERGY_DAYS_MIN, ENERGY_DAYS_MAX, ENERGY_DAYS_MIN);
    e.target.value = S.settings.energyDays;
    writeCookie();
    renderEnergyBarsActive();
    if (S.view.name === "dashboard") { S.sig = ""; renderChart(); } // rebuild energy graph
  });
  const colorIds = { colVoltage: "voltage_v", colCurrent: "current_a", colPower: "power_w", colEnergy: "energy" };
  Object.keys(colorIds).forEach((id) => bind(id, "input", (e) => {
    S.settings.colors[colorIds[id]] = e.target.value;
    writeCookie();
    S.sig = "";
    renderChart();
    renderEnergyBarsActive();
  }));
  const seg = $("setThemeSeg");
  if (seg) Array.from(seg.querySelectorAll("button")).forEach((b) => b.addEventListener("click", () => {
    S.settings.theme = b.dataset.theme;
    writeCookie();
    applyTheme();
    populateSettings();
    S.sig = "";
    renderChart();
  }));
  bind("btnResetS", "click", () => { clearCookie(); location.reload(); });
}

// Copy the current S.settings into the Settings form controls.
function populateSettings() {
  const s = S.settings;
  const setVal = (id, val) => { const el = $(id); if (el) el.value = val; };
  setVal("setMetric", s.metric);
  setVal("setDashWin", String(s.dashWindowSec));
  setVal("setChanWin", String(s.chanWindowSec));
  setVal("setEnergy", s.energyUnit);
  setVal("setCurrent", s.currentUnit);
  setVal("setDays", String(s.energyDays));
  setVal("colVoltage", s.colors.voltage_v || "");
  setVal("colCurrent", s.colors.current_a || "");
  setVal("colPower", s.colors.power_w || "");
  setVal("colEnergy", s.colors.energy || "");
  const seg = $("setThemeSeg");
  if (seg) Array.from(seg.querySelectorAll("button")).forEach((b) =>
    b.classList.toggle("is-active", b.dataset.theme === s.theme));
}

// Re-format all on-screen numbers after a unit change.
function refreshTextViews() {
  S.channels.forEach((ch) => renderCard(ch.id));
  renderEnergyBarsActive();
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => { t = null; fn(...args); }, ms);
  };
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---- boot ------------------------------------------------------------------------
window.addEventListener("resize", debounce(fitCharts, 150));
wireNav();
connect();
