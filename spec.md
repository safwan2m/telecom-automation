# Telecom Network Automation — Project Specification

## High-Level Idea

Build an AI agent system that accepts deployment parameters for a geographic area and automatically plans, configures, and deploys an O-RAN-compliant 5G network. The system is context-aware of the live deployment and continuously optimizes for power efficiency and operator profit.

---

## Input Parameters

| Parameter | Description | Example |
|---|---|---|
| `geographic_area` | City name or GeoJSON polygon | `"Bangalore"` |
| `expected_user_density` | Users per km² | `500` |
| `traffic_profile` | Slice mix + peak hour | `{"eMBB":0.7,"URLLC":0.2,"mMTC":0.1,"peak_hour":19}` |
| `fiber_availability` | Fiber map or site list | `["Koramangala","Whitefield",...]` |
| `spectrum_bands` | Licensed bands | `["n78","n28"]` |
| `latency_constraints` | E2E and fronthaul targets | `{"e2e_ms":10,"fronthaul_us":100}` |
| `compute_resources` | Per-site server capacity | `{"cpu_cores":32,"ram_gb":64}` |
| `deployment_budget` | CAPEX/OPEX envelope (USD) | `2000000` |

---

## Resolved Decisions

| Question | Decision |
|---|---|
| RAN hardware | Docker containers simulating RU/DU/CU (dev), real O-RAN targets (prod) |
| KPI data source | Synthetic telemetry from DU/CU simulators → InfluxDB |
| Geographic area | Bangalore — 14 cells across 9 areas |
| LLM backend | Claude API (claude-sonnet-4-6), configurable via `ANTHROPIC_API_KEY` |
| Deployment target | Docker Compose (dev), Kubernetes Helm (prod) |
| SMO | Controller REST API (dev), O-RAN-compliant SMO (prod) |

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Orchestrator Agent                          │
│           (Claude API + tool-calling, port 8082)                │
└────────────┬────────────┬─────────────┬────────────────────────┘
             │            │             │
    ┌────────▼──┐  ┌──────▼──────┐  ┌──▼──────────────┐
    │ Planning  │  │  Controller │  │   KPI Agent     │
    │   API     │  │     API     │  │  (background)   │
    │  :8081    │  │    :8080    │  │                 │
    └────────┬──┘  └──────┬──────┘  └──────┬──────────┘
             │            │                │
    ┌────────▼────────────▼────────────────▼──────────┐
    │                  InfluxDB :8086                  │
    │   cell_kpi | du_kpi | cu_kpi | core_kpi         │
    │   ue_mobility | ue_usage | alerts | topo_events  │
    └──────────────────────┬───────────────────────────┘
                           │  reads /config/topology.json
           ┌───────────────┼───────────────┐
     ┌─────▼─────┐   ┌─────▼─────┐  ┌─────▼─────┐
     │ DU-NORTH-1│   │ DU-EAST-1 │  │  CU-NORTH │  ...
     │ DU-NORTH-2│   │ DU-SOUTH-1│  │  CU-SOUTH │
     │ DU-CENTRAL│   │ DU-SOUTH-2│  └───────────┘
     └───────────┘   └───────────┘
