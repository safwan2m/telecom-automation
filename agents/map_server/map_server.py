#!/usr/bin/env python3
"""
Network Map Server — live Leaflet.js map of all Malleswaram cells.

GET /          Leaflet map page (auto-refreshes every 30 s)
GET /api/cells Cell list + live KPIs + RF coverage radius (JSON)
GET /health
"""

import math
import os
import logging
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://controller:8080")

app = FastAPI(title="Telecom Map Server", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── RF coverage radius model ─────────────────────────────────────────────────

_BAND_PARAMS: dict[str, dict] = {
    "n78": {"freq_mhz": 3500, "bw_mhz": 100, "pen_loss_db": 20},
    "n41": {"freq_mhz": 2500, "bw_mhz":  80, "pen_loss_db": 20},
    "n28": {"freq_mhz":  700, "bw_mhz":  20, "pen_loss_db": 15},
    "B3":  {"freq_mhz": 1800, "bw_mhz":  20, "pen_loss_db": 18},
    "B40": {"freq_mhz": 2300, "bw_mhz":  20, "pen_loss_db": 18},
}
_ANT_GAIN: dict[str, float] = {"64T64R": 24.0, "4T4R": 17.0}
_RF_EFF:   dict[str, float] = {"5G": 0.22, "4G": 0.32}


def compute_coverage_radius_m(
    band: str, tx_power_w: int, generation: str, antenna_config: str
) -> float:
    """
    COST-231-Hata Urban Macro estimate of coverage-edge radius.
    hb=25 m, hm=1.5 m, dense-urban +3 dB, UE NF=7 dB, edge SNR=-3 dB.
    """
    p        = _BAND_PARAMS.get(band, _BAND_PARAMS["n78"])
    rf_eff   = _RF_EFF.get(generation, 0.25)
    ag       = _ANT_GAIN.get(antenna_config, 17.0)
    rf_w     = max(tx_power_w * rf_eff, 0.1)
    eirp_dbm = 10 * math.log10(rf_w * 1000) + ag
    noise_dbm = -174.0 + 10 * math.log10(p["bw_mhz"] * 1e6) + 7.0
    pl_max   = eirp_dbm - (noise_dbm - 3.0) - p["pen_loss_db"]
    hb = 25.0
    A  = 46.3 + 33.9 * math.log10(p["freq_mhz"]) - 13.82 * math.log10(hb) + 3.0
    B  = 44.9 - 6.55 * math.log10(hb)
    return round((10 ** ((pl_max - A) / B)) * 1000, 1)


# ── HTML page ─────────────────────────────────────────────────────────────────

MAP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Malleswaram 4G/5G NSA — Live Cell Map</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #0f1117; color: #e0e0e0;
         height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
  #header { background: #1a1d27; border-bottom: 1px solid #2d3142;
            padding: 10px 18px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
            flex-shrink: 0; }
  #header h1 { font-size: 1.1rem; font-weight: 600; color: #fff; letter-spacing: .5px; }
  .badge { font-size: 0.75rem; background: #2d3142; border-radius: 12px;
           padding: 3px 10px; color: #a0a8c0; }
  .badge.live { background: #1a3a2a; color: #4ade80; }
  #controls { background: #1a1d27; border-bottom: 1px solid #2d3142;
              padding: 8px 18px; display: flex; gap: 12px; flex-wrap: wrap;
              align-items: center; font-size: 0.8rem; flex-shrink: 0; }
  #controls label { cursor: pointer; display: flex; align-items: center; gap: 5px; }
  #controls input[type=checkbox] { accent-color: #60a5fa; }
  #chat-toggle-btn { background: #1e3a5f; border: none; color: #60a5fa;
                     padding: 5px 14px; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }
  #chat-toggle-btn:hover { background: #2563eb; color: #fff; }
  #chat-toggle-btn.closed { background: #2d3142; color: #a0a8c0; }
  #refresh-btn { margin-left: auto; background: #2563eb; border: none; color: #fff;
                 padding: 5px 14px; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }
  #refresh-btn:hover { background: #1d4ed8; }
  #main { flex: 1; display: flex; overflow: hidden; min-height: 0; }
  #map { flex: 1; min-width: 0; }
  #statusbar { background: #1a1d27; border-top: 1px solid #2d3142;
               padding: 5px 18px; font-size: 0.72rem; color: #6b7280; display: flex;
               gap: 20px; flex-shrink: 0; }
  .legend { background: #1a1d27; border: 1px solid #2d3142; border-radius: 8px;
            padding: 10px 14px; font-size: 0.75rem; line-height: 1.8; min-width: 185px; }
  .legend b { color: #fff; }
  .legend-dot { display: inline-block; width: 11px; height: 11px; border-radius: 50%;
                margin-right: 6px; vertical-align: middle; }
  .legend-ring { display: inline-block; width: 13px; height: 13px; border-radius: 50%;
                 border: 2px solid; background: transparent; margin-right: 5px; vertical-align: middle; }
  .leaflet-popup-content-wrapper { background: #1a1d27; color: #e0e0e0;
                                   border: 1px solid #2d3142; border-radius: 8px; }
  .leaflet-popup-tip { background: #1a1d27; }
  .popup-title { font-weight: 700; font-size: 0.95rem; color: #fff; margin-bottom: 6px; }
  .popup-badge { display: inline-block; padding: 1px 8px; border-radius: 10px;
                 font-size: 0.7rem; font-weight: 600; margin-bottom: 6px; }
  .popup-grid { display: grid; grid-template-columns: auto auto; gap: 2px 14px; font-size: 0.78rem; }
  .popup-label { color: #9ca3af; }
  .popup-val { color: #e0e0e0; font-weight: 500; }
  .overload { color: #f87171 !important; font-weight: 700; }

  /* ── chat panel ──────────────────────────────────────────────────────────── */
  #chat-panel { width: 360px; flex-shrink: 0; background: #13151f;
                border-left: 1px solid #2d3142; display: flex; flex-direction: column;
                overflow: hidden; transition: width 0.18s ease; }
  #chat-panel.closed { width: 0; border-left: none; }
  #chat-header { padding: 9px 13px; border-bottom: 1px solid #2d3142; display: flex;
                 align-items: center; gap: 8px; flex-shrink: 0; white-space: nowrap;
                 overflow: hidden; }
  #chat-header-title { flex: 1; font-size: 0.83rem; font-weight: 600; color: #fff; }
  #orch-badge { font-size: 0.68rem; background: #1e3a5f; color: #60a5fa;
                padding: 2px 8px; border-radius: 10px; white-space: nowrap; }
  #orch-badge.offline { background: #3a1e1e; color: #f87171; }
  #chat-clear-btn { background: none; border: 1px solid #3d4158; color: #6b7280;
                    padding: 2px 9px; border-radius: 6px; cursor: pointer; font-size: 0.70rem; }
  #chat-clear-btn:hover { border-color: #f87171; color: #f87171; }
  #chat-messages { flex: 1; overflow-y: auto; padding: 10px; display: flex;
                   flex-direction: column; gap: 8px; min-height: 0; }
  #chat-messages::-webkit-scrollbar { width: 4px; }
  #chat-messages::-webkit-scrollbar-track { background: #13151f; }
  #chat-messages::-webkit-scrollbar-thumb { background: #3d4158; border-radius: 2px; }
  .msg-user { background: #1e3a5f; color: #e0e0e0; border-radius: 10px 10px 2px 10px;
              padding: 7px 11px; align-self: flex-end; max-width: 92%;
              font-size: 0.80rem; line-height: 1.4; word-break: break-word; }
  .msg-agent { background: #1e2030; color: #d0d4e4; border-radius: 10px 10px 10px 2px;
               padding: 8px 12px; align-self: flex-start; max-width: 98%;
               font-size: 0.79rem; line-height: 1.52; word-break: break-word; }
  .msg-agent p { margin: 3px 0; }
  .msg-agent ul, .msg-agent ol { padding-left: 16px; margin: 4px 0; }
  .msg-agent li { margin: 2px 0; }
  .msg-agent table { border-collapse: collapse; width: 100%; font-size: 0.74rem; margin: 6px 0; }
  .msg-agent th, .msg-agent td { border: 1px solid #3d4158; padding: 3px 6px; text-align: left; }
  .msg-agent th { background: #252836; color: #a0a8c0; }
  .msg-agent code { background: #252836; padding: 1px 4px; border-radius: 3px;
                    font-size: 0.74rem; font-family: 'Consolas', monospace; }
  .msg-agent pre { background: #252836; padding: 8px; border-radius: 6px;
                   overflow-x: auto; margin: 6px 0; font-size: 0.74rem; }
  .msg-agent pre code { background: none; padding: 0; }
  .msg-agent h1, .msg-agent h2, .msg-agent h3 { color: #fff; margin: 6px 0 3px; font-size: 0.87rem; }
  .msg-agent strong { color: #e0e0e0; }
  .msg-agent blockquote { border-left: 3px solid #3d4158; margin: 4px 0; padding-left: 8px;
                           color: #9ca3af; }
  .msg-tool { align-self: center; font-size: 0.69rem; color: #60a5fa; font-style: italic;
              background: #0e1f3a; border-radius: 8px; padding: 3px 10px; max-width: 94%;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .msg-error { background: #3a1e1e; color: #f87171; border-radius: 8px; padding: 7px 11px;
               align-self: stretch; font-size: 0.78rem; }
  .typing-cursor { display: inline-block; width: 2px; height: 0.9em; background: #60a5fa;
                   vertical-align: text-bottom; animation: blink 0.7s step-end infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  #chat-shortcuts { padding: 5px 10px; display: flex; gap: 5px; flex-wrap: wrap;
                    border-top: 1px solid #2d3142; flex-shrink: 0; }
  .sc-btn { background: #1a1d27; border: 1px solid #2d3142; color: #7a85a0;
            padding: 3px 9px; border-radius: 10px; cursor: pointer; font-size: 0.70rem;
            white-space: nowrap; }
  .sc-btn:hover { background: #252836; color: #fff; border-color: #3d4158; }
  #chat-input-row { padding: 8px 10px; border-top: 1px solid #2d3142; display: flex;
                    gap: 7px; flex-shrink: 0; }
  #chat-input { flex: 1; background: #1e2030; border: 1px solid #3d4158; border-radius: 8px;
                color: #e0e0e0; padding: 7px 10px; font-size: 0.79rem; resize: none;
                font-family: inherit; min-height: 36px; max-height: 100px; line-height: 1.4; }
  #chat-input::placeholder { color: #4a5168; }
  #chat-input:focus { outline: none; border-color: #2563eb; }
  #chat-send { background: #2563eb; border: none; color: #fff; border-radius: 8px;
               padding: 0 13px; cursor: pointer; font-size: 1rem; align-self: flex-end;
               height: 36px; flex-shrink: 0; }
  #chat-send:hover { background: #1d4ed8; }
  #chat-send:disabled { background: #2d3142; color: #4a5168; cursor: not-allowed; }
</style>
</head>
<body>
<div id="header">
  <h1>Malleswaram 4G/5G NSA — Live Cell Map</h1>
  <span class="badge live" id="live-dot">&#9679; LIVE</span>
  <span class="badge" id="cell-count">— cells</span>
  <span class="badge" id="last-update">Fetching…</span>
</div>
<div id="controls">
  <label><input type="checkbox" id="show5g" checked> 5G NR</label>
  <label><input type="checkbox" id="show4g" checked> 4G LTE</label>
  <label><input type="checkbox" id="showNokia" checked> <span style="color:#60a5fa">&#9679;</span> Nokia</label>
  <label><input type="checkbox" id="showEricsson" checked> <span style="color:#4ade80">&#9679;</span> Ericsson</label>
  <label><input type="checkbox" id="showSamsung" checked> <span style="color:#a78bfa">&#9679;</span> Samsung</label>
  <label><input type="checkbox" id="showZTE" checked> <span style="color:#fb923c">&#9679;</span> ZTE</label>
  <label><input type="checkbox" id="showCoverage" checked> Coverage circles</label>
  <button id="chat-toggle-btn" onclick="toggleChat()">&#128172; Chat</button>
  <button id="refresh-btn" onclick="fetchCells()">&#8635; Refresh</button>
</div>
<div id="main">
  <div id="map"></div>
  <div id="chat-panel">
    <div id="chat-header">
      <span id="chat-header-title">&#128302; Network AI</span>
      <span id="orch-badge">connecting&#8230;</span>
      <button id="chat-clear-btn" onclick="clearChat()">Clear</button>
    </div>
    <div id="chat-messages">
      <div class="msg-agent">Hi! I&#8217;m your network AI assistant. Ask me anything about the Malleswaram 4G/5G deployment &#8212; cell KPIs, anomalies, topology changes, or deployment planning.</div>
    </div>
    <div id="chat-shortcuts">
      <button class="sc-btn" onclick="runShortcut('/status')">/status</button>
      <button class="sc-btn" onclick="runShortcut('/alerts')">/alerts</button>
      <button class="sc-btn" onclick="runShortcut('/cells')">/cells</button>
      <button class="sc-btn" onclick="runShortcut('/plan')">/plan</button>
    </div>
    <div id="chat-input-row">
      <textarea id="chat-input" rows="1" placeholder="Ask the network AI&#8230; (Enter to send, Shift+Enter for newline)"></textarea>
      <button id="chat-send" onclick="sendFromInput()">&#10148;</button>
    </div>
  </div>
</div>
<div id="statusbar">
  <span id="stat-total"></span>
  <span id="stat-5g"></span>
  <span id="stat-4g"></span>
  <span id="stat-overload"></span>
  <span id="stat-ues"></span>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>
<script>
// ── Map setup ─────────────────────────────────────────────────────────────────
const VENDOR_COLOR = { Nokia: '#60a5fa', Ericsson: '#4ade80', Samsung: '#a78bfa', ZTE: '#fb923c' };

const map = L.map('map', {
  zoomControl: true,
  zoomSnap: 0.25,
  zoomDelta: 0.25,
  wheelPxPerZoomLevel: 100,
}).setView([13.000, 77.570], 14);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap &copy; CARTO',
  subdomains: 'abcd', maxZoom: 19
}).addTo(map);

// Legend
const legend = L.control({ position: 'bottomright' });
legend.onAdd = () => {
  const d = L.DomUtil.create('div', 'legend');
  d.innerHTML =
    '<b>Vendor</b><br>' +
    Object.entries(VENDOR_COLOR).map(([v,c]) =>
      `<span class="legend-dot" style="background:${c}"></span>${v}<br>`).join('') +
    '<br><b>Generation</b><br>' +
    '<span class="legend-dot" style="background:#888;opacity:0.95"></span>5G NR (solid dot)<br>' +
    '<span class="legend-dot" style="background:#888;opacity:0.55"></span>4G LTE (faded dot)<br>' +
    '<br><b>Coverage circle</b><br>' +
    '<span class="legend-ring" style="border-color:#888"></span>RF footprint (COST-231-Hata)<br>' +
    '4G circles dashed · 5G solid<br>' +
    '<br><b>Status fill</b><br>' +
    '<span class="legend-dot" style="background:#f87171"></span>Overloaded (PRB &gt;85%)<br>' +
    '<span class="legend-dot" style="background:#fbbf24"></span>SINR low (&lt;5 dB)';
  return d;
};
legend.addTo(map);

let layers = [];
let allCells = [];

function statusColor(cell) {
  const kpi  = cell.kpi || {};
  const prb  = kpi.prb_dl_pct || 0;
  const sinr = kpi.sinr_db != null ? kpi.sinr_db : 99;
  if (prb  > 85) return '#f87171';
  if (sinr <  5) return '#fbbf24';
  return VENDOR_COLOR[cell.vendor] || '#888';
}

function popupHtml(cell) {
  const kpi  = cell.kpi || {};
  const prb  = kpi.prb_dl_pct   != null ? kpi.prb_dl_pct.toFixed(1)   + '%'   : '—';
  const sinr = kpi.sinr_db       != null ? kpi.sinr_db.toFixed(1)      + ' dB' : '—';
  const ues  = kpi.connected_ues != null ? kpi.connected_ues           : '—';
  const rsrp = kpi.rsrp_dbm      != null ? kpi.rsrp_dbm.toFixed(1)     + ' dBm': '—';
  const pwr  = kpi.power_w       != null ? kpi.power_w.toFixed(0)      + ' W'  : '—';
  const tput = kpi.dl_throughput_mbps != null ? kpi.dl_throughput_mbps.toFixed(0) + ' Mbps' : '—';
  const rad  = cell.coverage_radius_m != null
                 ? (cell.coverage_radius_m >= 1000
                     ? (cell.coverage_radius_m/1000).toFixed(2) + ' km'
                     : cell.coverage_radius_m.toFixed(0) + ' m')
                 : '—';
  const prb_num = kpi.prb_dl_pct || 0;
  const bc  = VENDOR_COLOR[cell.vendor] || '#888';
  const genLabel = cell.generation === '5G'
    ? `<span class="popup-badge" style="background:#1e3a5f;color:#60a5fa">5G NR · ${cell.band}</span>`
    : `<span class="popup-badge" style="background:#1e3a1e;color:#4ade80">4G LTE · ${cell.band}</span>`;
  return `<div class="popup-title">${cell.id}</div>
    ${genLabel}
    <span class="popup-badge" style="background:#2d2040;color:${bc}">${cell.vendor} · ${cell.hardware_model}</span><br>
    <div class="popup-grid">
      <span class="popup-label">Area</span><span class="popup-val">${cell.area}</span>
      <span class="popup-label">DU / CU</span><span class="popup-val">${cell.du_id} / ${cell.cu_id}</span>
      <span class="popup-label">PCI</span><span class="popup-val">${cell.pci}</span>
      <span class="popup-label">Coverage radius</span><span class="popup-val">${rad}</span>
      <span class="popup-label">Connected UEs</span><span class="popup-val">${ues}</span>
      <span class="popup-label">PRB DL</span>
        <span class="popup-val ${prb_num>85?'overload':''}">${prb}</span>
      <span class="popup-label">SINR</span>
        <span class="popup-val ${(kpi.sinr_db||99)<5?'overload':''}">${sinr}</span>
      <span class="popup-label">RSRP</span><span class="popup-val">${rsrp}</span>
      <span class="popup-label">Power</span><span class="popup-val">${pwr}</span>
      <span class="popup-label">DL Throughput</span><span class="popup-val">${tput}</span>
    </div>`;
}

function filterVisible(cell) {
  if (cell.generation === '5G' && !document.getElementById('show5g').checked)      return false;
  if (cell.generation === '4G' && !document.getElementById('show4g').checked)      return false;
  if (cell.vendor === 'Nokia'    && !document.getElementById('showNokia').checked)    return false;
  if (cell.vendor === 'Ericsson' && !document.getElementById('showEricsson').checked) return false;
  if (cell.vendor === 'Samsung'  && !document.getElementById('showSamsung').checked)  return false;
  if (cell.vendor === 'ZTE'      && !document.getElementById('showZTE').checked)      return false;
  return true;
}

function renderMarkers() {
  layers.forEach(l => map.removeLayer(l));
  layers = [];

  const showCov = document.getElementById('showCoverage').checked;
  let overloadCount = 0, totalUes = 0, count5g = 0, count4g = 0;

  allCells.forEach(cell => {
    if (!cell.lat || !cell.lon) return;
    const kpi = cell.kpi || {};
    if ((kpi.prb_dl_pct || 0) > 85) overloadCount++;
    totalUes += kpi.connected_ues || 0;
    cell.generation === '5G' ? count5g++ : count4g++;

    if (!filterVisible(cell)) return;

    const color  = VENDOR_COLOR[cell.vendor] || '#888';
    const sColor = statusColor(cell);
    const popup  = popupHtml(cell);
    const radius = cell.coverage_radius_m || 500;
    const is5g   = cell.generation === '5G';

    // Coverage circle — RF-derived radius, semi-transparent
    if (showCov) {
      const cov = L.circle([cell.lat, cell.lon], {
        radius:      radius,
        color:       color,
        fillColor:   sColor,
        fillOpacity: 0.07,
        weight:      1.5,
        opacity:     0.45,
        dashArray:   is5g ? null : '6 4',
      }).bindPopup(popup, { maxWidth: 340 });
      cov.addTo(map);
      layers.push(cov);
    }

    // Site dot — 80 m radius, always shown, colour indicates status
    const dot = L.circle([cell.lat, cell.lon], {
      radius:      80,
      color:       sColor,
      fillColor:   sColor,
      fillOpacity: is5g ? 0.90 : 0.55,
      weight:      2.5,
    }).bindPopup(popup, { maxWidth: 340 });
    dot.addTo(map);
    layers.push(dot);
  });

  document.getElementById('stat-total').textContent    = `${allCells.length} cells total`;
  document.getElementById('stat-5g').textContent       = `${count5g} × 5G NR`;
  document.getElementById('stat-4g').textContent       = `${count4g} × 4G LTE`;
  document.getElementById('stat-overload').textContent = overloadCount > 0
    ? `⚠ ${overloadCount} overloaded` : '✓ no overload';
  document.getElementById('stat-ues').textContent      = `${totalUes.toLocaleString()} connected UEs`;
}

async function fetchCells() {
  try {
    document.getElementById('last-update').textContent = 'Refreshing…';
    const r    = await fetch('/api/cells');
    const data = await r.json();
    allCells   = data.cells || [];
    document.getElementById('cell-count').textContent    = `${allCells.length} cells`;
    document.getElementById('last-update').textContent   = 'Updated ' + new Date().toLocaleTimeString();
    document.getElementById('live-dot').style.color      = '#4ade80';
    renderMarkers();
  } catch(e) {
    document.getElementById('last-update').textContent = 'Controller unreachable';
    document.getElementById('live-dot').style.color    = '#f87171';
  }
}

['show5g','show4g','showNokia','showEricsson','showSamsung','showZTE','showCoverage']
  .forEach(id => document.getElementById(id).addEventListener('change', renderMarkers));

fetchCells();
setInterval(fetchCells, 30000);

// ── Chat panel ────────────────────────────────────────────────────────────────
// Derive orchestrator URL from current hostname so this works on any host
const ORCH_URL = window.location.protocol + '//' + window.location.hostname + ':8082';

const SHORTCUTS = {
  '/status': 'What is the current status of all cells, DUs, and CUs? Summarise in a table.',
  '/alerts': 'Show me all recent KPI alerts from the last 60 minutes.',
  '/cells':  'List all cells with their current connected UEs, PRB utilisation, and DU assignment.',
  '/plan':   'Generate a network plan for Malleswaram using default parameters and show me a summary.',
};

// Unique session per page load so chat history is isolated
const sessionId = 'map-' + Math.random().toString(36).slice(2, 9);
let chatStreaming = false;

function scrollChat() {
  const el = document.getElementById('chat-messages');
  el.scrollTop = el.scrollHeight;
}

function appendMsg(cls, html, asText) {
  const el  = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = cls;
  if (asText) { div.textContent = html; } else { div.innerHTML = html; }
  el.appendChild(div);
  scrollChat();
  return div;
}

// Split tool-call marker lines from normal text
function parseChunks(text) {
  const toolLines = [], mainLines = [];
  for (const line of text.split('\\n')) {
    if (/^\\*\\[calling tool:/.test(line.trim())) {
      toolLines.push(line.trim().replace(/^\\*|\\*$/g, ''));
    } else {
      mainLines.push(line);
    }
  }
  return { toolLines, mainText: mainLines.join('\\n') };
}

function renderAgentDiv(div, rawText) {
  const { toolLines, mainText } = parseChunks(rawText);
  let html = toolLines.map(t => `<div class="msg-tool">&#128295; ${t}</div>`).join('');
  const md = mainText.trim();
  if (md) {
    html += window.marked ? marked.parse(md) : md.replace(/\\n/g, '<br>');
  }
  div.innerHTML = html;
}

async function sendMessage(text) {
  const input   = document.getElementById('chat-input');
  const sendBtn = document.getElementById('chat-send');
  if (chatStreaming || !text.trim()) return;

  const displayText = text.trim();
  const apiMessage  = SHORTCUTS[displayText] || displayText;

  appendMsg('msg-user', displayText, true);
  input.value = '';
  input.style.height = 'auto';
  chatStreaming = true;
  sendBtn.disabled = true;

  const agentDiv = appendMsg('msg-agent', '<span class="typing-cursor"></span>');
  let accumulated = '';

  try {
    const resp = await fetch(ORCH_URL + '/chat', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: apiMessage, session_id: sessionId }),
    });

    if (!resp.ok) {
      agentDiv.className   = 'msg-error';
      agentDiv.textContent = 'Orchestrator error ' + resp.status + ': ' + resp.statusText;
      return;
    }

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      accumulated += decoder.decode(value, { stream: true });
      renderAgentDiv(agentDiv, accumulated + '\\u258b'); // live cursor char
      scrollChat();
    }
    renderAgentDiv(agentDiv, accumulated); // final — no cursor

  } catch (e) {
    agentDiv.className = 'msg-error';
    agentDiv.innerHTML = 'Cannot reach orchestrator at <code>' + ORCH_URL +
                         '</code> — is it running on port 8082?';
  } finally {
    chatStreaming    = false;
    sendBtn.disabled = false;
    input.focus();
    scrollChat();
  }
}

function sendFromInput() {
  sendMessage(document.getElementById('chat-input').value);
}

function runShortcut(key) {
  document.getElementById('chat-input').value = key;
  sendMessage(key);
}

async function clearChat() {
  try {
    await fetch(ORCH_URL + '/history?session_id=' + sessionId, { method: 'DELETE' });
  } catch (_) {}
  document.getElementById('chat-messages').innerHTML =
    '<div class="msg-agent">History cleared.</div>';
}

function toggleChat() {
  const panel = document.getElementById('chat-panel');
  const btn   = document.getElementById('chat-toggle-btn');
  panel.classList.toggle('closed');
  const closed = panel.classList.contains('closed');
  btn.classList.toggle('closed', closed);
  btn.innerHTML = closed ? '&#128172; Chat' : '&#10005; Chat';
  setTimeout(() => map.invalidateSize(), 200);
}

// Auto-resize textarea; Enter sends, Shift+Enter inserts newline
const chatInput = document.getElementById('chat-input');
chatInput.addEventListener('input', function () {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 100) + 'px';
});
chatInput.addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendFromInput(); }
});

