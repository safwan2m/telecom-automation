# STATUS.md — Project Status (volatile)

Tracks progress, open gaps, and acceptance criteria. Design and reference content
live in [spec.md](spec.md).

## Deadline

All tasks below due **15 June 2026**.

## Task List

### Phase 0 — Digital Twin & Dataset ✅ COMPLETE
- [x] **DU simulator extended** — 7 new KPI fields: rsrq_db, cqi, mcs, bler_pct, latency_ms, jitter_ms, interference_dbm (physics-based, correlated with SINR/load)
- [x] **Day-of-week traffic variation** — `WEEKEND_FACTOR = 0.75`; Saturday/Sunday load scaled down in `load_factor()`
- [x] **`dataset_generator.py`** — standalone script; 50,400-row CSV (70 days × 24 h × 30 cells); 32 columns; realistic class distribution (70% NORMAL / 15% OVERLOAD / 8% UNDERLOAD / 5% SINR_LOW / 2% POWER_WASTE); CLI: `--days`, `--seed`, `--out`
- [x] KPI values grounded in 4 reference Kaggle datasets (suraj520/cellular-network-performance-data, srikumarnayak/5g-network-kpi-dataset, praveenaparimi/telecom-network-dataset, suraj520/cellular-network-analysis-dataset)

### Phase 1 — Foundation ✅ COMPLETE
- [x] Define data schema (InfluxDB measurements + topology.json with vendor/hardware metadata)
- [x] Build Controller Agent (GET/POST endpoints, atomic topology CRUD)
- [x] 30-cell Malleswaram deployment: 10 macro sites × 3 sectors; Nokia/Ericsson/Samsung/ZTE; active_ues_peak 18,400; streaming every 10 s
  - High-traffic sites (RWS, 18C, SNK, SPG, 10C): 5G n78 3500 MHz + 5G n41 2500 MHz + 4G B3 1800 MHz
  - Residential sites (BEL, 3MN, MGR, CHD, 6CR): 5G n78 3500 MHz + 4G B40 2300 MHz + 4G B3 1800 MHz
  - 700 MHz (n28) excluded: 8.4 km coverage radius extends beyond Malleswaram to Peenya
- [x] Deploy dev environment (12 containers: InfluxDB, Grafana, Core, 1×CU, 3×DU, Controller, Planning, KPI, Orchestrator, Map)

### Phase 2 — Planning Engine ✅ COMPLETE + Extended
- [x] Cell placement algorithm (density-weighted heuristic, Haversine distance)
- [x] DU/CU grouping (geographic proximity, configurable max cells/DU and DUs/CU)
- [x] PCI planning (graph-coloring, collision and confusion free)
- [x] Slice allocation (PRB budget per slice from traffic profile)
- [x] Fronthaul/midhaul routing (distance-based latency estimate)
- [x] Planning FastAPI on port 8081 with `/plan` and `/plan/apply` endpoints
- [x] **COST-231 Walfisch-Ikegami NLOS propagation model** (`mip_placer.py`)
- [x] **MIP-based optimal placement** (Almoghathawi et al. 2024) — CAPEX+OPEX min subject to coverage, capacity, SINR; CBC via pulp
- [x] **Multi-period planning** — Case A (phased rollout) and Case B (event/diurnal shift); BS reuse across periods
- [x] **Demand node concept** — 10 Bangalore demand clusters separating demand from candidates
- [x] **SINR quality constraint at planning time** — linearised constraint 8
- [x] **Installation vs. operational cost split** — CAPEX (c_jt) vs. OPEX (r_jt)
- [x] `/plan/multi-period` endpoint; `use_mip` flag; `plan_network_multi_period` tool

### Phase 3 — Deployment Agent ✅ COMPLETE
- [x] Topology manifest generation from planning outputs (topology.json format)
- [x] Health-check: Controller validates DU/CU acknowledgement via topology polling
- [x] **`POST /topology/replace`** added to Controller — `plan/apply` deploys live
- [x] **`plan_to_topology()` fixed** — vendor/hardware/generation/antenna/power propagated and preserved
- [x] **`POST /cells/add`** — conversational single-cell deployment (`add_cell`)
- [x] **`DELETE /cells/{id}`** — remove a cell (`remove_cell`)
- [x] **`GET /neighbors/{cell_id}`** — geographic neighbor lookup for SON ANR
- [ ] Helm/K8s manifest generation (prod — post-demo)
- [ ] SMO northbound API registration (prod — post-demo)

