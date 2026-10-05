"use strict";

const MIN_ZOOM = 15;
const MAX_BBOX_DEG = 0.07;
const RAY_LENGTH_M = 120;
const MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
const STATE_TEXT = { sun: "Солнце", shade: "Тень", night: "Ночь", inside: "Внутри здания" };

const $ = (id) => document.getElementById(id);
const pad = (n) => String(n).padStart(2, "0");
const fmtMinute = (m) => `${pad(Math.floor(m / 60))}:${pad(m % 60)}`;
const fmtDuration = (min) => `${Math.floor(min / 60)} ч ${pad(min % 60)} мин`;
const todayISO = () => {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ------------------------------------------------------------------ state

const state = {
  lat: 56.838,
  lon: 60.6057,
  zoom: 16.5,
  date: todayISO(),
  minute: 12 * 60,
  height: 1.5,
  timeline: null,
  step: 5,
  blocker: null,
};

(function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const num = (k) => (p.has(k) && !Number.isNaN(+p.get(k)) ? +p.get(k) : undefined);
  state.lat = num("lat") ?? state.lat;
  state.lon = num("lon") ?? state.lon;
  state.zoom = num("z") ?? state.zoom;
  state.height = num("h") ?? state.height;
  if (/^\d{4}-\d{2}-\d{2}$/.test(p.get("date") || "")) state.date = p.get("date");
  if (/^\d{1,2}:\d{2}$/.test(p.get("time") || "")) {
    const [h, m] = p.get("time").split(":").map(Number);
    state.minute = Math.min(1435, Math.round((h * 60 + m) / 5) * 5);
  }
})();

let hashTimer;
function writeHash() {
  clearTimeout(hashTimer);
  hashTimer = setTimeout(() => {
    const p = new URLSearchParams({
      lat: state.lat.toFixed(6),
      lon: state.lon.toFixed(6),
      z: map.getZoom().toFixed(2),
      date: state.date,
      time: fmtMinute(state.minute),
      h: String(state.height),
    });
    history.replaceState(null, "", `#${p}`);
  }, 300);
}

// ------------------------------------------------------------------- map

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      base: {
        type: "raster",
        tiles: ["a", "b", "c", "d"].map((s) => `https://${s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png`),
        tileSize: 256,
        maxzoom: 20,
        attribution:
          '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/attributions">CARTO</a>',
      },
    },
    layers: [{ id: "base", type: "raster", source: "base", paint: { "raster-saturation": -0.3 } }],
  },
  center: [state.lon, state.lat],
  zoom: state.zoom,
  pitch: 45,
  maxPitch: 70,
});
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

const markerEl = document.createElement("div");
markerEl.className = "sun-marker";
const marker = new maplibregl.Marker({ element: markerEl, draggable: true }).setLngLat([state.lon, state.lat]).addTo(map);
marker.on("dragend", () => {
  const { lng, lat } = marker.getLngLat();
  setPoint(lat, lng);
});

const empty = { type: "FeatureCollection", features: [] };