// Probe orchestrator health on page load
(async function probeOrch() {
  const badge = document.getElementById('orch-badge');
  try {
    const r = await fetch(ORCH_URL + '/health', { signal: AbortSignal.timeout(4000) });
    const j = await r.json();
    badge.textContent = (j.model || 'gemini') + ' ●';
    badge.classList.remove('offline');
  } catch (_) {
    badge.textContent = 'offline ●';
    badge.classList.add('offline');
  }
})();
</script>
</body>
</html>"""


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return MAP_HTML


@app.get("/api/cells")
def api_cells():
    try:
        r    = httpx.get(f"{CONTROLLER_URL}/network", timeout=8.0)
        data = r.json()
    except Exception as e:
        log.warning("Controller unreachable: %s", e)
        return JSONResponse({"cells": [], "error": str(e)})

    cells = []
    for cell_id, c in data.get("cells", {}).items():
        radius_m = compute_coverage_radius_m(
            c.get("band", "n78"),
            c.get("tx_power_w", 950),
            c.get("generation", "5G"),
            c.get("antenna_config", "64T64R"),
        )
        kpi = c.get("kpi", {})
        # Prefer live radius from KPI telemetry when it's within 2× of the model estimate
        # (guards against stale InfluxDB data from a previous topology)
        live_r = kpi.get("coverage_radius_m")
        if live_r and 0.5 * radius_m <= live_r <= 2.0 * radius_m:
            radius_m = live_r

        cells.append({
            "id":               cell_id,
            "area":             c.get("area", ""),
            "lat":              c.get("lat"),
            "lon":              c.get("lon"),
            "vendor":           c.get("vendor", "unknown"),
            "hardware_model":   c.get("hardware_model", ""),
            "generation":       c.get("generation", "5G"),
            "band":             c.get("band", ""),
            "pci":              c.get("pci"),
            "du_id":            c.get("du_id"),
            "cu_id":            c.get("cu_id"),
            "coverage_radius_m": radius_m,
            "kpi":              kpi,
        })
    return {"cells": cells, "total": len(cells)}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("map_server:app", host="0.0.0.0", port=8083, reload=False)
