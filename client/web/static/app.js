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

// ---- central state ---------------------------------------------------------
const S = {
  catalog: null,
  ws: null,
  piConnected: false,
  lastSample: 0,
  timeline: [], // shared x axis (epoch s), one entry per sample tick
  channels: [], // ordered ChannelConfig list from the catalog
  meta: {},     // id -> { name,label,kind,metrics,color, v:[],a:[],w:[], last:{...} }
  el: {},       // id -> card DOM refs
  settings: { metric: "power_w", windowSec: 300, energyUnit: "kWh", theme: "dark", hidden: [] },
  sig: "",      // chart signature (rebuild when it changes)
  u: null,      // uPlot instance (Dashboard multi-series chart)
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
function fmtW(w) {
  const a = Math.abs(w);
  if (a < 1) return w.toFixed(3) + " W";
  if (a < 100) return w.toFixed(2) + " W";
  if (a < 10000) return w.toFixed(1) + " W";
  return (w / 1000).toFixed(2) + " kW";
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

// ---- settings ------------------------------------------------------------------
function applyCatalogDefaults(cat) {
  S.catalog = cat;
  document.title = cat.title;
  const appTitle = $("appTitle");
  if (appTitle) appTitle.textContent = cat.title;
  const def = {
    metric: cat.default_metric || "power_w",
    windowSec: 300,
    energyUnit: cat.energy_unit || "kWh",
    theme: cat.theme || "dark",
    hidden: [],
  };
  const saved = readCookie();
  Object.assign(S.settings, def, saved || {});
  // validate against the catalog
  if (!(S.settings.metric in (cat.metrics || {}))) S.settings.metric = def.metric;
  if (!(cat.energy_units || []).includes(S.settings.energyUnit)) S.settings.energyUnit = def.energyUnit;
  if (!(cat.themes || []).includes(S.settings.theme)) S.settings.theme = def.theme;
  S.settings.hidden = (S.settings.hidden || []).filter((h) =>
    (cat.channels || []).some((c) => c.id === h)
  );
  writeCookie();
}

function populateToolbar() {
  const cat = S.catalog;
  const selM = $("selMetric");
  selM.innerHTML = "";
  Object.keys(cat.metrics).forEach((key) => {
    const meta = cat.metrics[key];
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = meta.label + " (" + meta.unit + ")";
    selM.appendChild(opt);
  });
  selM.value = S.settings.metric;

  const selW = $("selWindow");
  selW.innerHTML = "";
  WINDOWS.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = WINDOW_LABEL(s);
    selW.appendChild(opt);
  });
  selW.value = String(S.settings.windowSec);

  const selE = $("selEnergy");
  selE.innerHTML = "";
  cat.energy_units.forEach((u) => {
    const opt = document.createElement("option");
    opt.value = u;
    opt.textContent = u;
    selE.appendChild(opt);
  });
  selE.value = S.settings.energyUnit;
}

