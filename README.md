# Telecom Network Automation

An AI agent system for automated 4G/5G NSA network planning, deployment, and continuous optimisation over a simulated Malleswaram (North Bangalore) deployment. Accepts geographic and operational parameters and autonomously plans cell placement, applies live topology changes, monitors KPIs with a bidirectional LSTM model, and serves a live interactive map — all controllable through a natural language chat interface.

Built as an IISc course project demonstrating end-to-end O-RAN-aligned network automation.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│               Orchestrator Agent  :8082                      │
│    Claude CLI (Docker default)  ·  Gemini API (fallback)     │
│    16-tool calling loop  ·  streaming text/plain             │
└────────┬───────────────┬───────────────┬─────────────────────┘
         │               │               │
  ┌──────▼──────┐  ┌─────▼──────┐  ┌────▼──────────┐
  │ Planning    │  │ Controller │  │  KPI Agent    │
  │ Agent :8081 │  │ Agent :8080│  │  (background) │
  └──────┬──────┘  └─────┬──────┘  └────┬──────────┘
         │               │              │
  ┌──────▼───────────────▼──────────────▼──────────┐
  │                 InfluxDB  :8086                 │
  │  cell_kpi | du_kpi | cu_kpi | core_kpi         │
  │  ue_mobility | ue_usage | alerts | son_actions  │
  └──────────────────────┬─────────────────────────┘
                         │  topology.json
           ┌─────────────┼───────────────────┐
     ┌─────▼──────┐ ┌────▼──────┐ ┌──────────▼────┐
     │  3× DU sims│ │ 1× CU sim │ │  Core sim     │
     │ (4G+5G RAN)│ │(RRC/PDCP) │ │  AMF/SMF/UPF  │
     └────────────┘ └───────────┘ └───────────────┘
                         │
                  ┌──────▼──────┐
                  │ Map Server  │
                  │   :8083     │
                  └─────────────┘
