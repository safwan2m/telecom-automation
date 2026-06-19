#!/usr/bin/env python3
"""
Network Map Server — live Leaflet.js map of all Malleswaram cells + AI chat panel.

GET /          Leaflet map page with right-side AI chat
GET /api/cells Cell list + live KPIs + RF coverage radius (JSON)
POST /api/chat         Proxy → orchestrator /chat
GET  /api/history      Proxy → orchestrator /history
DELETE /api/history    Proxy → orchestrator /history
GET  /api/tools        Proxy → orchestrator /tools
GET  /api/orch-health  Proxy → orchestrator /health
GET  /health
"""

import math
import os
import logging
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONTROLLER_URL    = os.environ.get("CONTROLLER_URL",    "http://controller:8080")
ORCHESTRATOR_URL  = os.environ.get("ORCHESTRATOR_URL",  "http://orchestrator:8082")

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
            padding: 10px 18px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; flex-shrink: 0; }
  #header h1 { font-size: 1.1rem; font-weight: 600; color: #fff; letter-spacing: .5px; }
  .badge { font-size: 0.75rem; background: #2d3142; border-radius: 12px;
           padding: 3px 10px; color: #a0a8c0; }
  .badge.live { background: #1a3a2a; color: #4ade80; }
  #controls { background: #1a1d27; border-bottom: 1px solid #2d3142;
              padding: 8px 18px; display: flex; gap: 12px; flex-wrap: wrap;
              align-items: center; font-size: 0.8rem; flex-shrink: 0; }
  #controls label { cursor: pointer; display: flex; align-items: center; gap: 5px; }
  #controls input[type=checkbox] { accent-color: #60a5fa; }
  #refresh-btn { margin-left: auto; background: #2563eb; border: none; color: #fff;
                 padding: 5px 14px; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }
  #refresh-btn:hover { background: #1d4ed8; }

  /* ── main content row ── */
  #content-wrapper { display: flex; flex: 1; min-height: 0; overflow: hidden; }
  #map { flex: 1; min-width: 0; }

  #statusbar { background: #1a1d27; border-top: 1px solid #2d3142;
               padding: 5px 18px; font-size: 0.72rem; color: #6b7280;
               display: flex; gap: 20px; flex-shrink: 0; }
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

  /* ── chat panel ── */
  #chat-panel {
    width: 380px;
    min-width: 380px;
    display: flex;
    flex-direction: column;
    background: #141720;
    border-left: 1px solid #2d3142;
    transition: width 0.2s ease, min-width 0.2s ease;
  }
  #chat-panel.collapsed {
    width: 36px;
    min-width: 36px;
    overflow: hidden;
  }
  #chat-panel.collapsed #chat-body { display: none; }

  #chat-header {
    padding: 8px 10px;
    border-bottom: 1px solid #2d3142;
    display: flex;
    align-items: center;
    gap: 7px;
    background: #1a1d27;
    flex-shrink: 0;
    min-height: 40px;
  }
  #chat-header h2 {
    font-size: 0.85rem;
    font-weight: 600;
    color: #fff;
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
  }
  #orch-status { font-size: 0.68rem; color: #6b7280; white-space: nowrap; }
  #orch-dot { font-size: 0.7rem; color: #6b7280; }
  #clear-chat-btn {
    background: none;
    border: 1px solid #2d3142;
    color: #9ca3af;
    padding: 2px 7px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.7rem;
    white-space: nowrap;
  }
  #clear-chat-btn:hover { color: #f87171; border-color: #f87171; }
  #toggle-chat-btn {
    background: none;
    border: 1px solid #2d3142;
    color: #9ca3af;
    padding: 2px 6px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.8rem;
    flex-shrink: 0;
  }
  #toggle-chat-btn:hover { color: #60a5fa; border-color: #60a5fa; }

  #chat-body {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }
  #messages {
    flex: 1;
    overflow-y: auto;
    padding: 10px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-height: 0;
    scroll-behavior: smooth;
  }
  #messages::-webkit-scrollbar { width: 5px; }
  #messages::-webkit-scrollbar-track { background: #0f1117; }
  #messages::-webkit-scrollbar-thumb { background: #2d3142; border-radius: 3px; }

  .msg { max-width: 100%; padding: 7px 10px; border-radius: 8px;
         font-size: 0.78rem; line-height: 1.5; word-break: break-word; }
  .msg .msg-label { font-size: 0.62rem; color: #6b7280; margin-bottom: 3px; font-weight: 600;
                    text-transform: uppercase; letter-spacing: 0.5px; }
  .msg .msg-body { white-space: pre-wrap; }
  .msg.user { background: #1e3a5f; color: #bfdbfe;
              align-self: flex-end; max-width: 90%;
              border-radius: 8px 8px 2px 8px; }
  .msg.agent { background: #1e2433; color: #e0e0e0;
               border: 1px solid #2d3142; align-self: flex-start;
               border-radius: 2px 8px 8px 8px; max-width: 100%; }
  .msg.system-msg { background: #1a2a1a; color: #4ade80; font-size: 0.7rem;
                    align-self: center; border-radius: 4px; padding: 3px 10px; text-align: center; }
  .msg.error-msg { background: #2a1a1a; color: #f87171;
                   align-self: center; font-size: 0.7rem; border-radius: 4px; padding: 3px 10px; }

  /* agent message markdown-lite table */
  .msg.agent table { border-collapse: collapse; width: 100%; margin-top: 4px; font-size: 0.72rem; }
  .msg.agent th, .msg.agent td { border: 1px solid #2d3142; padding: 3px 7px; text-align: left; }
  .msg.agent th { background: #2d3142; color: #e0e0e0; }
  .msg.agent code { background: #0f1117; padding: 1px 4px; border-radius: 3px;
                    font-family: monospace; font-size: 0.72rem; color: #a78bfa; }

  .typing-indicator { display: flex; gap: 4px; align-items: center; padding: 8px 10px; }
  .typing-indicator span { width: 6px; height: 6px; background: #4b5563; border-radius: 50%;
                            animation: typebounce 1.2s infinite; display: inline-block; }
  .typing-indicator span:nth-child(2) { animation-delay: 0.2s; }
  .typing-indicator span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes typebounce {
    0%,80%,100% { transform: scale(0.6); opacity: 0.4; }
    40% { transform: scale(1); opacity: 1; }
  }

  #shortcuts {
    padding: 6px 10px;
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    border-top: 1px solid #2d3142;
    flex-shrink: 0;
    background: #1a1d27;
  }
  .sc-btn {
    background: #2d3142;
    border: none;
    color: #9ca3af;
    padding: 3px 7px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.7rem;
    font-family: 'Consolas', monospace;
    transition: background 0.15s;
  }
  .sc-btn:hover { background: #3d4260; color: #e0e0e0; }

  #input-area {
    padding: 8px 10px;
    border-top: 1px solid #2d3142;
    display: flex;
    gap: 6px;
    align-items: flex-end;
    flex-shrink: 0;
    background: #1a1d27;
  }
  #chat-input {
    flex: 1;
    background: #0f1117;
    border: 1px solid #2d3142;
    color: #e0e0e0;
    padding: 6px 9px;
    border-radius: 6px;
    font-size: 0.78rem;
    font-family: inherit;
    resize: none;
    min-height: 32px;
    max-height: 96px;
    line-height: 1.4;
    overflow-y: auto;
  }
  #chat-input:focus { outline: none; border-color: #2563eb; }
  #chat-input::placeholder { color: #4b5563; }
  #send-btn {
    background: #2563eb;
    border: none;
    color: #fff;
    padding: 6px 13px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.78rem;
    white-space: nowrap;
    height: 32px;
  }
  #send-btn:hover { background: #1d4ed8; }
  #send-btn:disabled { background: #374151; cursor: default; }
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
  <button id="refresh-btn" onclick="fetchCells()">&#8635; Refresh</button>