// Слои добавляем, как только готов стиль: событие "load" может не наступить,
// если тайлы подложки недоступны.
let initialized = false;
function initLayers() {
  if (initialized || !map.isStyleLoaded()) return;
  initialized = true;
  map.addSource("shadows", { type: "geojson", data: empty });
  map.addSource("buildings", { type: "geojson", data: empty, promoteId: "id" });
  map.addSource("ray", { type: "geojson", data: empty });

  map.addLayer({
    id: "shadows",
    type: "fill",
    source: "shadows",
    paint: { "fill-color": cssVar("--shadow-fill"), "fill-opacity": 0.42, "fill-antialias": false },
  });
  map.addLayer({
    id: "ray",
    type: "line",
    source: "ray",
    layout: { "line-cap": "round" },
    paint: { "line-color": cssVar("--sun"), "line-width": 3, "line-dasharray": [2, 1.5] },
  });
  map.addLayer({
    id: "buildings",
    type: "fill-extrusion",
    source: "buildings",
    paint: {
      "fill-extrusion-color": [
        "case",
        ["boolean", ["feature-state", "blocker"], false],
        cssVar("--blocker"),
        ["==", ["get", "height_source"], "default"],
        cssVar("--bld-est"),
        cssVar("--bld"),
      ],
      "fill-extrusion-height": ["get", "height"],
      "fill-extrusion-base": ["get", "min_height"],
      "fill-extrusion-opacity": 0.92,
    },
  });

  map.on("click", (e) => setPoint(e.lngLat.lat, e.lngLat.lng));
  map.on("mousemove", "buildings", (e) => {
    const p = e.features[0].properties;
    const src = { tag: "из данных", levels: "по этажности", default: "оценка" }[p.height_source];
    showTooltip(e.originalEvent, `${p.name || p.kind || "Здание"} · ${p.height} м (${src})`);
  });
  map.on("mouseleave", "buildings", hideTooltip);

  map.on("moveend", () => {
    requestBuildings();
    requestShadows();
    writeHash();
  });

  loadInfo();
  requestBuildings();
  onTimeChanged();
  requestPoint();
  requestYear();
}
// Центр карты — в видимой части, не под панелью.
function updatePadding() {
  const panel = $("panel");
  const mobile = window.innerWidth <= 600;
  map.setPadding(mobile ? { top: 0, left: 0, bottom: panel.offsetHeight, right: 0 } : { top: 0, bottom: 0, right: 0, left: panel.offsetWidth + 12 });
}
updatePadding();
map.setCenter([state.lon, state.lat]);

map.on("load", initLayers);
map.on("styledata", initLayers);
const initTimer = setInterval(() => (initialized ? clearInterval(initTimer) : initLayers()), 250);

// ---------------------------------------------------------------- network