```

### Deployment topology — Malleswaram, North Bangalore

30 cells across 10 macro tower sites (3 sectors each), grouped into 3 DUs under 1 CU. Peak active UEs: **18,400** (40,000 residents + 15% commuter overhead × 40% operator market share).

| CU | DU | Sites | Cells |
|---|---|---|---|
| CU-MLS | DU-MLS-1 | RWS, 18C, BEL, SNK (north) | 12 |
| CU-MLS | DU-MLS-2 | SPG, 3MN, 10C (central) | 9 |
| CU-MLS | DU-MLS-3 | MGR, CHD, 6CR (south-west) | 9 |

**Cell naming:** `MLS_<SITE>_<SECTOR>` e.g. `MLS_RWS_01`.  
**Sector mix per site:**

| Sites | Sector 1 | Sector 2 | Sector 3 |
|---|---|---|---|
| RWS, 18C, SNK, SPG, 10C (high-traffic) | 5G n78 3500 MHz | 5G n41 2500 MHz | 4G B3 1800 MHz |
| BEL, 3MN, MGR, CHD, 6CR (residential) | 5G n78 3500 MHz | 4G B40 2300 MHz | 4G B3 1800 MHz |

**RAN:** 4G/5G NSA — LTE anchor + 5G NR secondary, shared AMF/SMF/UPF core.  
**Vendor split (25% each — ~7–8 cells per vendor):**

| Vendor | 5G Hardware | 4G Hardware | Peak DL | Max UEs (5G) | System Power |
|---|---|---|---|---|---|
| Nokia | AirScale MAA 64T64R | AWHFA | 3800 Mbps | 800 | 1000 W |
| Ericsson | AIR 6449 / AIR 3221 | RBS 6402 | 3600 Mbps | 750 | 950 W |
| Samsung | TM500 64T64R | RRU | 3400 Mbps | 700 | 900 W |
| ZTE | AAU 5614 | RRU | 3200 Mbps | 680 | 1000 W |

---

## Components

### Orchestrator Agent (`agents/orchestrator/`) — port 8082

Accepts natural language, drives a multi-step tool-calling loop, and streams responses. Injects a live network snapshot into every system prompt.

**Backends** (selected at startup by `CLAUDE_CLI_PATH`):
- **Claude CLI** — active in Docker; spawns `claude -p` subprocess via `CustomAnthropicClient`
- **Gemini** — fallback when `CLAUDE_CLI_PATH` is unset; uses `google-genai` SDK

**16 available tools:**

| Tool | What it does |
|---|---|
| `query_network` | Full topology + live KPIs for all cells, DUs, CUs |
| `list_cells` | Filtered cell list (by area / DU / CU) |
| `query_cell` | Single cell config + 30-min KPI time series |
| `move_cell` | Move a cell to a different DU (~5 s propagation) |
| `move_du` | Reassign a DU to a different CU |
| `list_areas` | All named Malleswaram sub-locality areas with lat/lon |
| `get_area_cells` | Deployed cells covering ≥20% of a named area |
| `list_suspended_cells` | Cells with hardware installed but not transmitting |
| `plan_network` | Run planning engine → returns a network plan |
| `apply_plan` | Push an accepted plan to the Controller as live topology |
| `get_alerts` | Recent KPI alerts from InfluxDB |
| `query_ue` | Per-UE usage (DL/UL, latency) and mobility (handovers) |
| `get_son_status` | SON action summary + recent autonomous actions |
| `add_cell` | Deploy a new cell via chat (PCI auto-assigned) |
| `remove_cell` | Decommission a cell from the live topology |
| `optimize_congestion` | Cells ranked by multi-factor congestion score with neighbour hints |

### Controller Agent (`agents/controller/`) — port 8080

Single control plane and sole writer of `topology.json`. Merges live KPIs from InfluxDB into every GET response. Atomic topology writes (`.tmp` → rename) propagate to all DU/CU simulators within 5 seconds.

### Planning Agent (`agents/planning/`) — port 8081

Takes network parameters and runs a pipeline in sequence:
1. **Sufficiency check** — reorganise existing cells vs. deploy new ones
2. **Placement** — heuristic or MIP-based site selection
3. **PCI planner** — graph-colouring (collision and confusion-free)
4. **Slice allocator** — PRB budget per slice (eMBB / URLLC / mMTC)
5. **DU/CU grouping** — geographic proximity with configurable capacity limits

Supports five plan types: `reorganize`, `deploy`, `suspend`, `reactivate`, `reactivate_and_deploy`.

### KPI Monitoring Agent (`agents/kpi_agent/`) — background

Polls InfluxDB every 30 seconds (Docker). Maintains a 6-step (60 s) sliding window per cell and classifies state using a local bidirectional LSTM model.

**AI model — bidirectional LSTM classifier:**
- Input: 6 consecutive KPI readings × **9 features** = 60 seconds of history per cell
- Features: `prb_dl_pct`, `sinr_db`, `connected_ues`, `power_w`, `packet_loss_pct`, `dl_throughput_mbps`, `cqi`, `bler_pct`, `latency_ms`
- Architecture: 2-layer BiLSTM (hidden=64) → Linear(128→64) → ReLU → Dropout → Linear(64→5)
- Classes: `NORMAL`, `OVERLOAD`, `UNDERLOAD`, `SINR_LOW`, `POWER_WASTE`
- Trained on 5,000 synthetic sequences (70%/15%/8%/5%/2% split) at container startup; weights cached to `kpi_model.pt`
- Falls back to rule-based detection (tagged `[RULE]`) for first 60 s; switches to model (tagged `[AI]`) once buffer fills

**Autonomous SON actions:**

| Class | Condition | Action |
|---|---|---|
| `OVERLOAD` | PRB > 85% | 1st: steer load to neighbour cell (NEIGHBOR_LOAD_STEER); 2nd (score > 0.75): move cell to lightest DU (LOAD_BALANCE) |
| `NORMAL` | congestion score > 0.65 | Pre-emptive SON write (PRE_EMPTIVE_STEER) before threshold breach |
| `UNDERLOAD` | PRB < 20% | Write INFO alert; recommend handing UEs to lightest DU for DTX/sleep (TRAFFIC_STEER) |
| `SINR_LOW` | SINR < 5 dB | Write CRITICAL alert; request PCI re-optimisation (PCI_REOPT_REQUEST) |
| `POWER_WASTE` | power > 500 W, UEs < 15 | Write WARNING alert; recommend DTX mode (DTX_RECOMMEND) |

Cooldown gate: 300 s between SON actions on the same cell (configurable via `SON_COOLDOWN_SEC`).

### Map Server (`agents/map_server/`) — port 8083

Serves a Leaflet.js interactive map of all 30 Malleswaram cells on a dark basemap. Auto-refreshes every 30 seconds. Includes an integrated AI chat panel (proxied to the Orchestrator).

- **Colour by vendor:** Nokia=blue (#60a5fa), Ericsson=green (#4ade80), Samsung=purple (#a78bfa), ZTE=orange (#fb923c)
- **Opacity by generation:** 5G NR = solid (0.9), 4G LTE = faded (0.55)
- **Status fill:** red = overloaded (PRB > 85%), amber = SINR low (< 5 dB)
- **Coverage circles:** COST-231-Hata radius per cell; live KPI override when within ×0.5–2.0 of model estimate
- **Click popup:** vendor, hardware model, band, DU/CU, PCI, UEs, PRB, SINR, RSRP, power, throughput
- **Filter controls:** 5G/4G and all four vendors; updates in-place
- **AI chat panel:** collapsible right-side panel with shortcut buttons; proxied to Orchestrator

---

## Getting Started

### Prerequisites

- Docker Desktop (with Compose v2)
- Google AI Studio API key (free tier) — for Gemini fallback backend

### 1. Configure environment

```bash
cp dev-env/.env.example dev-env/.env
# Edit dev-env/.env and fill in all values
```

```env
INFLUXDB_ADMIN_USER=admin
INFLUXDB_ADMIN_PASSWORD=yourpassword
INFLUXDB_ORG=telecom
INFLUXDB_BUCKET=telecom_metrics
INFLUXDB_TOKEN=telecom-super-secret-auth-token-2026
GRAFANA_PASSWORD=yourpassword
GOOGLE_API_KEY=your-google-api-key        # Gemini fallback
LANGCHAIN_API_KEY=your-langsmith-key      # optional — LangSmith tracing
LANGCHAIN_TRACING_V2=true                 # set false to disable tracing
LANGCHAIN_PROJECT=telecom-automation
```

### 2. Start the stack

```bash
cd dev-env
docker compose up --build
```

First startup builds all images and trains the KPI LSTM model (~3 minutes). All 12 containers start in dependency order.

| Service | URL |
|---|---|
| Live cell map + AI chat | http://localhost:8083 |
| Terminal chat client | `py chat.py` |
| Grafana dashboards | http://localhost:3000 (admin / your password) |
| Controller API | http://localhost:8080 |
| Planning API | http://localhost:8081 |
| Orchestrator API | http://localhost:8082 |
| InfluxDB UI | http://localhost:8086 |

### 3. Open the map

Navigate to **http://localhost:8083** to see all 30 Malleswaram cells with live KPI colour coding, coverage circles, and an integrated AI chat panel.

### 4. Chat with the network

```bash
py chat.py
```

```
> show me all overloaded cells
> move MLS_RWS_01 to DU-MLS-2
> plan a network for Malleswaram with 800 users/km²
> apply the plan
> show CRITICAL alerts from the last 2 hours
```

Or via HTTP:

```bash
curl -X POST http://localhost:8082/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "show network status", "session_id": "ops1"}'
```

---

## API Reference

### Orchestrator (`localhost:8082`)
```
POST   /chat          {"message": "...", "session_id": "default"}  → streaming text/plain
GET    /history?session_id=
DELETE /history?session_id=
GET    /tools         → [{name, description}, ...]  (16 tools)
GET    /health        → {status, model, backend}
```

### Controller (`localhost:8080`)
```
GET    /health
GET    /topology                              raw topology.json
GET    /network                               full state + live KPIs
GET    /cells?area=&du_id=&cu_id=
GET    /cells/{cell_id}                       config + 30-min KPI series
GET    /dus
GET    /cus
GET    /neighbors/{cell_id}?max_neighbors=6   Haversine nearest cells
GET    /congestion                            cells ranked by congestion score