### Phase 4 — KPI Monitoring & Optimization Agent ✅ COMPLETE
- [x] KPI telemetry pipeline (InfluxDB)
- [x] Bidirectional LSTM anomaly classifier: NORMAL / OVERLOAD / UNDERLOAD / SINR_LOW / POWER_WASTE
- [x] **9-feature BiLSTM** — added cqi, bler_pct, latency_ms
- [x] **Realistic training distribution** + WeightedRandomSampler; separate 4G/5G specs
- [x] Overload → auto cell-move + `LOAD_BALANCE`; 3-cycle cooldown
- [x] Underload → `TRAFFIC_STEER`
- [x] SINR degradation → `PCI_REOPT_REQUEST` + best-effort `/son/pci-reopt` call
- [x] Power waste → `DTX_RECOMMEND`
- [x] Rule-based fallback; AI inference with confidence gate
- [x] Alert + SON action writes with `ai_confidence` / `confidence`
- [ ] Reinforcement learning-based power optimizer (future sprint)

### Phase 5 — Orchestrator Agent ✅ COMPLETE
- [x] LLM chat interface (streaming via sync generator + StreamingResponse)
- [x] Tool-calling loop; all 13 tools wired; results JSON-sanitised
- [x] Anthropic→Gemini tool schema translation (`_clean_params`)
- [x] Context injection (`build_network_context()` per request)
- [x] Per-session in-memory history
- [x] `chat.py` CLI client with shortcuts and named sessions
- [x] **4 new tools** — `query_ue`, `get_son_status`, `add_cell`, `remove_cell`
- [ ] End-to-end integration test suite

### Phase 6 — Map Visualization & Dashboards ✅ COMPLETE
- [x] Leaflet.js live cell map (port 8083)
- [x] Colour-coded markers (vendor + 5G/4G opacity + status)
- [x] Click popup (vendor, hardware, band, DU/CU, KPIs)
- [x] Filter controls (generation, vendor)
- [x] Auto-refresh 30 s; aggregate status bar
- [x] AI chat panel in map UI; shortcuts `/status`, `/alerts`, `/cells`, `/plan`, `/son`, `/ue`
- [x] **5 Grafana dashboards** provisioned via `grafana/provisioning/dashboards/default.yaml`:
  - `network_overview.json`, `cell_kpi.json`, `ue_analytics.json`, `son_alerts.json`, `du_cu_performance.json`

### Phase 7 — Testing & Demo
- [ ] Unit tests for planning algorithms (placement, PCI, slicing)
- [ ] Integration tests (orchestrator → planning → controller → DU reconfigures)
- [ ] Demo script: deploy Bangalore network from scratch via chat
- [ ] Deployment runbook

### Future / Outsource
- [ ] Replace the custom tool-schema layer in `tools.py` with an MCP server so any MCP-compatible LLM (Claude, Gemini, GPT-4o) can discover and call the orchestrator tools without per-provider translation.

## Open Discrepancies & Gaps

Verified against code on the current branch. (The earlier "Known Gaps" table was
stale — 6 of its 7 entries were already complete and have been removed.)

| Item | Location | Impact |
|---|---|---|
| `/son/pci-reopt` has no Controller route | KPI agent calls it (`kpi_agent.py:210,329`); Controller has no such endpoint | SINR_LOW SON call always 404s; `PCI_REOPT_REQUEST` is still logged, so PCI re-opt never executes |
| Tool count mismatch | `tools.py` defines 13; `GET /tools` doc/handler cites 9 (`spec` Orchestrator ref legacy) | Cosmetic/doc — confirm `GET /tools` returns all 13 |
| `n28` vestigial | Excluded from deployment (§5) yet present in `_BAND_PARAMS`, `_SINR_BASE`, `_RSRP_BASE`, and a `PlanRequest` default `spectrum_bands=["n78","n28"]` | Decide: "supported but unused" vs remove; make all sites agree |
| Phase 7 acceptance criteria undefined | this file | Unit/integration/demo/runbook scope not specified |
| RACH procedures not modelled | `du_simulator.py` | UEs jump pool→connected with no preamble/RAR/MSG3/MSG4 contention modelling |

## Success Criteria

- Planning engine produces a conflict-free plan in < 30 s.
- Plan apply propagates to all DU/CU containers within 10 s.
- KPI agent detects and responds to overload within 2 polling cycles (60 s).
- Orchestrator correctly routes ≥ 90% of operator commands in manual testing.
- All 30 cells stream data with zero gaps in the demo scenario.
- Map page loads and renders all 30 cells with live KPIs within 5 s of controller startup.