async function getJSON(url, signal) {
  const res = await fetch(url, { signal });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

const status = { buildings: "", shadows: "", point: "", source: "" };
function setStatus(key, text) {
  status[key] = text;
  $("status").textContent = [status.buildings, status.shadows, status.point].filter(Boolean)[0] || status.source;
}

async function loadInfo() {
  try {
    const info = await getJSON("/api/info");
    setStatus("source", info.attribution);
  } catch (_) {}
}

function viewBbox() {
  const b = map.getBounds();
  let w = b.getWest(), s = b.getSouth(), e = b.getEast(), n = b.getNorth();
  const c = map.getCenter();
  const half = MAX_BBOX_DEG / 2;
  w = Math.max(w, c.lng - half); e = Math.min(e, c.lng + half);
  s = Math.max(s, c.lat - half); n = Math.min(n, c.lat + half);
  return [w, s, e, n];
}
const bboxStr = (b) => b.map((x) => x.toFixed(5)).join(",");
const contains = (outer, inner) =>
  outer && outer[0] <= inner[0] && outer[1] <= inner[1] && outer[2] >= inner[2] && outer[3] >= inner[3];
function padBbox(b, k) {
  const dx = (b[2] - b[0]) * k, dy = (b[3] - b[1]) * k;
  const out = [b[0] - dx, b[1] - dy, b[2] + dx, b[3] + dy];
  // не выходим за лимит API
  const cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2, h = MAX_BBOX_DEG / 2;
  return [Math.max(out[0], cx - h), Math.max(out[1], cy - h), Math.min(out[2], cx + h), Math.min(out[3], cy + h)];
}

let buildingsBbox = null;
let buildingsCtl = null;
async function requestBuildings() {
  const tooFar = map.getZoom() < MIN_ZOOM;
  $("zoom-hint").hidden = !tooFar;
  if (tooFar) return;
  const view = viewBbox();
  if (contains(buildingsBbox, view)) return;
  const bbox = padBbox(view, 0.25);
  buildingsCtl?.abort();
  buildingsCtl = new AbortController();
  setStatus("buildings", "Загрузка зданий…");
  try {
    const data = await getJSON(`/api/buildings?bbox=${bboxStr(bbox)}`, buildingsCtl.signal);
    buildingsBbox = bbox;
    map.getSource("buildings").setData(data);
    applyBlocker();
    setStatus("buildings", "");
  } catch (err) {
    if (err.name !== "AbortError") setStatus("buildings", `Ошибка: ${err.message}`);
  }
}

let shadowsCtl = null;
let shadowsTimer = null;
let shadowsKey = "";
let shadowsWaiters = [];
function requestShadows(immediate = false) {
  clearTimeout(shadowsTimer);
  return new Promise((resolve) => {
    shadowsWaiters.push(resolve);
    shadowsTimer = setTimeout(() => {
      const waiters = shadowsWaiters;
      shadowsWaiters = [];
      fetchShadows().finally(() => waiters.forEach((r) => r()));
    }, immediate ? 0 : 120);
  });
}
async function fetchShadows() {
  if (map.getZoom() < MIN_ZOOM) {
    map.getSource("shadows")?.setData(empty);
    shadowsKey = "";
    return;
  }
  const bbox = padBbox(viewBbox(), 0.15);
  const key = `${bboxStr(bbox)}|${state.date}|${state.minute}`;
  if (key === shadowsKey) return;
  shadowsCtl?.abort();
  shadowsCtl = new AbortController();
  setStatus("shadows", "Расчёт теней…");
  try {
    const url = `/api/shadows?bbox=${bboxStr(bbox)}&date=${state.date}&time=${fmtMinute(state.minute)}`;
    const data = await getJSON(url, shadowsCtl.signal);
    map.getSource("shadows").setData(data);
    shadowsKey = key;
    setStatus("shadows", "");
  } catch (err) {
    if (err.name !== "AbortError") setStatus("shadows", `Ошибка: ${err.message}`);
  }
}

let pointCtl = null;
let pointTimer = null;
function requestPoint(delay = 0) {
  clearTimeout(pointTimer);
  pointTimer = setTimeout(fetchPoint, delay);
}
async function fetchPoint() {
  pointCtl?.abort();
  pointCtl = new AbortController();
  setStatus("point", "Расчёт для точки…");
  const q = new URLSearchParams({
    lat: state.lat.toFixed(7),
    lon: state.lon.toFixed(7),
    date: state.date,
    time: fmtMinute(state.minute),
    height: String(state.height),
    step: String(state.step),
  });
  try {
    const data = await getJSON(`/api/point?${q}`, pointCtl.signal);
    state.timeline = data.timeline;
    state.blocker = data.blocker;
    state.sunInfo = data.sun;
    renderPoint(data);
    setStatus("point", "");
  } catch (err) {
    if (err.name !== "AbortError") setStatus("point", `Ошибка: ${err.message}`);
  }
}

let yearCtl = null;
async function requestYear() {
  yearCtl?.abort();
  yearCtl = new AbortController();
  const q = new URLSearchParams({
    lat: state.lat.toFixed(7),
    lon: state.lon.toFixed(7),
    year: state.date.slice(0, 4),
    height: String(state.height),
  });
  $("year").classList.add("loading");
  try {
    const data = await getJSON(`/api/point/year?${q}`, yearCtl.signal);
    renderYear(data.months);
  } catch (err) {
    if (err.name !== "AbortError") $("year").textContent = `Ошибка: ${err.message}`;
  }
}

// ------------------------------------------------------------ interaction

function setPoint(lat, lon, fly = false) {
  state.lat = lat;
  state.lon = lon;
  marker.setLngLat([lon, lat]);
  $("coords").value = `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
  if (fly) map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 16.5) });
  state.timeline = null;
  updateRay();
  requestPoint();
  requestYear();
  writeHash();
}

function setDate(iso) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) return;
  const yearChanged = iso.slice(0, 4) !== state.date.slice(0, 4);
  state.date = iso;
  $("date").value = iso;
  $("day").value = dayOfYear(iso);
  state.timeline = null;
  requestShadows();
  requestPoint(150);
  if (yearChanged) requestYear();
  highlightMonth();
  writeHash();
}

function dayOfYear(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return Math.round((Date.UTC(y, m - 1, d) - Date.UTC(y, 0, 1)) / 864e5) + 1;
}
function dateFromDay(year, day) {
  const d = new Date(Date.UTC(year, 0, day));
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

function setMinute(m, { fetch = true } = {}) {
  state.minute = ((Math.round(m / 5) * 5) % 1440 + 1440) % 1440;
  $("time").value = state.minute;
  onTimeChanged(fetch);
  writeHash();
}

function onTimeChanged(fetch = true) {
  $("time-label").textContent = fmtMinute(state.minute);
  renderTimelineCursor();
  renderCurrentState();
  updateRay();
  if (fetch) {
    requestShadows();
    requestPoint(350); // уточняет здание, закрывающее солнце
  }
}

function currentSample() {
  if (!state.timeline) return null;
  const i = Math.min(state.timeline.length - 1, Math.floor(state.minute / state.step));
  return state.timeline[i];
}

function updateRay() {
  const s = currentSample();
  const src = map.getSource("ray");
  if (!src) return;
  if (s) updateLight(s.azimuth, s.altitude);
  if (!s || s.altitude <= 0) {
    src.setData(empty);
    return;
  }
  const az = (s.azimuth * Math.PI) / 180;
  const dLat = (RAY_LENGTH_M * Math.cos(az)) / 110540;
  const dLon = (RAY_LENGTH_M * Math.sin(az)) / (111320 * Math.cos((state.lat * Math.PI) / 180));
  src.setData({
    type: "Feature",
    geometry: { type: "LineString", coordinates: [[state.lon, state.lat], [state.lon + dLon, state.lat + dLat]] },
    properties: {},
  });
}

function updateLight(az, alt) {
  map.setLight({
    anchor: "map",
    position: [1.5, az, Math.min(Math.max(90 - alt, 0), 89)],
    color: "#ffffff",
    intensity: alt > 0 ? 0.45 : 0.15,
  });
}

let blockerId = null;
function applyBlocker() {
  const id = state.blocker && currentSample()?.state === "shade" ? state.blocker.id : null;
  if (blockerId && blockerId !== id) map.setFeatureState({ source: "buildings", id: blockerId }, { blocker: false });
  if (id) map.setFeatureState({ source: "buildings", id }, { blocker: true });
  blockerId = id;
}

// --------------------------------------------------------------- rendering

function renderPoint(data) {
  const s = data.sun;
  $("sun-rise").textContent =
    s.polar === "day" ? "полярный день" : s.polar === "night" ? "полярная ночь" : `${s.sunrise ?? "—"} / ${s.sunset ?? "—"}`;
  $("sun-total").textContent = fmtDuration(data.sun_minutes);
  $("coords").value = `${state.lat.toFixed(6)}, ${state.lon.toFixed(6)}`;
  renderTimeline();
  renderCurrentState();
  updateRay();
}

function renderCurrentState() {
  const s = currentSample();
  const el = $("state");
  if (!s) return;
  el.className = `state ${s.state}`;
  let sub = "";
  if (s.state === "shade") {
    const b = state.blocker;
    if (s.altitude <= 0.5) sub = "солнце у горизонта";
    else if (b && b.distance != null) sub = `закрывает ${b.name || "здание"} (${b.height} м), в ${Math.round(b.distance)} м`;
    else sub = "от соседнего здания";
  } else if (s.state === "sun") sub = "прямые лучи достигают точки";
  else if (s.state === "night") sub = "солнце под горизонтом";
  else if (s.state === "inside") sub = "точка внутри здания — сдвиньте метку";
  el.innerHTML = `${STATE_TEXT[s.state]} в ${fmtMinute(state.minute)}<small>${sub}</small>`;
  $("sun-pos").textContent = `азимут ${Math.round(s.azimuth)}°, высота ${s.altitude.toFixed(1)}°`;
  applyBlocker();
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function renderTimeline() {
  const box = $("timeline");
  box.innerHTML = "";
  const tl = state.timeline;
  if (!tl) return;
  const W = box.clientWidth, H = 26;
  const svg = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img" });
  const colors = { sun: cssVar("--sun"), shade: cssVar("--shade"), night: cssVar("--night"), inside: cssVar("--night") };
  const x = (m) => (m / 1440) * W;
  const runs = [];
  for (const t of tl) {
    const last = runs[runs.length - 1];
    if (last && last.state === t.state) last.end = t.minute + state.step;
    else runs.push({ state: t.state, start: t.minute, end: t.minute + state.step });
  }
  runs.forEach((r, i) => {
    const gap = i < runs.length - 1 ? 1 : 0;
    const width = Math.max(x(r.end) - x(r.start) - gap, 1);
    svg.appendChild(svgEl("rect", { x: x(r.start), y: 2, width, height: H - 4, rx: 2, fill: colors[r.state] }));
  });
  svg.setAttribute(
    "aria-label",
    runs.filter((r) => r.state === "sun").map((r) => `солнце ${fmtMinute(r.start)}–${fmtMinute(r.end % 1440)}`).join(", ") || "солнца нет"
  );
  svg.appendChild(svgEl("line", { class: "cursor", x1: 0, x2: 0, y1: 0, y2: H }));
  box.appendChild(svg);
  renderTimelineCursor();
}

function renderTimelineCursor() {
  const line = $("timeline").querySelector(".cursor");
  if (!line) return;
  const W = $("timeline").clientWidth;
  const xm = ((state.minute + 0.5) / 1440) * W;
  line.setAttribute("x1", xm);
  line.setAttribute("x2", xm);
}

function minuteFromEvent(e) {
  const r = $("timeline").getBoundingClientRect();
  return Math.max(0, Math.min(1435, Math.floor((((e.clientX - r.left) / r.width) * 1440) / 5) * 5));
}
let scrubbing = false;
$("timeline").addEventListener("pointerdown", (e) => {
  scrubbing = true;
  $("timeline").setPointerCapture(e.pointerId);
  setMinute(minuteFromEvent(e));
});
$("timeline").addEventListener("pointermove", (e) => {
  const m = minuteFromEvent(e);
  if (scrubbing) setMinute(m);
  const t = state.timeline?.[Math.floor(m / state.step)];
  if (t) showTooltip(e, `${fmtMinute(m)} · ${STATE_TEXT[t.state]} · высота солнца ${t.altitude.toFixed(1)}°`);
});
$("timeline").addEventListener("pointerup", () => (scrubbing = false));
$("timeline").addEventListener("pointerleave", hideTooltip);

let yearData = null;
function renderYear(months) {
  yearData = months;
  const box = $("year");
  box.classList.remove("loading");
  box.innerHTML = "";
  const W = box.clientWidth, H = 120, top = 6, bottom = 16;
  const maxMin = Math.max(...months.map((m) => m.daylight_minutes), 60);
  const maxH = Math.ceil(maxMin / 60 / 4) * 4;
  const y = (min) => top + (H - top - bottom) * (1 - min / 60 / maxH);
  const svg = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` });
  const gutter = 28; // место под подписи шкалы справа
  const slot = (W - gutter) / 12, bw = Math.min(16, slot - 6);
  // сетка
  for (let h = 0; h <= maxH; h += maxH / 2) {
    svg.appendChild(svgEl("line", { x1: 0, x2: W - gutter, y1: y(h * 60), y2: y(h * 60), stroke: cssVar("--border"), "stroke-width": 1 }));
    const t = svgEl("text", { x: W, y: y(h * 60) + 3, "text-anchor": "end" });
    t.textContent = `${h} ч`;
    svg.appendChild(t);
  }
  months.forEach((m, i) => {
    const cx = slot * i + slot / 2;
    const base = y(0);
    const g = svgEl("g", { "data-month": m.month });
    g.appendChild(roundedBar(cx - bw / 2, y(m.daylight_minutes), bw, base - y(m.daylight_minutes), "bg"));
    if (m.sun_minutes > 0) g.appendChild(roundedBar(cx - bw / 2 + 2, y(m.sun_minutes), bw - 4, base - y(m.sun_minutes), "bar"));
    const label = svgEl("text", { x: cx, y: H - 3, "text-anchor": "middle", class: "mlabel" });
    label.textContent = MONTHS[i];
    g.appendChild(label);
    const hit = svgEl("rect", { x: slot * i, y: 0, width: slot, height: H, class: "hit" });
    hit.addEventListener("pointermove", (e) =>
      showTooltip(e, `${MONTHS[i]}: солнце ${fmtDuration(m.sun_minutes)} из ${fmtDuration(m.daylight_minutes)} светового дня`)
    );
    hit.addEventListener("pointerleave", hideTooltip);
    hit.addEventListener("click", () => setDate(m.date));
    g.appendChild(hit);
    svg.appendChild(g);
  });
  box.appendChild(svg);
  highlightMonth();
}