POST   /move/cell          {"cell_id", "to_du_id"}
POST   /move/du            {"du_id", "to_cu_id"}
POST   /topology/replace   {"cus", "dus", "cells"}
POST   /cells/add          {cell_id, du_id, area, lat, lon, ...}
DELETE /cells/{cell_id}
```

### Planning API (`localhost:8081`)
```
GET    /health
GET    /candidates
GET    /areas
GET    /areas/{area_id}/cells
GET    /plan/{id}
POST   /plan                {geographic_area, expected_user_density, traffic_profile,
                             spectrum_bands, latency_constraints, deployment_budget}
POST   /plan/multi-period   {..., demand_mode, time_periods}
POST   /plan/apply          {"plan_id": "..."}
GET    /cells/suspended
```

### Map Server (`localhost:8083`)
```
GET    /                  Leaflet.js map HTML (auto-refresh 30 s)
GET    /api/cells         cell list + coverage radii + live KPIs
POST   /api/chat          proxy → Orchestrator /chat (120 s timeout)
GET    /api/history       proxy → Orchestrator /history
DELETE /api/history       proxy → Orchestrator /history
GET    /api/tools         proxy → Orchestrator /tools
GET    /api/orch-health   proxy → Orchestrator /health
GET    /health
```

---

## Example chat interactions

```
> what is the current network status?
  → calls query_network, summarises cells by load, SINR, power

