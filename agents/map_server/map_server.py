#!/usr/bin/env python3
"""
Network Map Server — live Leaflet.js map of all Bangalore cells.

GET /          Leaflet map page (auto-refreshes every 30 s)
GET /api/cells Cell list with KPIs from Controller API (JSON)
GET /health
"""

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

# ── HTML page ─────────────────────────────────────────────────────────────────

MAP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Bangalore RAN — Live Cell Map</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #0f1117; color: #e0e0e0; height: 100vh; display: flex; flex-direction: column; }
  #header { background: #1a1d27; border-bottom: 1px solid #2d3142; padding: 10px 18px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  #header h1 { font-size: 1.1rem; font-weight: 600; color: #fff; letter-spacing: .5px; }
  #header .badge { font-size: 0.75rem; background: #2d3142; border-radius: 12px; padding: 3px 10px; color: #a0a8c0; }
  #header .badge.live { background: #1a3a2a; color: #4ade80; }
  #controls { background: #1a1d27; border-bottom: 1px solid #2d3142; padding: 8px 18px; display: flex; gap: 12px; flex-wrap: wrap; align-items: center; font-size: 0.8rem; }
  #controls label { cursor: pointer; display: flex; align-items: center; gap: 5px; }
  #controls input[type=checkbox] { accent-color: #60a5fa; }
  #refresh-btn { margin-left: auto; background: #2563eb; border: none; color: #fff; padding: 5px 14px; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }
  #refresh-btn:hover { background: #1d4ed8; }
  #map { flex: 1; }
  #statusbar { background: #1a1d27; border-top: 1px solid #2d3142; padding: 5px 18px; font-size: 0.72rem; color: #6b7280; display: flex; gap: 20px; }
  .legend { background: #1a1d27; border: 1px solid #2d3142; border-radius: 8px; padding: 10px 14px; font-size: 0.75rem; line-height: 1.8; }
  .legend-dot { display: inline-block; width: 11px; height: 11px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  .leaflet-popup-content-wrapper { background: #1a1d27; color: #e0e0e0; border: 1px solid #2d3142; border-radius: 8px; }
  .leaflet-popup-tip { background: #1a1d27; }
  .popup-title { font-weight: 700; font-size: 0.95rem; color: #fff; margin-bottom: 6px; }
  .popup-badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.7rem; font-weight: 600; margin-bottom: 6px; }
  .popup-grid { display: grid; grid-template-columns: auto auto; gap: 2px 14px; font-size: 0.78rem; }
  .popup-label { color: #9ca3af; }
  .popup-val { color: #e0e0e0; font-weight: 500; }
  .overload { color: #f87171 !important; font-weight: 700; }
</style>
</head>
<body>
<div id="header">
  <h1>Bangalore 4G/5G NSA — Live Cell Map</h1>
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
  <button id="refresh-btn" onclick="fetchCells()">&#8635; Refresh</button>
</div>
<div id="map"></div>
<div id="statusbar">
  <span id="stat-total"></span>
  <span id="stat-5g"></span>
  <span id="stat-4g"></span>
  <span id="stat-overload"></span>
  <span id="stat-ues"></span>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const VENDOR_COLOR = { Nokia: '#60a5fa', Ericsson: '#4ade80', Samsung: '#a78bfa', ZTE: '#fb923c' };
const GEN_OPACITY  = { '5G': 0.92, '4G': 0.65 };

const map = L.map('map', { zoomControl: true, attributionControl: true }).setView([12.9716, 77.5946], 12);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap &copy; CARTO',
  subdomains: 'abcd', maxZoom: 18
}).addTo(map);

// Legend
const legend = L.control({ position: 'bottomright' });
legend.onAdd = () => {
  const d = L.DomUtil.create('div', 'legend');
  d.innerHTML = '<b style="color:#fff">Vendor</b><br>' +
    Object.entries(VENDOR_COLOR).map(([v,c]) =>
      '<span class="legend-dot" style="background:'+c+'"></span>'+v+'<br>').join('') +
    '<br><b style="color:#fff">Generation</b><br>' +
    '<span class="legend-dot" style="background:#888;opacity:0.95"></span>5G NR (solid)<br>' +
    '<span class="legend-dot" style="background:#888;opacity:0.6"></span>4G LTE (faded)<br>' +
    '<br><b style="color:#fff">Status</b><br>' +
    '<span class="legend-dot" style="background:#f87171"></span>Overloaded (PRB>85%)<br>' +
    '<span class="legend-dot" style="background:#fbbf24"></span>SINR low (<5 dB)';
  return d;
};
legend.addTo(map);

let markers = [];
let allCells = [];

function circleOpts(cell) {
  const kpi = cell.kpi || {};
  const prb  = kpi.prb_dl_pct || 0;
  const sinr = kpi.sinr_db != null ? kpi.sinr_db : 99;
  const is5g = cell.generation === '5G';
  let color = VENDOR_COLOR[cell.vendor] || '#888';
  let fillColor = color;
  if (prb > 85)   { fillColor = '#f87171'; }
  else if (sinr < 5) { fillColor = '#fbbf24'; }
  return {
    radius: is5g ? 420 : 280,
    color: fillColor,
    fillColor: fillColor,
    fillOpacity: is5g ? 0.55 : 0.35,
    weight: is5g ? 2.5 : 1.5,
    opacity: is5g ? 0.9 : 0.7,
  };
}

function popupHtml(cell) {
  const kpi = cell.kpi || {};
  const prb  = kpi.prb_dl_pct != null ? kpi.prb_dl_pct.toFixed(1) + '%' : '—';
  const sinr = kpi.sinr_db    != null ? kpi.sinr_db.toFixed(1) + ' dB' : '—';
  const ues  = kpi.connected_ues != null ? kpi.connected_ues : '—';
  const rsrp = kpi.rsrp_dbm   != null ? kpi.rsrp_dbm.toFixed(1) + ' dBm' : '—';
  const pwr  = kpi.power_w    != null ? kpi.power_w.toFixed(0) + ' W' : '—';
  const tput = kpi.dl_throughput_mbps != null ? kpi.dl_throughput_mbps.toFixed(0) + ' Mbps' : '—';
  const prb_num = kpi.prb_dl_pct || 0;
  const bcolor = VENDOR_COLOR[cell.vendor] || '#888';
  const genLabel = cell.generation === '5G'
    ? '<span class="popup-badge" style="background:#1e3a5f;color:#60a5fa">5G NR · '+cell.band+'</span>'
    : '<span class="popup-badge" style="background:#1e3a1e;color:#4ade80">4G LTE · '+cell.band+'</span>';
  return '<div class="popup-title">'+cell.id+'</div>'+
    genLabel+' '+
    '<span class="popup-badge" style="background:#2d2040;color:'+bcolor+'">'+cell.vendor+' · '+cell.hardware_model+'</span><br>'+
    '<div class="popup-grid">'+
    '<span class="popup-label">Area</span><span class="popup-val">'+cell.area+'</span>'+
    '<span class="popup-label">DU / CU</span><span class="popup-val">'+cell.du_id+' / '+cell.cu_id+'</span>'+
    '<span class="popup-label">PCI</span><span class="popup-val">'+cell.pci+'</span>'+
    '<span class="popup-label">Connected UEs</span><span class="popup-val">'+ues+'</span>'+
    '<span class="popup-label">PRB DL</span><span class="popup-val '+(prb_num>85?'overload':'')+'">'+prb+'</span>'+
    '<span class="popup-label">SINR</span><span class="popup-val '+(kpi.sinr_db<5?'overload':'')+'">'+sinr+'</span>'+
    '<span class="popup-label">RSRP</span><span class="popup-val">'+rsrp+'</span>'+
    '<span class="popup-label">Power</span><span class="popup-val">'+pwr+'</span>'+
    '<span class="popup-label">DL Throughput</span><span class="popup-val">'+tput+'</span>'+
    '</div>';
}

function filterVisible(cell) {
  const s5 = document.getElementById('show5g').checked;
  const s4 = document.getElementById('show4g').checked;
  const sN = document.getElementById('showNokia').checked;
  const sE = document.getElementById('showEricsson').checked;
  const sS = document.getElementById('showSamsung').checked;
  const sZ = document.getElementById('showZTE').checked;
  if (cell.generation === '5G' && !s5) return false;
  if (cell.generation === '4G' && !s4) return false;
  if (cell.vendor === 'Nokia'    && !sN) return false;
  if (cell.vendor === 'Ericsson' && !sE) return false;
  if (cell.vendor === 'Samsung'  && !sS) return false;
  if (cell.vendor === 'ZTE'      && !sZ) return false;
  return true;
}

function renderMarkers() {
  markers.forEach(m => map.removeLayer(m));
  markers = [];
  let overloadCount = 0;
  let totalUes = 0;
  let count5g = 0, count4g = 0;
  allCells.forEach(cell => {
    if (!cell.lat || !cell.lon) return;
    const prb = (cell.kpi || {}).prb_dl_pct || 0;
    if (prb > 85) overloadCount++;
    totalUes += (cell.kpi || {}).connected_ues || 0;
    if (cell.generation === '5G') count5g++; else count4g++;
    if (!filterVisible(cell)) return;
    const m = L.circle([cell.lat, cell.lon], circleOpts(cell))
      .bindPopup(popupHtml(cell), { maxWidth: 320 });
    m.addTo(map);
    markers.push(m);
  });
  document.getElementById('stat-total').textContent = allCells.length + ' cells total';
  document.getElementById('stat-5g').textContent    = count5g + ' × 5G NR';
  document.getElementById('stat-4g').textContent    = count4g + ' × 4G LTE';
  document.getElementById('stat-overload').textContent = overloadCount > 0 ? '⚠ ' + overloadCount + ' overloaded' : '✓ no overload';
  document.getElementById('stat-ues').textContent   = totalUes.toLocaleString() + ' connected UEs';
}

async function fetchCells() {
  try {
    document.getElementById('last-update').textContent = 'Refreshing…';
    const r = await fetch('/api/cells');
    const data = await r.json();
    allCells = data.cells || [];
    document.getElementById('cell-count').textContent = allCells.length + ' cells';
    document.getElementById('last-update').textContent = 'Updated ' + new Date().toLocaleTimeString();
    renderMarkers();
  } catch(e) {
    document.getElementById('last-update').textContent = 'Controller unreachable';
    document.getElementById('live-dot').style.color = '#f87171';
  }
}

// Filter checkboxes
['show5g','show4g','showNokia','showEricsson','showSamsung','showZTE'].forEach(id => {
  document.getElementById(id).addEventListener('change', renderMarkers);
});

fetchCells();
setInterval(fetchCells, 30000);
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
        r = httpx.get(f"{CONTROLLER_URL}/network", timeout=8.0)
        data = r.json()
    except Exception as e:
        log.warning("Controller unreachable: %s", e)
        return JSONResponse({"cells": [], "error": str(e)})

    cells = []
    for cell_id, c in data.get("cells", {}).items():
        cells.append({
            "id":             cell_id,
            "area":           c.get("area", ""),
            "lat":            c.get("lat"),
            "lon":            c.get("lon"),
            "vendor":         c.get("vendor", "unknown"),
            "hardware_model": c.get("hardware_model", ""),
            "generation":     c.get("generation", "5G"),
            "band":           c.get("band", ""),
            "pci":            c.get("pci"),
            "du_id":          c.get("du_id"),
            "cu_id":          c.get("cu_id"),
            "kpi":            c.get("kpi", {}),
        })
    return {"cells": cells, "total": len(cells)}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("map_server:app", host="0.0.0.0", port=8083, reload=False)