```

---

## Agent Architecture

### Agent 1 — LLM Orchestrator (`agents/orchestrator/`)
- FastAPI service on port 8082
- POST `/chat` — natural language → tool calls → response (streaming)
- Tools: `query_network`, `query_cell`, `move_cell`, `move_du`, `plan_network`, `get_alerts`
- Injects live network state from Controller API into every system prompt

### Agent 2 — Controller / Core DB (`dev-env/simulators/controller/`)
- FastAPI service on port 8080 — single control plane
- GET `/network` — full topology + live KPIs merged
- GET `/cells`, `/dus`, `/cus` — component listings with KPIs
- POST `/move/cell`, `/move/du` — live topology changes (atomic write to topology.json)
- Topology changes propagate to DU/CU containers within 5 s via file-watch polling

### Agent 3 — Planning Engine (`planning/`)
- FastAPI service on port 8081
- POST `/plan` — takes input parameters, returns complete network plan
- Algorithms: heuristic cell placement, graph-coloring PCI planner, slice allocator, fronthaul/midhaul router
- POST `/plan/apply` — pushes accepted plan to Controller as topology update

### Agent 4 — KPI Monitoring & Optimization Agent (`agents/kpi_agent/`)
- Background process, no exposed port
- Polls InfluxDB every 30 s for cell/DU KPIs
- Detects: overload (PRB > 85%), underload (PRB < 20%), power waste, SINR degradation
- Actions: calls Controller `/move/cell` to balance load; writes `alerts` measurement to InfluxDB

---

## Dev Environment — Running Components

| Container | Image | Port | Status |
|---|---|---|---|
| `influxdb` | influxdb:2.7 | 8086 | ✅ running |
| `grafana` | grafana:10.4.0 | 3000 | ✅ running |
| `core-sim` | dev-env-core-sim | — | ✅ running |
| `cu-north` | dev-env-cu-north | — | ✅ running |
| `cu-south` | dev-env-cu-south | — | ✅ running |
| `du-north-1` | dev-env-du-north-1 | — | ✅ running |
| `du-north-2` | dev-env-du-north-2 | — | ✅ running |
| `du-central` | dev-env-du-central | — | ✅ running |
| `du-east-1` | dev-env-du-east-1 | — | ✅ running |
| `du-south-1` | dev-env-du-south-1 | — | ✅ running |
| `du-south-2` | dev-env-du-south-2 | — | ✅ running |
| `controller` | dev-env-controller | 8080 | ✅ running |
| `planning-api` | telecom-planning-api | 8081 | ✅ running |
| `kpi-agent` | telecom-kpi-agent | — | ✅ running |
| `orchestrator` | telecom-orchestrator | 8082 | ✅ running |

### InfluxDB Measurements

| Measurement | Tags | Key Fields |
|---|---|---|
| `cell_kpi` | cell_id, area, band, pci, du_id, cu_id | connected_ues, dl/ul_throughput_mbps, rsrp_dbm, sinr_db, power_w, prb_dl/ul_pct |
| `du_kpi` | du_id, cu_id | active_ues, cpu_pct, memory_pct, fronthaul_latency_us |
| `cu_kpi` | cu_id | rrc_connected, pdcp_dl/ul_gbps, f1/n2/n3_latency_ms |
| `core_kpi` | component, instance_id | registered_ues, active_sessions, throughput_gbps |
| `ue_mobility` | ue_id, source_cell, target_cell, event_type | rsrp_source/target, ho_duration_ms, velocity_kmh |
| `ue_usage` | ue_id, cell_id, slice_type | dl/ul_bytes, latency_ms, jitter_ms, packet_loss |
| `alerts` | severity, cell_id, du_id, alert_type | message, metric_value, threshold |
| `topology_event` | event_type | cell_id/du_id, from/to component |

---

## Task List (Due: 15 June 2026)

### Phase 1 — Foundation ✅ COMPLETE
- [x] Define data schema for Core DB (InfluxDB measurements + topology.json)
- [x] Build Core DB Agent / Controller API (GET/POST endpoints, topology CRUD)
- [x] Collect/mock Bangalore deployment dataset (14 cells, 9 areas, streaming every 10 s)
- [x] Deploy dev environment (12 containers: InfluxDB, Grafana, Core, 2×CU, 6×DU, Controller)

### Phase 2 — Planning Engine ✅ COMPLETE
- [x] Cell placement algorithm (density-weighted heuristic, haversine distance)
- [x] DU/CU grouping (geographic proximity, configurable max cells/DU and DUs/CU)
- [x] PCI planning (graph-coloring, collision and confusion free)
- [x] Slice allocation (PRB budget per slice from traffic profile)
- [x] Fronthaul/midhaul routing (distance-based latency estimate)
- [x] Planning FastAPI service on port 8081 with `/plan` and `/plan/apply` endpoints

### Phase 3 — Deployment Agent ✅ COMPLETE
- [x] Topology manifest generation from planning outputs (topology.json format)
- [x] Plan apply: pushes accepted plan to Controller (live topology update)
- [x] Health-check: Controller validates all DUs/CUs acknowledged new topology
- [ ] Helm/K8s manifest generation (prod target — post-demo)
- [ ] SMO northbound API registration (prod target — post-demo)

### Phase 4 — KPI Monitoring & Optimization Agent ✅ COMPLETE
- [x] KPI telemetry pipeline (InfluxDB, polled every 30 s)
- [x] Overload detection (PRB > 85%) with cell-move action via Controller
- [x] Underload / power-waste detection (PRB < 20%, flags sleep candidates)
- [x] SINR degradation alerting
- [x] Alert writes to InfluxDB `alerts` measurement
- [ ] Reinforcement learning-based power optimizer (future sprint)

### Phase 5 — Orchestrator Agent ✅ COMPLETE
- [x] LLM chat interface (FastAPI POST /chat, streaming responses)
- [x] Tool-calling: query_network, query_cell, move_cell, move_du, plan_network, get_alerts
- [x] Context injection: live network state prepended to every system prompt
- [x] Conversation history (per-session, in-memory)
- [ ] End-to-end integration test suite (in progress)

### Phase 6 — Testing & Demo
- [ ] Unit tests for planning algorithms (placement, PCI, slicing)
- [ ] Integration tests (orchestrator → planning → controller → DU reconfigures)
- [ ] Demo script: deploy Bangalore network from scratch via chat
- [ ] Deployment runbook

---

## Controller API Reference

```
GET  /health                     # system health
GET  /topology                   # raw topology.json
GET  /network                    # full state + live KPIs (use this for agent context)
GET  /cells?area=&du_id=&cu_id=  # filtered cell list with KPIs
GET  /cells/{cell_id}            # cell detail + 30 min KPI time series
GET  /dus                        # DU list with KPIs
GET  /cus                        # CU list with KPIs

POST /move/cell  {"cell_id":"...", "to_du_id":"..."}
POST /move/du    {"du_id":"...",   "to_cu_id":"..."}
```

## Planning API Reference

```
POST /plan        {geographic_area, user_density, traffic_profile,
                   spectrum_bands, latency_constraints,
                   compute_resources, deployment_budget}
POST /plan/apply  {"plan_id": "..."}   # push plan to Controller
GET  /plan/{id}   # retrieve a previously generated plan
```

## Orchestrator API Reference

```
POST /chat        {"message":"...", "session_id":"..."}  → streaming text
GET  /history     {"session_id":"..."}
DELETE /history   {"session_id":"..."}
GET  /tools       # list available tools
```

---

## Success Criteria

- Given valid input parameters, planning engine produces a conflict-free network plan in < 30 s.
- Plan apply propagates to all DU/CU containers within 10 s (topology.json polling).
- KPI agent detects and responds to overload within 2 polling cycles (60 s).
- Orchestrator correctly routes ≥ 90% of operator commands to the right tool in manual testing.
- All 14 Bangalore cells continuously stream data with zero gaps in the demo scenario.