function roundedBar(x, y, w, h, cls) {
  // скругление только у верхнего края, низ на базовой линии
  const r = Math.min(4, w / 2, h);
  const d = `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  return svgEl("path", { d, class: cls });
}

function highlightMonth() {
  const month = +state.date.slice(5, 7);
  $("year").querySelectorAll(".mlabel").forEach((t, i) => {
    t.classList.toggle("val", i + 1 === month);
    t.style.fontWeight = i + 1 === month ? "700" : "";
  });
}

const tooltip = $("tooltip");
function showTooltip(e, text) {
  tooltip.textContent = text;
  tooltip.hidden = false;
  const x = Math.min(e.clientX + 12, window.innerWidth - tooltip.offsetWidth - 8);
  tooltip.style.left = `${x}px`;
  tooltip.style.top = `${e.clientY + 14}px`;
}
function hideTooltip() {
  tooltip.hidden = true;
}

// ----------------------------------------------------------------- controls

$("date").value = state.date;
$("day").value = dayOfYear(state.date);
$("time").value = state.minute;
$("obs-height").value = state.height;
$("coords").value = `${state.lat.toFixed(6)}, ${state.lon.toFixed(6)}`;

$("date").addEventListener("change", (e) => setDate(e.target.value));
$("day").addEventListener("input", (e) => setDate(dateFromDay(+state.date.slice(0, 4), +e.target.value)));
$("time").addEventListener("input", (e) => setMinute(+e.target.value));
document.querySelectorAll("[data-preset]").forEach((btn) =>
  btn.addEventListener("click", () => {
    const p = btn.dataset.preset;
    setDate(p === "today" ? todayISO() : `${state.date.slice(0, 4)}-${p}`);
  })
);
$("now").addEventListener("click", () => {
  const d = new Date();
  setDate(todayISO());
  setMinute(d.getHours() * 60 + d.getMinutes());
});
$("obs-height").addEventListener("change", (e) => {
  const v = parseFloat(e.target.value);
  if (Number.isNaN(v) || v < 0) return;
  state.height = v;
  requestPoint();
  requestYear();
  writeHash();
});
$("coords").addEventListener("change", (e) => {
  const m = e.target.value.match(/(-?\d+(?:\.\d+)?)[,\s;]+(-?\d+(?:\.\d+)?)/);
  if (m) setPoint(+m[1], +m[2], true);
});
$("pitch").addEventListener("change", (e) => map.easeTo({ pitch: e.target.checked ? 45 : 0 }));
$("collapse").addEventListener("click", () => {
  $("panel").classList.toggle("collapsed");
  updatePadding();
});

let playing = false;
$("play").addEventListener("click", () => {
  playing = !playing;
  $("play").setAttribute("aria-pressed", String(playing));
  $("play").textContent = playing ? "❚❚" : "▶";
  if (playing) playLoop();
});
async function playLoop() {
  while (playing) {
    const started = performance.now();
    setMinute(state.minute + 10, { fetch: false });
    await requestShadows(true);
    const wait = 120 - (performance.now() - started);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
  }
  requestPoint();
}

window.addEventListener("resize", () => {
  updatePadding();
  renderTimeline();
  if (yearData) renderYear(yearData);
});
