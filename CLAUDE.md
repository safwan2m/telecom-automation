# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI agent system for automated 4G/5G NSA network management over a simulated Malleswaram (North Bangalore) deployment (30 cells, 3 DUs, 1 CU, 18,400 peak UEs). The system plans, monitors, and reconfigures the RAN in real time via a Gemini-powered LLM orchestrator.

See `spec.md` for full specification and `prerequisites.md` for background knowledge.

## Running the Dev Environment

All services run via Docker Compose. A `.env` file in `dev-env/` is required with:

```
INFLUXDB_ADMIN_USER=admin
INFLUXDB_ADMIN_PASSWORD=...
INFLUXDB_ORG=telecom
INFLUXDB_BUCKET=telecom_metrics
INFLUXDB_TOKEN=telecom-super-secret-auth-token-2026
GRAFANA_PASSWORD=...
GOOGLE_API_KEY=...        # Gemini API key
```

```bash
cd dev-env
docker compose up --build          # start everything
docker compose down -v             # stop and wipe volumes
docker compose logs -f kpi-agent   # tail a specific service
```

**Chat CLI** (after `docker compose up`):

```bash
py chat.py                          # connects to localhost:8082
py chat.py --url http://... --session ops
```

## Service Map

| Service | Port | Source |
|---|---|---|
| Controller | 8080 | `agents/controller/controller.py` |
| Planning API | 8081 | `agents/planning/planner_api.py` |
| Orchestrator | 8082 | `agents/orchestrator/orchestrator.py` |
| Map Server | 8083 | `agents/map_server/map_server.py` |
| InfluxDB | 8086 | Docker image |
| Grafana | 3000 | Docker image |
| KPI Agent | — | `agents/kpi_agent/kpi_agent.py` (no HTTP port) |
| DU simulators × 3 | — | `dev-env/simulators/du/du_simulator.py` |
| CU simulator × 1 | — | `dev-env/simulators/cu/cu_simulator.py` |
| Core simulator | — | `dev-env/simulators/core/core_simulator.py` |

## Architecture

### Data flows

```
DU/CU simulators ──push KPIs──► InfluxDB ◄──query── Controller, KPI Agent, Orchestrator
Controller ──write──► /config/topology.json ◄──poll (5 s)── DU, CU simulators
KPI Agent ──auto-rebalance──► Controller /move/cell
LLM (Gemini) ──tool calls──► Orchestrator tools ──HTTP──► Controller / Planning API / InfluxDB
```

### Topology change mechanism

`dev-env/config/topology.json` is the single source of truth for cell↔DU↔CU assignments. The Controller writes it atomically (write to `.tmp`, then rename). All DU and CU simulators poll it every `TOPO_POLL_SEC` (default 5 s) and reconfigure themselves live without restart.

### Orchestrator / LLM

Uses **Google Gemini** (`gemini-2.5-flash` in Docker; configurable via `GEMINI_MODEL`). Tool schemas are defined in Anthropic-style JSON in `tools.py` and translated to Gemini `function_declarations` at startup. Available tools: `query_network`, `list_cells`, `query_cell`, `move_cell`, `move_du`, `plan_network`, `apply_plan`, `get_alerts`. The orchestrator injects a live network snapshot into every system prompt turn.

### KPI Agent (AI)

Runs a 6-timestep (60 s) **sliding-window LSTM** (`KPIClassifier` in `model.py`) per cell. Detects: OVERLOAD, UNDERLOAD, SINR_LOW, POWER_WASTE, NORMAL. Falls back to threshold rules while the buffer fills. Auto-moves overloaded cells to the lightest available DU. Writes alerts to InfluxDB `alerts` measurement with `severity`, `cell_id`, `alert_type` tags. Model weights are saved to / loaded from `kpi_model.pt`; if the file is absent the model trains from scratch on startup (`train.py`).

### Planning API

`planner_api.py` chains four pure modules:
1. `placement.py` — selects cells from the Bangalore candidate inventory given budget/density
2. `pci_planner.py` — assigns PCIs (collision + confusion-free)
3. `placement.py` — groups cells into DUs (`assign_dus`) then DUs into CUs (`assign_cus`)
4. `slice_allocator.py` — allocates eMBB/URLLC/mMTC slices per cell

Plans are stored in-memory (`_plans` dict) and applied by POSTing the converted topology to `Controller /topology/replace`.

## Key Design Decisions

- **Topology file, not DB**: runtime state lives in `topology.json` (not a database). The Controller is the only writer; simulators are read-only consumers.
- **InfluxDB for time-series**: all KPI telemetry, alerts, and topology events go to InfluxDB. The Controller merges topology + latest InfluxDB KPIs on every `/network` request.
- **LLM never writes directly**: the Gemini model only calls typed tools; tools call the Controller or Planning API over HTTP.
- **KPI agent is a separate process**: it polls independently on `POLL_INTERVAL_SEC` (default 30 s in Docker, 10 s in code default), never blocks the chat path.

## Coding Conventions

- LLM backend: `google-genai` SDK (not `anthropic`). Use `genai.Client` pattern matching the existing orchestrator.
- FastAPI + Pydantic v2 for all services; `httpx` (synchronous) for inter-service calls.
- Each agent's `requirements.txt` lists its own dependencies; no shared top-level requirements file.
- Env vars override all config; sane defaults are coded so services start without a full `.env` in dev.

## Environment Variables (non-obvious ones)

```
TOPO_POLL_SEC=5          # how often DU/CU simulators reload topology.json
POLL_INTERVAL_SEC=30     # KPI agent poll cadence
MODEL_PATH=kpi_model.pt  # LSTM weights file; trained on first boot if absent
MIN_CONFIDENCE=0.70      # minimum model softmax confidence to act on a prediction
GEMINI_MODEL=gemini-2.5-flash
```

## Deadline

All tasks in `spec.md` must be completed by **15 June 2026**.