</div>

<div id="content-wrapper">
  <div id="map"></div>

  <!-- ── AI Chat Panel ── -->
  <div id="chat-panel">
    <div id="chat-header">
      <span id="orch-dot">&#9679;</span>
      <h2>AI Network Assistant</h2>
      <span id="orch-status">connecting…</span>
      <button id="clear-chat-btn" title="Clear conversation">Clear</button>
      <button id="toggle-chat-btn" title="Collapse chat panel">&#9664;</button>
    </div>
    <div id="chat-body">
      <div id="messages"></div>
      <div id="shortcuts">
        <button class="sc-btn" data-cmd="/status">/status</button>
        <button class="sc-btn" data-cmd="/alerts">/alerts</button>
        <button class="sc-btn" data-cmd="/cells">/cells</button>
        <button class="sc-btn" data-cmd="/son">/son</button>
        <button class="sc-btn" data-cmd="/ue">/ue</button>
        <button class="sc-btn" data-cmd="/plan">/plan</button>
        <button class="sc-btn" data-cmd="/history">/history</button>
        <button class="sc-btn" data-cmd="/tools">/tools</button>
        <button class="sc-btn" data-cmd="/clear">/clear</button>
      </div>
      <div id="input-area">
        <textarea id="chat-input" rows="1" placeholder="Ask about cells, alerts, plans… (Enter to send)"></textarea>
        <button id="send-btn">Send</button>
      </div>
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
<script>
// ── Map setup ────────────────────────────────────────────────────────────────
const VENDOR_COLOR = { Nokia: '#60a5fa', Ericsson: '#4ade80', Samsung: '#a78bfa', ZTE: '#fb923c' };

