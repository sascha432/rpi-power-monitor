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
  u: null,      // uPlot instance
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
  $("appTitle").textContent = cat.title;
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
      v: [], a: [], w: [],
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
        <span>run <b class="session">--</b></span>
      </div>`;
    wrap.appendChild(card);

    const cb = card.querySelector(".plot-toggle input");
    cb.addEventListener("change", () => setHidden(ch.id, !cb.checked));

    S.el[ch.id] = {
      power: card.querySelector(".power .val"),
      subrow: card.querySelector(".subrow"),
      total: card.querySelector(".total"),
      session: card.querySelector(".session"),
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
  const ref = S.el[id];
  const last = S.meta[id].last;
  if (!ref || !last) return;
  const meta = S.meta[id];
  ref.power.textContent = last.w === null ? "--" : fmtW(last.w);
  if (meta.kind === "rail") {
    const v = last.v === null ? "--" : last.v.toFixed(2) + " V";
    const a = last.a === null ? "--" : fmtA(last.a);
    ref.subrow.innerHTML = `<span><span class="k">V</span>${v}</span><span><span class="k">A</span>${a}</span>`;
  } else {
    ref.subrow.innerHTML = `<span class="muted">aggregate power only</span>`;
  }
  ref.total.textContent = fmtEnergy(last.t || 0);
  ref.session.textContent = fmtEnergy(last.s || 0);
}
function fmtA(a) {
  const x = Math.abs(a);
  return x < 0.1 ? a.toFixed(3) + " A" : a.toFixed(2) + " A";
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
  // reflect the Pi connection state carried by hello
  S.piConnected = !!(msg.state && msg.state.connected);
  updatePill();
  // build an empty chart so the axes appear before data arrives
  S.sig = "";
  renderChart();
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
    m.v = []; m.a = []; m.w = [];
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
  });
}

// ---- chart -----------------------------------------------------------------------
function updatePill() {
  const pill = $("connPill");
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

function visibleForMetric() {
  const m = S.settings.metric;
  return S.channels.filter((ch) => !S.settings.hidden.includes(ch.id) && ch.metrics.includes(m));
}

function buildChartData() {
  const m = S.settings.metric;
  const arrName = ARR[m] || "w";
  const data = [S.timeline];
  const ids = [];
  visibleForMetric().forEach((ch) => {
    ids.push(ch.id);
    data.push(S.meta[ch.id][arrName]);
  });
  return { ids, data };
}

function renderChart() {
  if (!S.u && !document.getElementById("chart")) return;
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
    scrollWindow();
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
    if (S.u) {
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
connect();
