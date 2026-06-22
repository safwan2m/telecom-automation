# Map Server — Specification

FastAPI service on port 8083. Serves the Leaflet.js live map UI as a single inline HTML page and proxies all browser requests to the orchestrator so the browser never makes cross-origin calls directly to port 8082.

## Routes

```
GET    /                 Inline Leaflet.js HTML map page (no separate static files)
GET    /api/cells        Cell list + coverage_radius_m + live KPIs for the map frontend
POST   /api/chat         body: {"message", "session_id"} → proxy to Orchestrator /chat (120 s timeout)
GET    /api/history      ?session_id=  → proxy to Orchestrator /history
DELETE /api/history      ?session_id=  → proxy to Orchestrator /history
GET    /api/tools        proxy to Orchestrator /tools
GET    /api/orch-health  proxy to Orchestrator /health
GET    /health           → {"status": "ok"}
```

All proxy routes pass the raw response body and status code through unchanged.

## /api/cells — internal flow

```
GET /network on Controller  (all cells + live KPIs merged)
  └─► per cell:
        ├─► r_model = compute_coverage_radius_m(band, tx_power_w, generation, antenna_config)
        ├─► live_r  = cell.kpi.coverage_radius_m   (if present)
        │     if 0.5×r_model ≤ live_r ≤ 2.0×r_model → use live_r
        │     else → use r_model
        └─► return cell record + coverage_radius_m
```

Response: `{"cells": [...], "total": N}`. Each cell object includes:
`id`, `area`, `lat`, `lon`, `vendor`, `hardware_model`, `generation`, `band`, `pci`, `du_id`, `cu_id`, `coverage_radius_m`, `kpi` (forwarded verbatim from Controller).

If the Controller is unreachable the route returns `{"cells": [], "error": "<msg>"}`.

## Coverage radius computation

`compute_coverage_radius_m(band, tx_power_w, generation, antenna_config) → float (metres)`

**Band parameters table** — unknown bands fall back to n78 values:

| Band | freq_mhz | bw_mhz | pen_loss_db |
|------|----------|--------|-------------|
| n78  | 3500     | 100    | 20          |
| n41  | 2500     | 80     | 20          |
| n28  | 700      | 20     | 15          |
| B3   | 1800     | 20     | 18          |
| B40  | 2300     | 20     | 18          |

**Constants:**

| Symbol | Value | Note |
|--------|-------|------|
| rf_eff | 0.22 (5G) / 0.32 (4G) | PA radiated-RF fraction |
| ant_gain | 24.0 dBi (64T64R) / 17.0 dBi (4T4R) | default 17.0 for unknown configs |
| rf_eff default | 0.25 | for unknown generations |
| NF | 7 dB | UE noise figure |
| SNR_edge | −3 dB | coverage-edge SNR margin |
| hbs | 25 m | base station antenna height |
| C_cm | 3.0 dB | large-urban correction (all bands) |

**Formula (COST-231-Hata, urban macro):**

```
rf_w      = max(tx_power_w × rf_eff, 0.1)
eirp_dbm  = 10 × log10(rf_w × 1000) + ant_gain_dbi
noise_dbm = −174 + 10 × log10(bw_mhz × 1e6) + NF
pl_max    = eirp_dbm − (noise_dbm − SNR_edge) − pen_loss_db
A         = 46.3 + 33.9×log10(freq_mhz) − 13.82×log10(hbs) + C_cm
B         = 44.9 − 6.55×log10(hbs)
d_m       = 10^((pl_max − A) / B) × 1000          (returned, rounded to 1 decimal)
```

`tx_power_w` is taken from `topology.json` (the hardware design point), not `kpi.power_w`. The live-KPI override path in `/api/cells` corrects for power-reduced states (e.g., DTX).

## Map UI

Single-page HTML rendered by `GET /`. No separate static directory — all CSS, JS, and HTML are inlined.

### Layout

```
┌─────────────────────────────────────────────────────┐
│ Header bar: title · LIVE badge · cell count · time  │
├─────────────────────────────────────────────────────┤
│ Controls: 5G/4G checkboxes · vendor checkboxes      │
│           Coverage circles checkbox · Refresh btn   │
├────────────────────────────────┬────────────────────┤
│                                │  AI Chat Panel     │
│        Leaflet.js map          │  (collapsible,     │
│        (dark CartoDB tiles)    │   380 px wide)     │
│                                │                    │
├────────────────────────────────┴────────────────────┤
│ Status bar: cell count · 5G count · 4G count        │
│             overload count · total UEs              │
└─────────────────────────────────────────────────────┘
```