const map = L.map('map', { zoomControl: true }).setView([13.000, 77.570], 14);
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

// ── Chat panel ───────────────────────────────────────────────────────────────

const CHAT_SHORTCUTS = {
  '/status': 'What is the current status of all cells, DUs, and CUs? Summarise in a table.',
  '/alerts': 'Show me all recent KPI alerts from the last 60 minutes.',
  '/cells':  'List all cells with their current connected UEs, PRB utilisation, and which DU they belong to.',
  '/plan':   'Generate a network plan for Bangalore with default parameters and show me the summary.',
  '/son':    'Show me the SON agent status: what autonomous actions has it taken in the last hour, and are there any active anomalies?',
  '/ue':     'Give me a summary of UE usage patterns: which slices are most active, what are the average latencies, and how many handovers have occurred?',
};

let chatSession = 'map-' + Math.random().toString(36).slice(2, 9);
let chatBusy = false;

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// Minimal markdown renderer: bold, code, bullet lists, tables
function renderMarkdown(text) {
  const lines = text.split('\\n');
  let html = '';
  let inTable = false;
  let tableHeaderDone = false;

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];

    // Detect markdown table
    if (/^\\s*\\|/.test(line)) {
      if (!inTable) {
        html += '<table>';
        inTable = true;
        tableHeaderDone = false;
      }
      if (/^\\s*\\|[-:| ]+\\|\\s*$/.test(line)) {
        tableHeaderDone = true;
        continue;
      }
      const cells = line.trim().replace(/^\\|/, '').replace(/\\|$/, '').split('|');
      const tag = !tableHeaderDone ? 'th' : 'td';
      html += '<tr>' + cells.map(c => `<${tag}>${escHtml(c.trim())}</${tag}>`).join('') + '</tr>';
      continue;
    } else if (inTable) {
      html += '</table>';
      inTable = false;
    }

    // Bullet points
    if (/^\\s*[•\\-\\*] /.test(line)) {
      line = '• ' + line.replace(/^\\s*[•\\-\\*] /, '');
    }

    // Bold **text** and *text*
    line = escHtml(line)
      .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
      .replace(/\\*([^*]+)\\*/g, '<em>$1</em>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');

    html += line + '\\n';
  }
  if (inTable) html += '</table>';
  return html;
}

function scrollMsgs() {
  const el = document.getElementById('messages');
  el.scrollTop = el.scrollHeight;
}