function applyTheme() {
  document.documentElement.dataset.theme = S.settings.theme;
  const other = (S.catalog.themes || []).find((t) => t !== S.settings.theme);
  $("btnTheme").textContent = other ? other[0].toUpperCase() + other.slice(1) + " mode" : "Theme";
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
      <div class="power"><span class="val">--</span><span class="unit">W</span></div>
      <div class="subrow"></div>
      <div class="energy">
        <span>total <b class="total">--</b></span>
      </div>`;
    wrap.appendChild(card);

    const cb = card.querySelector(".plot-toggle input");
    cb.addEventListener("change", () => setHidden(ch.id, !cb.checked));

    S.el[ch.id] = {
      power: card.querySelector(".power .val"),
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
  ref.power.textContent = last.w === null ? "--" : fmtW(last.w);
  if (meta.kind === "rail") {
    const v = last.v === null ? "--" : last.v.toFixed(2) + " V";
    const a = last.a === null ? "--" : fmtA(last.a);
    ref.subrow.innerHTML = `<span><span class="k">V</span>${v}</span><span><span class="k">A</span>${a}</span>`;
  } else {
    ref.subrow.innerHTML = `<span class="muted">aggregate power only</span>`;
  }
  ref.total.textContent = fmtEnergy(last.t || 0);
}
function fmtA(a) {
  const x = Math.abs(a);
  return x < 0.1 ? a.toFixed(3) + " A" : a.toFixed(2) + " A";
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
    const a = Math.abs(raw);
    return { num: a < 0.1 ? raw.toFixed(3) : raw.toFixed(2), unit: "A" };
  }
  if (key === "voltage_v") return { num: raw.toFixed(2), unit: "V" };
  return { num: Number(raw).toFixed(2), unit: metricUnit(key) };
}
// Update the focused channel: big value stat, and every metric-tile readout.
function updateCellEls(cell, meta) {
  if (!cell || !meta) return;
  const key = S.settings.metric;
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
  S.channels.forEach((ch, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "nav-item nav-channel";
    b.dataset.view = "channel";
    b.dataset.channel = ch.id;
    b.title = "Show " + (ch.label || ch.name);
    const num = document.createElement("span");
    num.className = "nav-num";
    num.textContent = i + 1;
    const label = document.createElement("span");
    label.className = "nav-label";
    label.textContent = ch.label || ch.name;
    b.append(num, label);
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
  const withChart = v === "dashboard" || v === "channel";
  setShown($("toolbar"), withChart);
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
  const activeKey = S.settings.metric;
  const tilesHtml = metricTileKeys(meta.kind)
    .map((key) => {
      const on = key === activeKey ? " is-active" : "";
      return `<button type="button" class="metric-tile${on}" data-metric="${key}">` +
        `<span class="mt-top"><span class="mt-label">${metricTileLabel(key)}</span><span class="mt-value">--</span></span>` +
        `<span class="mt-plot"></span></button>`;
    })
    .join("") +
    `<div class="metric-tile energy" data-metric="energy" title="Energy total (read-only)">` +
    `<span class="mt-spacer" aria-hidden="true"></span>` +
    `<span class="mt-top"><span class="mt-label">Energy total</span><span class="mt-value">--</span></span></div>`;

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
  return cell;
}

// Single-series uPlot used for the main (big) chart and each metric tile.
// The big chart is a full chart (legend + x/y axes + grid) like the Dashboard;
// tiles stay minimal sparklines (no axes/legend).
function makeFocusPlot(box, meta, metricKey, big) {
  if (!box) return null;
  const pal = PALETTE[S.settings.theme] || PALETTE.dark;
  const width = Math.max(big ? 320 : 120, box.clientWidth || (big ? 640 : 200));
  const height = big ? 320 : 56;
  const color = metricKey === "energy" ? cssVar("--muted") : (meta.color || pal[0]);

  const opts = {
    width,
    height,
    legend: { show: false },
    scales: { x: { time: true }, y: { auto: true } },
    cursor: { show: false },
  };

  if (big) {
    const axis = cssVar("--axis");
    const grid = cssVar("--grid");
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
  const cell = buildFocusCell(ch);
  $("channelFocus").appendChild(cell.root);
  // Main (hero) chart follows the toolbar metric selector.
  const mainU = makeFocusPlot(cell.box, cell.meta, S.settings.metric, true);
  cell.chart = mainU;
  cell.charts.push(mainU);
  // A mini sparkline for every metric tile (incl. the cumulative Energy tile).
  Object.keys(cell.tiles).forEach((key) => {
    const t = cell.tiles[key];
    if (!t.plot) return;
    const u = makeFocusPlot(t.plot, cell.meta, key, false);
    t.chart = u;
    cell.charts.push(u);
  });
  S.table = { cells: { [ch.id]: cell }, charts: cell.charts, activeId };
  renderCard(ch.id); // populate the hero overlay + every tile readout
  syncTable();
}

// A metric tile becomes the big main graph (mirrors the toolbar Metric select).
function selectTileMetric(key) {
  if (!key || key === "energy" || S.settings.metric === key) return;
  S.settings.metric = key;
  writeCookie();
  const sel = $("selMetric");
  if (sel) sel.value = key;
  S.sig = "";
  renderChart();
}

function destroyChart() {
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
    return; // the settings view has no chart
  }
  S.sig = "";
  renderChart();
}

function wireNav() {
  $("navDashboard").addEventListener("click", () => setView("dashboard"));
  $("navSettings").addEventListener("click", () => setView("settings"));
  $("btnBack").addEventListener("click", () => setView("dashboard"));
  $("btnResetS").addEventListener("click", () => { clearCookie(); location.reload(); });
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
  else if (msg.type === "sample") onSample(msg);
}

function onHello(msg) {
  applyCatalogDefaults(msg.catalog);
  populateToolbar();
  applyTheme();
  createCards();
  buildNav();
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

  $("ageText").textContent = "age " + ageText() + "s";
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

function visibleForMetric() {
  const m = S.settings.metric;
  return S.channels.filter((ch) => !S.settings.hidden.includes(ch.id) && ch.metrics.includes(m));
}

function chartChannels() {
  // Dashboard plots every visible channel; a channel view plots just that one
  // (ignoring the plot-toggle so the focused channel is always shown).
  if (S.view.name === "channel") {
    return S.channels.filter((ch) => ch.id === S.view.id);
  }
  return visibleForMetric();
}

function buildChartData() {
  const m = S.settings.metric;
  const arrName = ARR[m] || "w";
  const data = [S.timeline];
  const ids = [];
  chartChannels().forEach((ch) => {
    ids.push(ch.id);
    data.push(S.meta[ch.id][arrName]);
  });
  return { ids, data };
}

function renderChart() {
  if (!S.catalog || !chartActive()) return; // no catalog yet / chart hidden on settings
  if (S.view.name === "channel") {
    // Single focus chart: rebuild when metric/theme/focused channel change.
    const sig = S.settings.metric + "|" + S.settings.theme + "|" + S.view.id;
    if (sig !== S.sig) {
      buildChannelFocus(S.view.id);
      S.sig = sig;
    } else {
      syncTable();
    }
    return;
  }
  const { ids, data } = buildChartData();
  const sig = S.settings.metric + "|" + S.settings.theme + "|" + ids.join(",");
  if (sig !== S.sig) {
    buildChart(ids, data);
    S.sig = sig;
  } else if (S.u) {
    S.u.setData(data);
    scrollWindow();
  }
}

function buildChart(ids, data) {
  if (S.u) { try { S.u.destroy(); } catch (_e) {} S.u = null; }
  const cat = S.catalog;
  const m = cat.metrics[S.settings.metric];
  const pal = PALETTE[S.settings.theme] || PALETTE.dark;
  const colorOf = {};
  S.channels.forEach((ch, i) => { colorOf[ch.id] = pal[i % pal.length]; });

  const series = [{ label: "time" }];
  ids.forEach((id) => {
    const chMeta = S.meta[id];
    series.push({ label: chMeta.label, stroke: colorOf[id], width: 1.6 });
  });

  const box = $("chart");
  const width = Math.max(320, box.clientWidth || 600);
  const axisColor = cssVar("--axis");
  const gridColor = cssVar("--grid");

  S.u = new uPlot(
    {
      width,
      height: 320,
      legend: { show: true },
      scales: { x: { time: true }, y: { auto: true } },
      axes: [
        { stroke: axisColor, grid: { stroke: gridColor }, ticks: { stroke: axisColor } },
        {
          stroke: axisColor,
          grid: { show: false },
          ticks: { stroke: axisColor },
          label: m ? m.label + " (" + m.unit + ")" : "",
          size: 56,
        },
      ],
      series,
      cursor: { x: true, y: true },
    },
    data,
    box
  );
  scrollWindow();
}

function scrollWindow() {
  if (!S.u) return;
  const now = S.lastSample || Date.now() / 1000;
  S.u.setScale("x", { min: now - S.settings.windowSec, max: now + 0.5 });
}

// Push the latest buffered points into the main chart and every metric tile.
function syncTable() {
  if (!S.table) return;
  Object.keys(S.table.cells).forEach((id) => {
    const cell = S.table.cells[id];
    const now = S.lastSample || Date.now() / 1000;
    const range = { min: now - S.settings.windowSec, max: now + 0.5 };
    if (cell.chart) {
      cell.chart.setData([S.timeline, bufferForMetric(cell.meta, S.settings.metric)]);
      cell.chart.setScale("x", range);
    }
    Object.keys(cell.tiles || {}).forEach((key) => {
      const t = cell.tiles[key];
      if (!t.chart) return;
      t.chart.setData([S.timeline, bufferForMetric(cell.meta, key)]);
      t.chart.setScale("x", range);
    });
  });
}

// Re-anchor the x window on whichever chart set is active.
function scrollAllCharts() {
  if (S.view.name === "channel") {
    if (!S.table) return;
    Object.keys(S.table.cells).forEach((id) => {
      const cell = S.table.cells[id];
      const now = S.lastSample || Date.now() / 1000;
      const range = { min: now - S.settings.windowSec, max: now + 0.5 };
      if (cell.chart) cell.chart.setScale("x", range);
      Object.keys(cell.tiles || {}).forEach((key) => {
        const u = cell.tiles[key].chart;
        if (u) u.setScale("x", range);
      });
    });
  } else {
    scrollWindow();
  }
}

// ---- wiring ----------------------------------------------------------------------
function wireToolbar() {
  $("selMetric").addEventListener("change", (e) => {
    S.settings.metric = e.target.value;
    writeCookie();
    S.sig = "";
    renderChart();
  });
  $("selWindow").addEventListener("change", (e) => {
    S.settings.windowSec = Number(e.target.value);
    writeCookie();
    scrollAllCharts();
  });
  $("selEnergy").addEventListener("change", (e) => {
    S.settings.energyUnit = e.target.value;
    writeCookie();
    S.channels.forEach((ch) => renderCard(ch.id));
  });
  $("btnTheme").addEventListener("click", () => {
    const themes = (S.catalog && S.catalog.themes) || ["dark", "light"];
    const next = themes[(themes.indexOf(S.settings.theme) + 1) % themes.length] || "light";
    S.settings.theme = next;
    writeCookie();
    applyTheme();
    S.sig = "";
    renderChart();
  });
  $("btnReset").addEventListener("click", () => { clearCookie(); location.reload(); });
  window.addEventListener("resize", debounce(() => {
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
      });
    } else if (chartActive() && S.u) {
      const w = Math.max(320, $("chart").clientWidth || 600);
      S.u.setSize({ width: w, height: 320 });
    }
  }, 150));
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
wireToolbar();
wireNav();
connect();
