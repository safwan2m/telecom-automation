# Telecom Network Automation — Project Specification

## High-Level Idea

Build an AI agent system that accepts deployment parameters for a specific geographic area in Bangalore and automatically plans, configures, and deploys, or modifies an O-RAN-compliant 4G/5G network. The system is context-aware of the live deployment and continuously optimizes for power efficiency and operator profit.

A live map container shows the current Bangalore cell layout with per-cell KPI overlays, colour-coded by vendor and generation. The RAN is a 4G/5G NSA mix — both 4G LTE and 5G NR connect to the same NSA core (shared AMF/SMF/UPF). 5G NR deployment uses split CU/DU/RU architecture throughout; the planning engine decides cell placement and DU/CU grouping. UE counts and hardware limits are calibrated to real market equipment specs.

The KPI agent, controller, or planning engine can reorganise cells at any time in response to changing load or operator instruction. Input may be partial — the system will request missing fields or flag redundant ones.

---

## Input Parameters

| Parameter | Description | Example |
|---|---|---|
| `hardware_resources` | Radio hardware model + capability | `Nokia AirScale MAA 64T64R` |
| `geographic_area` | City zone or GeoJSON polygon | `"ITPL & EPIP zone, Whitefield"` |
| `expected_user_density` | Users per km² | `50000` |
| `traffic_profile` | Slice mix + peak hour | `{"eMBB":0.7,"URLLC":0.2,"mMTC":0.1,"peak_hour":19}` |
| `fiber_availability` | Fiber map or site list | `["Koramangala","Whitefield",...]` |
| `spectrum_bands` | Licensed bands | `["n78","n28","B3","B40"]` |
| `latency_constraints` | E2E and fronthaul targets | `{"e2e_ms":10,"fronthaul_us":100}` |
| `compute_resources` | Per-site server capacity | `{"cpu_cores":32,"ram_gb":64}` |
| `deployment_budget` | CAPEX/OPEX envelope (USD) | `2000000` |

---

## Resolved Decisions

| Question | Decision |
|---|---|
| RAN hardware | Docker containers simulating RU/DU/CU (dev); Nokia, Ericsson, Samsung, ZTE equipment specs (25% each); real O-RAN targets (prod) |
| KPI data source | Synthetic telemetry from DU/CU/Core simulators using real hardware specs (peak_dl_mbps, tx_power_w, band-specific SINR/RSRP) → InfluxDB |
| Geographic area | Bangalore — 48 cells across 18 zones; scaled to ~25% operator market share of 13 M city population |
| LLM backend | Gemini API (gemini-2.5-flash), configurable via `GOOGLE_API_KEY` |
| RAN mode | 4G/5G NSA — LTE anchor + 5G NR secondary; shared AMF/SMF/UPF core |
| 5G architecture | Split CU/DU/RU throughout (planning engine groups DUs under CUs by proximity) |
| Deployment target | Docker Compose (dev), Kubernetes Helm (prod) |
| SMO | Controller REST API (dev), O-RAN-compliant SMO (prod) |
| Live map | Leaflet.js map container (port 8083) showing all cells, vendor colours, live KPI status |

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────┐
│               Orchestrator Agent  :8082                      │
│          Gemini 2.5 Flash  +  tool-calling (streaming)       │
└────────┬───────────────┬───────────────┬─────────────────────┘
         │               │               │
  ┌──────▼──────┐  ┌─────▼──────┐  ┌────▼──────────┐
  │ Planning    │  │ Controller │  │  KPI Agent    │
  │ Agent :8081 │  │ Agent :8080│  │  (background) │
  │ /plan       │  │ /network   │  │  BiLSTM model │
  │ /plan/apply │  │ /move/cell │  │  + alerting   │
  └──────┬──────┘  └─────┬──────┘  └────┬──────────┘
         │               │              │
  ┌──────▼───────────────▼──────────────▼──────────┐
  │                 InfluxDB  :8086                 │
  │  cell_kpi | du_kpi | cu_kpi | core_kpi         │
  │  ue_mobility | ue_usage | alerts | topo_events  │
  └──────────────────────┬─────────────────────────┘
                         │  topology.json
           ┌─────────────┼───────────────────┐
     ┌─────▼──────┐ ┌────▼──────┐ ┌──────────▼────┐
     │ 14× DU sims│ │ 4× CU sims│ │  Core sim     │
     │ (4G+5G RAN)│ │(RRC/PDCP) │ │  AMF/SMF/UPF  │
     └────────────┘ └───────────┘ └───────────────┘
                         │
                  ┌──────▼──────┐
                  │ Map Server  │
                  │   :8083     │
                  │ Leaflet.js  │
                  └─────────────┘