function addMsg(role, text) {
  const msgs = document.getElementById('messages');
  const wrap = document.createElement('div');

  if (role === 'system') {
    wrap.className = 'msg system-msg';
    wrap.textContent = text;
  } else if (role === 'error') {
    wrap.className = 'msg error-msg';
    wrap.textContent = '⚠ ' + text;
  } else {
    wrap.className = `msg ${role}`;
    const label = document.createElement('div');
    label.className = 'msg-label';
    label.textContent = role === 'user' ? 'You' : 'AI Agent';
    const body = document.createElement('div');
    body.className = 'msg-body';
    if (role === 'agent') {
      body.innerHTML = renderMarkdown(text);
    } else {
      body.textContent = text;
    }
    wrap.appendChild(label);
    wrap.appendChild(body);
  }

  msgs.appendChild(wrap);
  scrollMsgs();
  return wrap;
}

function showTyping() {
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg agent';
  div.id = 'typing-bubble';
  div.innerHTML = '<div class="typing-indicator"><span></span><span></span><span></span></div>';
  msgs.appendChild(div);
  scrollMsgs();
}

function hideTyping() {
  const t = document.getElementById('typing-bubble');
  if (t) t.remove();
}

async function sendChat(rawInput) {
  const text = rawInput.trim();
  if (!text || chatBusy) return;

  const input = document.getElementById('chat-input');
  const btn   = document.getElementById('send-btn');
  input.value = '';
  input.style.height = '';
  chatBusy = true;
  btn.disabled = true;

  // Special local commands
  if (text === '/history') {
    try {
      const r = await fetch(`/api/history?session_id=${chatSession}`);
      const hist = await r.json();
      if (!Array.isArray(hist) || hist.length === 0) {
        addMsg('system', 'No history yet.');
      } else {
        hist.forEach(m => {
          const role = m.role === 'user' ? 'user' : 'agent';
          const c = typeof m.content === 'string' ? m.content : JSON.stringify(m.content);
          addMsg(role, c.length > 600 ? c.slice(0, 600) + '…' : c);
        });
      }
    } catch(e) { addMsg('error', 'Could not load history.'); }
    chatBusy = false; btn.disabled = false;
    return;
  }

  if (text === '/clear') {
    try {
      await fetch(`/api/history?session_id=${chatSession}`, { method: 'DELETE' });
      document.getElementById('messages').innerHTML = '';
      addMsg('system', 'Conversation cleared.');
    } catch(e) { addMsg('error', 'Could not clear history.'); }
    chatBusy = false; btn.disabled = false;
    return;
  }

  if (text === '/tools') {
    try {
      const r = await fetch('/api/tools');
      const tools = await r.json();
      const body = Array.isArray(tools)
        ? tools.map(t => `• ${t.name} — ${(t.description||'').slice(0,80)}`).join('\\n')
        : JSON.stringify(tools, null, 2);
      addMsg('agent', body);
    } catch(e) { addMsg('error', 'Could not load tools.'); }
    chatBusy = false; btn.disabled = false;
    return;
  }

  // Send to orchestrator
  const message = CHAT_SHORTCUTS[text] || text;
  addMsg('user', text);
  showTyping();

  try {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: chatSession }),
    });
    hideTyping();
    const resp = await r.text();
    if (r.ok) {
      addMsg('agent', resp);
    } else {
      addMsg('error', `HTTP ${r.status}: ${resp.slice(0, 200)}`);
    }
  } catch(e) {
    hideTyping();
    addMsg('error', 'Orchestrator unreachable — is the system running?');
  }

  chatBusy = false;
  btn.disabled = false;
}

async function checkOrchHealth() {
  const dot    = document.getElementById('orch-dot');
  const status = document.getElementById('orch-status');
  try {
    const r = await fetch('/api/orch-health');
    if (r.ok) {
      const h = await r.json();
      dot.style.color    = '#4ade80';
      status.textContent = h.model ? `model: ${h.model}` : 'online';
      status.style.color = '#4ade80';
    } else {
      throw new Error('not ok');
    }
  } catch(_) {
    dot.style.color    = '#f87171';
    status.textContent = 'offline';
    status.style.color = '#6b7280';
  }
}