> which 5G cells are overloaded right now?
  → calls list_cells, filters by PRB > 85%

> move MLS_RWS_01 to DU-MLS-2
  → asks for confirmation, then calls move_cell

> plan a 5G network for Malleswaram with 800 users/km²,
  70% eMBB, 20% URLLC, budget $3M
  → calls plan_network, summarises the plan, asks to apply

> apply the plan
  → calls apply_plan with the plan_id from the previous response

> show CRITICAL alerts from the last 2 hours
  → calls get_alerts(severity="CRITICAL", last_minutes=120)

> are there any cells I can put to sleep?
  → calls get_son_status + list_suspended_cells, reports UNDERLOAD candidates

> which Nokia cells have the highest power draw?
  → calls query_network, filters by vendor, sorts by power_w
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_API_KEY` | required (Gemini) | Google AI Studio key |
| `GEMINI_MODEL` | `gemini-2.5-flash` (Docker) | Gemini model name |
| `CLAUDE_CLI_PATH` | `/usr/bin/claude` (Docker) | Path to `claude` binary; non-empty activates Claude CLI backend |
| `ANTHROPIC_MODEL_NAME` | `sonnet` | Claude model alias (Claude CLI backend only) |
| `LANGCHAIN_API_KEY` | optional | LangSmith tracing key |
| `LANGCHAIN_TRACING_V2` | `true` | Set `false` to disable LangSmith tracing |
| `LANGCHAIN_PROJECT` | `telecom-automation` | LangSmith project name |
| `INFLUXDB_TOKEN` | required | InfluxDB auth token |
| `INFLUXDB_ORG` | `telecom` | InfluxDB organisation |
| `INFLUXDB_BUCKET` | `telecom_metrics` | InfluxDB bucket |
| `CONTROLLER_URL` | `http://controller:8080` | Controller base URL |
| `PLANNING_URL` | `http://planning-api:8081` | Planning API base URL |
| `POLL_INTERVAL_SEC` | `30` (Docker) | KPI agent poll interval |
| `MIN_CONFIDENCE` | `0.70` | LSTM confidence gate |
| `SON_COOLDOWN_SEC` | `300` | Min seconds between SON actions on the same cell |
| `OVERLOAD_PRB_PCT` | `85` | PRB overload threshold |
| `UNDERLOAD_PRB_PCT` | `20` | PRB underload threshold |
| `SINR_MIN_DB` | `5` | SINR floor for SINR_LOW |
| `POWER_WASTE_W` | `500` | Power threshold for POWER_WASTE |
| `POWER_WASTE_MIN_UES` | `15` | Max UEs for POWER_WASTE trigger |