### Map rendering

Centre: `[13.000, 77.570]`, zoom 14. Dark CartoDB tile layer.

**Vendor colours:**

| Vendor   | Hex colour |
|----------|------------|
| Nokia    | `#60a5fa`  |
| Ericsson | `#4ade80`  |
| Samsung  | `#a78bfa`  |
| ZTE      | `#fb923c`  |

**Status colour** (per cell, used for dot fill and coverage circle fill):
- PRB DL > 85 % → `#f87171` (red)
- SINR < 5 dB → `#fbbf24` (amber)
- Otherwise → vendor colour

**Per-cell layers** (both bind the same popup):

1. **Coverage circle** — radius = `coverage_radius_m`; border colour = vendor colour; fill colour = status colour; `fillOpacity 0.07`, `weight 1.5`, `opacity 0.45`; 4G: `dashArray "6 4"`, 5G: solid. Hidden when Coverage circles checkbox is unchecked.

2. **Site dot** — fixed radius 80 m; colour = status colour; `fillOpacity 0.90` (5G) / `0.55` (4G), `weight 2.5`. Always shown if cell passes filter.

**Filter checkboxes:** 5G NR, 4G LTE, Nokia, Ericsson, Samsung, ZTE. Toggling any checkbox calls `renderMarkers()` in-place (no network request).

**Bottom-right legend** (Leaflet control): vendor colour swatches, generation opacity key, coverage circle style, status fill key.

### Click popup fields

```
Cell ID (title)
Generation badge (5G NR · <band>  or  4G LTE · <band>)
Vendor badge (vendor · hardware_model)
Area | DU / CU | PCI | Coverage radius
Connected UEs | PRB DL % | SINR dB | RSRP dBm | Power W | DL Throughput Mbps
```

PRB and SINR values are highlighted red when over the same thresholds as the status colour. Missing KPI fields render as `—`.

### Auto-refresh

`fetchCells()` is called on load and every 30 s via `setInterval`. Updates `allCells` and re-renders all map layers without a page reload. Sets LIVE badge green on success, red on Controller unreachable.

## AI Chat Panel

Right-side panel, 380 px wide, collapsible to 36 px (arrow toggle; Leaflet `invalidateSize()` called after CSS transition).

**Session ID:** `map-<7 random alphanumeric chars>`, generated once on page load.

**Orchestrator health check:** `GET /api/orch-health` on load and every 30 s. Displays `model: <name>` from the response JSON when online; shows offline in red otherwise.

**Shortcut buttons** — clicking sends the expanded message to the orchestrator:

| Button    | Expanded message sent to orchestrator |
|-----------|---------------------------------------|
| `/status` | `What is the current status of all cells, DUs, and CUs? Summarise in a table.` |
| `/alerts` | `Show me all recent KPI alerts from the last 60 minutes.` |
| `/cells`  | `List all cells with their current connected UEs, PRB utilisation, and which DU they belong to.` |
| `/son`    | `Show me the SON agent status: what autonomous actions has it taken in the last hour, and are there any active anomalies?` |
| `/ue`     | `Give me a summary of UE usage patterns: which slices are most active, what are the average latencies, and how many handovers have occurred?` |
| `/plan`   | `I want to plan a network deployment. Ask me for all the required parameters before proceeding.` |
| `/history` | *(local — see below)* |
| `/tools`  | *(local — see below)* |
| `/clear`  | *(local — see below)* |

**Local commands** (handled client-side, never sent to the orchestrator):

- `/history` — `GET /api/history?session_id=<id>`; renders each message in the chat view (truncated to 600 chars).
- `/clear` — `DELETE /api/history?session_id=<id>`; clears the messages div.
- `/tools` — `GET /api/tools`; renders a bullet list of `name — description` (first 80 chars).

**Input:** `<textarea>` with auto-height (max 96 px). Enter sends; Shift+Enter inserts newline. Send button disabled while a request is in flight.

**Response rendering:** agent messages run through a lightweight markdown renderer (bold `**`, italic `*`, inline code `` ` ``, bullet lists, GitHub-style pipe tables). User messages rendered as plain text.

**Typing indicator:** three-dot bounce animation shown from request start until response arrives.

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `CONTROLLER_URL` | `http://controller:8080` | `/api/cells` data source |
| `ORCHESTRATOR_URL` | `http://orchestrator:8082` | all `/api/*` proxy targets |