// Toggle collapse
let chatCollapsed = false;
document.getElementById('toggle-chat-btn').addEventListener('click', () => {
  chatCollapsed = !chatCollapsed;
  document.getElementById('chat-panel').classList.toggle('collapsed', chatCollapsed);
  document.getElementById('toggle-chat-btn').innerHTML = chatCollapsed ? '&#9654;' : '&#9664;';
  // Let Leaflet know map size changed
  setTimeout(() => map.invalidateSize(), 220);
});

// Clear button in chat header
document.getElementById('clear-chat-btn').addEventListener('click', async () => {
  try {
    await fetch(`/api/history?session_id=${chatSession}`, { method: 'DELETE' });
    document.getElementById('messages').innerHTML = '';
    addMsg('system', 'Conversation cleared.');
  } catch(_) { addMsg('error', 'Could not clear.'); }
});

// Shortcut buttons
document.querySelectorAll('.sc-btn').forEach(btn => {
  btn.addEventListener('click', () => sendChat(btn.dataset.cmd));
});

// Send button
document.getElementById('send-btn').addEventListener('click', () => {
  sendChat(document.getElementById('chat-input').value);
});

// Enter to send, Shift+Enter for newline
document.getElementById('chat-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChat(e.target.value);
  }
});

// Auto-resize textarea
document.getElementById('chat-input').addEventListener('input', function() {
  this.style.height = '';
  this.style.height = Math.min(this.scrollHeight, 96) + 'px';
});

// Init
checkOrchHealth();
setInterval(checkOrchHealth, 30000);
addMsg('system', 'Connected — ask about cells, alerts, DUs, or network plans.');
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


# ── Orchestrator proxy routes ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    try:
        r = httpx.post(
            f"{ORCHESTRATOR_URL}/chat",
            json={"message": req.message, "session_id": req.session_id},
            timeout=120.0,
        )
        return Response(content=r.text, status_code=r.status_code, media_type="text/plain")
    except Exception as e:
        log.warning("Orchestrator unreachable for /chat: %s", e)
        return Response(content=str(e), status_code=503, media_type="text/plain")


@app.get("/api/history")
def api_history(session_id: str = Query("default")):
    try:
        r = httpx.get(f"{ORCHESTRATOR_URL}/history",
                      params={"session_id": session_id}, timeout=10.0)
        return Response(content=r.text, status_code=r.status_code, media_type="application/json")
    except Exception as e:
        log.warning("Orchestrator unreachable for /history: %s", e)
        return JSONResponse([], status_code=503)


@app.delete("/api/history")
def api_history_delete(session_id: str = Query("default")):
    try:
        r = httpx.delete(f"{ORCHESTRATOR_URL}/history",
                         params={"session_id": session_id}, timeout=10.0)
        return Response(content=r.text, status_code=r.status_code, media_type="application/json")
    except Exception as e:
        log.warning("Orchestrator unreachable for DELETE /history: %s", e)
        return JSONResponse({"error": str(e)}, status_code=503)


@app.get("/api/tools")
def api_tools():
    try:
        r = httpx.get(f"{ORCHESTRATOR_URL}/tools", timeout=10.0)
        return Response(content=r.text, status_code=r.status_code, media_type="application/json")
    except Exception as e:
        log.warning("Orchestrator unreachable for /tools: %s", e)
        return JSONResponse([], status_code=503)


@app.get("/api/orch-health")
def api_orch_health():
    try:
        r = httpx.get(f"{ORCHESTRATOR_URL}/health", timeout=5.0)
        return Response(content=r.text, status_code=r.status_code, media_type="application/json")
    except Exception as e:
        log.warning("Orchestrator unreachable for /health: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=503)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("map_server:app", host="0.0.0.0", port=8083, reload=False)