---

## Project Structure

```
telecom-automation/
├── agents/
│   ├── orchestrator/          LLM chat agent (FastAPI, port 8082)
│   │   ├── orchestrator.py    Dual-backend chat service (Claude CLI / Gemini)
│   │   ├── tools.py           16 tool schemas + TOOL_MAP dispatch table
│   │   ├── custom_anthropic_client.py  claude -p subprocess bridge
│   │   ├── tracing.py         LangSmith integration (no-op when inactive)
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── controller/            Topology control plane (FastAPI, port 8080)
│   │   ├── controller.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── planning/              Network planning engine (FastAPI, port 8081)
│   │   ├── planner_api.py
│   │   ├── placement.py       Heuristic + DU/CU grouping
│   │   ├── mip_placer.py      MIP-based optimal placement
│   │   ├── pci_planner.py     Graph-colouring PCI assignment
│   │   ├── slice_allocator.py PRB budget split (eMBB/URLLC/mMTC)
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── kpi_agent/             KPI monitoring + BiLSTM anomaly detection (background)
│   │   ├── kpi_agent.py       Poll loop, feature extraction, SON dispatcher
│   │   ├── model.py           KPIClassifier — 2-layer BiLSTM + MLP head
│   │   ├── train.py           Synthetic dataset generation + training
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── map_server/            Leaflet.js live cell map (FastAPI, port 8083)
│       ├── map_server.py      Inline HTML map + Orchestrator proxy routes
│       ├── Dockerfile
│       └── requirements.txt
├── dev-env/
│   ├── docker-compose.yml     12-container dev stack
│   ├── .env.example           Environment variable template
│   ├── config/
│   │   └── topology.json      30-cell Malleswaram topology (source of truth)
│   ├── grafana/               Dashboard JSON + datasource provisioning
│   └── simulators/
│       ├── core/              AMF/SMF/UPF KPI simulator
│       ├── cu/                CU simulator (shared image — 1 instance)
│       └── du/                DU simulator (shared image — 3 instances)
├── chat.py                    Interactive terminal chat client
├── spec.md                    Full project specification
├── prerequisites.md           O-RAN and 5G background reading
└── CLAUDE.md                  AI assistant instructions for this repo
```

---

## Useful Commands

```bash
# Start everything
cd dev-env && docker compose up --build -d

# Tail logs for a specific container
docker compose logs -f kpi-agent
docker compose logs -f orchestrator
docker compose logs -f map-server

# Stop everything
docker compose down

# Reset all InfluxDB state (wipe volumes)
docker compose down -v

# Rebuild a single service after code changes
docker compose up --build orchestrator
docker compose up --build map-server

# Check all container health
docker compose ps

# Query latest cell KPIs directly from InfluxDB
curl "http://localhost:8086/api/v2/query?org=telecom" \
  -H "Authorization: Token telecom-super-secret-auth-token-2026" \
  -H "Content-Type: application/vnd.flux" \
  -d 'from(bucket:"telecom_metrics") |> range(start:-5m) |> filter(fn:(r)=>r._measurement=="cell_kpi") |> last()'

# Get raw network state from Controller
curl http://localhost:8080/network | py -m json.tool

# Trigger a manual cell move
curl -X POST http://localhost:8080/move/cell \
  -H "Content-Type: application/json" \
  -d '{"cell_id":"MLS_RWS_01","to_du_id":"DU-MLS-2"}'

# Check recent SON actions
curl -X POST http://localhost:8082/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"/son","session_id":"ops"}'
```
