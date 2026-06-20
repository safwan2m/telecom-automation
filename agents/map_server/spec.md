# Map Server — Specification

FastAPI service on port 8083. Serves the Leaflet.js live map UI and acts as an HTTP proxy to the orchestrator so the browser never makes cross-origin calls directly to port 8082.

## Internal flow

```
Browser  GET /
      └──► serves inline HTML (Leaflet.js + AI chat panel)

Browser  GET /api/cells  (every 30 s auto-refresh)
      └──► api_cells():
              ├─► GET /network on Controller  (all cells + live KPIs)
              └─► per cell:
                    ├─► compute_coverage_radius_m(band, tx_power_w, generation, antenna_config)
                    │     → model estimate r_model (COST-231-Hata)
                    │
                    ├─► if cell.kpi.coverage_radius_m exists:
                    │       live_r = cell.kpi.coverage_radius_m
                    │       if 0.5×r_model ≤ live_r ≤ 2.0×r_model:
                    │           use live_r  (KPI agent's coverage estimate)
                    │       else:
                    │           use r_model  (live value out of plausible range; ignore)
                    │   else:
                    │       use r_model
                    │
                    └─► attach coverage_radius_m to cell record → return

Browser  POST /api/chat   (AI chat panel in map UI)
      └──► proxy to orchestrator POST /chat (session_id = "map-<uuid>")
```

## Coverage radius computation

`compute_coverage_radius_m(band, tx_power_w, generation, antenna_config)`:

1. **RF efficiency** — fraction of tx_power_w that becomes radiated RF:
   - 5G: 22% (high-power PA losses)
   - 4G: 32%
2. **Antenna gain** — based on antenna_config string:
   - `"64T64R"` → 24.0 dBi (massive MIMO)
   - `"4T4R"` → 17.0 dBi
3. **EIRP** — `10 × log10(rf_w × 1000) + ant_gain_dbi` (dBm)
4. **COST-231-Hata parameters** per band:
   - `n78` / `n41`: `f=3500/2500 MHz`, `a_hm=3.20`, `C_cm=3.0` (large urban 5G)
   - `B3`: `f=1800 MHz`, `a_hm=1.45`, `C_cm=0.0`
   - `B40`: `f=2300 MHz`, `a_hm=1.93`, `C_cm=0.0`
5. **Path loss budget** — `PL_max = EIRP − receiver_sensitivity_dbm`
   - Receiver sensitivity defaults: 5G `−100 dBm`, 4G `−95 dBm`
6. **Invert COST-231-Hata** — solve `PL(d) = PL_max` for `d`:
   ```
   PL_cm = PL_max − 46.3 − 33.9×log10(f) + 13.82×log10(hbs) + a_hm − C_cm
   d_km = 10^(PL_cm / (44.9 − 6.55×log10(hbs)))
   ```
   clamp result to `[100 m, 8000 m]`.

The radius returned reflects the **hardware design point** (`tx_power_w` from topology.json), not the instantaneous live transmitted power (`kpi.power_w`). The live override path corrects this if the KPI agent has computed a tighter estimate (e.g., cell power-reduced for DTX).

## Map UI features

- **Leaflet.js** map centred on Malleswaram (`13.003, 77.570`, zoom 14)
- **Colour-coded markers** — outer ring = vendor colour; fill opacity = generation (`5G: 1.0`, `4G: 0.6`); red fill overlay if PRB > 85% or SINR < 5 dB
- **Click popup** — vendor, hardware model, generation, band, DU/CU assignment, live KPIs (UEs, PRB %, SINR dB, power W), coverage circle
- **Coverage circles** — drawn at computed `coverage_radius_m` per cell; same vendor colour; semi-transparent fill
- **Filter controls** — checkboxes for 5G/4G and all four vendors; updates map in-place without full reload
- **Status bar** — aggregate cell count, total UEs, overloaded cell count, last refresh timestamp
- **AI chat panel** (right side) — sends messages to `/api/chat`; renders streaming responses; built-in shortcuts `/status`, `/alerts`, `/cells`, `/plan`, `/son`, `/ue`
- **Auto-refresh** — polls `/api/cells` every 30 s; updates markers and coverage circles; no page reload

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CONTROLLER_URL` | `http://controller:8080` | `/api/cells` data source |
| `ORCHESTRATOR_URL` | `http://orchestrator:8082` | `/api/chat` proxy target |

## Routes

```
GET  /              serves the Leaflet.js map HTML (inline — no separate static files)
GET  /api/cells     cell array with coverage_radius_m for the map frontend
POST /api/chat      body: {"message", "session_id"}; proxied to orchestrator; streaming response
GET  /health
```