```

### Network Topology

| CU | DUs | Areas |
|---|---|---|
| CU-NORTH | DU-NORTH-1/2/3/4 | Hebbal, Yelahanka, Sadashivanagar, Yeshwanthpur, Rajajinagar, Vijayanagar |
| CU-CENTRAL | DU-CENTRAL-1/2/3 | MG Road, Indiranagar, Koramangala |
| CU-EAST | DU-EAST-1/2/3/4 | Whitefield, Marathahalli, KR Puram, Bellandur |
| CU-SOUTH | DU-SOUTH-1/2/3 | Electronic City, Jayanagar, BTM Layout, Banashankari, JP Nagar |

### Vendor Distribution (25% each — 12 cells per vendor)

| Vendor | 5G Hardware | 4G Hardware | 5G Max UEs | 5G Peak DL | System Power |
|---|---|---|---|---|---|
| Nokia | AirScale MAA 64T64R | AWHFA | 800 | 3800 Mbps | 1000 W |
| Ericsson | AIR 6449 / AIR 3221 | RBS 6402 | 750 | 3600 Mbps | 950 W |
| Samsung | TM500 64T64R | RRU | 700 | 3400 Mbps | 900 W |
| ZTE | AAU 5614 | RRU | 680 | 3200 Mbps | 1000 W |

---

## Agent Architecture

### Agent 1 — LLM Orchestrator (`agents/orchestrator/`)
- FastAPI on port 8082
- `POST /chat` — natural language → Gemini tool-calling → streaming response
- Tools: `query_network`, `list_cells`, `query_cell`, `move_cell`, `move_du`, `plan_network`, `apply_plan`, `get_alerts`
- Injects live network snapshot from Controller into every system prompt

### Agent 2 — Controller (`agents/controller/`)
- FastAPI on port 8080 — single control plane / source of truth
- `GET /network` — full topology + live KPIs merged in one response
- `GET /cells`, `/dus`, `/cus` — component listings with KPIs
- `POST /move/cell`, `/move/du` — live topology changes (atomic write to topology.json)
- Topology changes propagate to DU/CU simulators within 5 s via file-watch polling

### Agent 3 — Planning Engine (`agents/planning/`)
- FastAPI on port 8081
- `POST /plan` — takes deployment parameters, returns complete network plan
- Algorithms: density-weighted cell placement (Haversine), graph-coloring PCI planner, PRB slice allocator, distance-based fronthaul/midhaul router
- `POST /plan/apply` — pushes accepted plan to Controller as topology update

### Agent 4 — KPI Monitoring Agent (`agents/kpi_agent/`)
- Background process (no exposed port)
- Polls InfluxDB every 30 s; maintains 6-step (60 s) sliding window per cell
- Bidirectional LSTM classifier (5 classes): NORMAL, OVERLOAD, UNDERLOAD, SINR_LOW, POWER_WASTE
- Rule-based fallback for first 60 s while window fills; then AI inference with 70% confidence gate
- Actions: calls `/move/cell` for overload rebalancing; writes `alerts` to InfluxDB

### Agent 5 — Map Server (`agents/map_server/`)
- FastAPI on port 8083
- Serves a Leaflet.js interactive map of all 48 Bangalore cells
- `GET /` — HTML map page (auto-refreshes every 30 s)
- `GET /api/cells` — proxies Controller `/network`, returns GeoJSON-ready cell list
- Colour-coded by vendor (Nokia=blue, Ericsson=green, Samsung=purple, ZTE=orange)
- Visual status: solid = 5G NR, faded = 4G LTE; red fill = overloaded (PRB > 85%), amber = SINR < 5 dB
- Click popup: vendor, hardware model, band, DU/CU, PCI, connected UEs, PRB, SINR, RSRP, power, throughput

---

## Dev Environment — Running Containers

| Container | Port | Purpose |
|---|---|---|
| `influxdb` | 8086 | Time-series KPI storage |
| `grafana` | 3000 | Dashboards |
| `core-sim` | — | AMF + SMF + UPF simulator |
| `cu-north` | — | CU-NORTH simulator |
| `cu-central` | — | CU-CENTRAL simulator |
| `cu-east` | — | CU-EAST simulator |
| `cu-south` | — | CU-SOUTH simulator |
| `du-north-1/2/3/4` | — | DU simulators (Hebbal, Yelahanka, Sadashivanagar/YPR, Rajajinagar, Vijayanagar) |
| `du-central-1/2/3` | — | DU simulators (MG Road, Indiranagar, Koramangala) |
| `du-east-1/2/3/4` | — | DU simulators (Whitefield, Marathahalli, KR Puram, Bellandur) |
| `du-south-1/2/3` | — | DU simulators (Electronic City, Jayanagar/BTM, Banashankari/JP Nagar) |
| `controller` | 8080 | Topology control plane |
| `planning-api` | 8081 | Network planning engine |
| `kpi-agent` | — | KPI monitoring + BiLSTM anomaly detection |
| `orchestrator` | 8082 | Gemini LLM chat agent |
| `map-server` | 8083 | Leaflet.js live cell map |

**Total: 26 containers**

### InfluxDB Measurements

| Measurement | Tags | Key Fields |
|---|---|---|
| `cell_kpi` | cell_id, area, band, pci, du_id, cu_id, vendor, generation | connected_ues, dl/ul_throughput_mbps, rsrp_dbm, sinr_db, power_w, prb_dl/ul_pct, packet_loss_pct |
| `du_kpi` | du_id, cu_id | active_ues, cell_count, cpu_pct, memory_pct, fronthaul_latency_us |
| `cu_kpi` | cu_id | rrc_connected, rrc_idle, pdcp_dl/ul_gbps, f1/n2/n3/e1_latency_ms |
| `core_kpi` | component, instance_id | registered_ues, active_sessions, throughput_gbps |
| `ue_mobility` | ue_id, source_cell, target_cell, event_type | rsrp_source/target, ho_duration_ms, velocity_kmh |
| `ue_usage` | ue_id, cell_id, slice_type | dl/ul_bytes, latency_ms, jitter_ms, packet_loss |
| `alerts` | severity, cell_id, du_id, alert_type | message, metric_value, threshold, ai_confidence |
| `topology_event` | event_type | cell_id/du_id, from/to component |

---

## Task List (Due: 15 June 2026)

### Phase 1 — Foundation ✅ COMPLETE
- [x] Define data schema (InfluxDB measurements + topology.json with vendor/hardware metadata)
- [x] Build Controller Agent (GET/POST endpoints, atomic topology CRUD)
- [x] 48-cell Bangalore dataset: 18 zones, 4G/5G mixed, Nokia/Ericsson/Samsung/ZTE 25% each, streaming every 10 s
- [x] Deploy dev environment (26 containers: InfluxDB, Grafana, Core, 4×CU, 14×DU, Controller, Planning, KPI, Orchestrator, Map)

### Phase 2 — Planning Engine ✅ COMPLETE
- [x] Cell placement algorithm (density-weighted heuristic, Haversine distance)
- [x] DU/CU grouping (geographic proximity, configurable max cells/DU and DUs/CU)
- [x] PCI planning (graph-coloring, collision and confusion free)
- [x] Slice allocation (PRB budget per slice from traffic profile)
- [x] Fronthaul/midhaul routing (distance-based latency estimate)
- [x] Planning FastAPI on port 8081 with `/plan` and `/plan/apply` endpoints

### Phase 3 — Deployment Agent ✅ COMPLETE
- [x] Topology manifest generation from planning outputs (topology.json format)
- [x] Plan apply: pushes accepted plan to Controller (live topology update)
- [x] Health-check: Controller validates DU/CU acknowledgement via topology polling
- [ ] Helm/K8s manifest generation (prod — post-demo)
- [ ] SMO northbound API registration (prod — post-demo)

### Phase 4 — KPI Monitoring & Optimization Agent ✅ COMPLETE
- [x] KPI telemetry pipeline (InfluxDB, polled every 30 s)
- [x] Bidirectional LSTM anomaly classifier: NORMAL / OVERLOAD / UNDERLOAD / SINR_LOW / POWER_WASTE
- [x] Overload detection (PRB > 85%) with automatic cell-move action
- [x] Underload / power-waste detection with INFO/WARNING alerts
- [x] SINR degradation CRITICAL alerting
- [x] Rule-based fallback for first 60 s; AI inference thereafter with confidence gate
- [x] Alert writes to InfluxDB `alerts` measurement with `ai_confidence` field
- [ ] Reinforcement learning-based power optimizer (future sprint)

### Phase 5 — Orchestrator Agent ✅ COMPLETE
- [x] LLM chat interface (FastAPI POST /chat, streaming)
- [x] Tool-calling: query_network, list_cells, query_cell, move_cell, move_du, plan_network, apply_plan, get_alerts
- [x] Context injection: live network state prepended to every system prompt
- [x] Conversation history (per-session, in-memory)
- [ ] End-to-end integration test suite

### Phase 6 — Map Visualization ✅ COMPLETE
- [x] Leaflet.js live cell map (port 8083)
- [x] Colour-coded markers: vendor colour + 5G/4G opacity + status (overloaded/SINR low)
- [x] Click popup: vendor, hardware, band, DU/CU, KPIs
- [x] Filter controls: show/hide by generation and vendor
- [x] Auto-refresh every 30 s; status bar with aggregate counts

### Phase 7 — Testing & Demo
- [ ] Unit tests for planning algorithms (placement, PCI, slicing)
- [ ] Integration tests (orchestrator → planning → controller → DU reconfigures)
- [ ] Demo script: deploy Bangalore network from scratch via chat
- [ ] Deployment runbook

---

## Controller API Reference

```
GET  /health
GET  /topology                           raw topology.json
GET  /network                            full state + live KPIs
GET  /cells?area=&du_id=&cu_id=          filtered cell list with KPIs
GET  /cells/{cell_id}                    cell detail + 30-min KPI time series
GET  /dus
GET  /cus

POST /move/cell  {"cell_id":"...", "to_du_id":"..."}
POST /move/du    {"du_id":"...",   "to_cu_id":"..."}
```

## Planning API Reference

```
POST /plan        {geographic_area, expected_user_density, traffic_profile,
                   spectrum_bands, latency_constraints,
                   compute_resources, deployment_budget}
POST /plan/apply  {"plan_id": "..."}
GET  /plan/{id}
GET  /health
```

## Orchestrator API Reference

```
POST /chat        {"message":"...", "session_id":"..."}  → streaming text
GET  /history?session_id=default
DELETE /history?session_id=default
GET  /tools
GET  /health
```

## Map Server API Reference

```
GET  /            Leaflet map HTML page (auto-refresh 30 s)
GET  /api/cells   Cell list + live KPIs (JSON)
GET  /health
```

---

## Success Criteria

- Planning engine produces a conflict-free plan in < 30 s.
- Plan apply propagates to all DU/CU containers within 10 s.
- KPI agent detects and responds to overload within 2 polling cycles (60 s).
- Orchestrator correctly routes ≥ 90% of operator commands in manual testing.
- All 48 cells stream data with zero gaps in the demo scenario.
- Map page loads and renders all 48 cells with live KPIs within 5 s of controller startup.
