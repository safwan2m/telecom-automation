# Telecom Network Automation

An AI agent system for automated 5G network planning, deployment, and continuous optimization. Accepts geographic and operational parameters and autonomously plans RU/DU/CU placement, applies topology changes live, and continuously monitors KPIs — all controllable through a natural language chat interface.

Built as an IISc course project demonstrating end-to-end O-RAN-aligned network automation.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│              Orchestrator Agent  :8082               │
│         Gemini 2.5 Flash  +  tool-calling            │
│   /chat (streaming)  /history  /tools  /health       │
└────────┬──────────────────┬───────────────┬──────────┘
         │                  │               │
  ┌──────▼──────┐   ┌───────▼──────┐  ┌────▼──────────┐
  │ Planning API│   │  Controller  │  │   KPI Agent   │
  │    :8081    │   │    :8080     │  │  (background) │
  │  /plan      │   │  /network    │  │  LSTM model   │
  │  /plan/apply│   │  /move/cell  │  │  + alerting   │
  └──────┬──────┘   └───────┬──────┘  └────┬──────────┘
         │                  │              │
  ┌──────▼──────────────────▼──────────────▼──────────┐
  │                  InfluxDB  :8086                   │
  │  cell_kpi | du_kpi | cu_kpi | core_kpi            │
  │  ue_mobility | ue_usage | alerts | topo_events     │
  └──────────────────────┬────────────────────────────┘
                         │  topology.json
           ┌─────────────┼─────────────┐
     ┌─────▼─────┐ ┌─────▼─────┐ ┌────▼──────┐
     │ 6× DU sims│ │ 2× CU sims│ │ Core sim  │
     │ (RAN/L2)  │ │(RRC/PDCP) │ │(AMF/SMF)  │
     └───────────┘ └───────────┘ └───────────┘
```

### Deployment topology

14 cells across 9 Bangalore areas, grouped under 6 DUs and 2 CUs:

| CU | DUs under it | Areas served |
|---|---|---|
| CU-NORTH | DU-NORTH-1, DU-NORTH-2, DU-CENTRAL | Koramangala, Indiranagar, Yeshwanthpur, MG Road, Hebbal |
| CU-SOUTH | DU-EAST-1, DU-SOUTH-1, DU-SOUTH-2 | Whitefield, Electronic City, HSR Layout, Jayanagar, Banashankari |

All containers stream KPIs to InfluxDB every 10 seconds. Topology changes propagate within 5 seconds via file-watch polling on `topology.json`.

---

## Components

### Orchestrator Agent (`agents/orchestrator/`)
FastAPI service on **port 8082**. Accepts natural language from an operator, routes to tools via Gemini function-calling, and streams the response. Injects a live network snapshot into every system prompt so the model always has current state.

**Tools available to the LLM:**

| Tool | What it does |
|---|---|
| `query_network` | Full topology + live KPIs for all cells, DUs, CUs |
| `list_cells` | Filtered cell list (by area / DU / CU) |
| `query_cell` | 30-minute KPI time series for one cell |
| `move_cell` | Move a cell to a different DU (live, ~5 s propagation) |
| `move_du` | Reassign a DU to a different CU |
| `plan_network` | Run planning engine → returns full network plan |
| `apply_plan` | Push an accepted plan to the Controller |
| `get_alerts` | Recent KPI alerts from InfluxDB |

### Controller / Core DB (`dev-env/simulators/controller/`)
Single control plane on **port 8080**. Serves as the source of truth for topology (backed by `topology.json`). Merges live InfluxDB KPIs into every GET response. Atomic topology writes propagate to all DU/CU containers.

### Planning Engine (`planning/`)
FastAPI service on **port 8081**. Takes network parameters and runs four algorithms in sequence:

1. **Placement** — density-weighted heuristic cell placement with Haversine distance
2. **PCI planner** — graph-coloring algorithm (collision and confusion free)
3. **Slice allocator** — PRB budget allocation per slice (eMBB / URLLC / mMTC)
4. **DU/CU grouping** — geographic proximity grouping with configurable capacity limits

### KPI Monitoring Agent (`agents/kpi_agent/`)
Background process (no exposed port). Polls InfluxDB every 30 seconds per cell and classifies state using a local bidirectional LSTM model. Takes autonomous corrective actions.

**AI model — bidirectional LSTM classifier:**
- Input: sliding window of 6 consecutive KPI readings per cell (60 seconds of history)
- Features: `prb_dl_pct`, `sinr_db`, `connected_ues`, `power_w`, `packet_loss_pct`, `throughput_dl_mbps`
- Architecture: 2-layer BiLSTM (hidden=64) → MLP head → 5-class softmax
- Classes: `NORMAL`, `OVERLOAD`, `UNDERLOAD`, `SINR_LOW`, `POWER_WASTE`
- Trained from synthetic data at container startup (~3 min); weights cached to `kpi_model.pt`
- Falls back to rule-based detection (tagged `[RULE]`) for the first 60 seconds while the buffer fills; switches to model inference (tagged `[AI]`) once 6 readings accumulate

**Autonomous actions:**
- `OVERLOAD` (PRB > 85%) → calls `POST /move/cell` to rebalance load
- `SINR_LOW` (SINR < 5 dB) → writes `CRITICAL` alert to InfluxDB
- `UNDERLOAD` (PRB < 20%) → writes `INFO` alert (sleep candidate)
- `POWER_WASTE` → writes `WARNING` alert

---

## Getting Started

### Prerequisites

- Docker Desktop (with Compose v2)
- A Google AI Studio API key (free tier): [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey)

### 1. Configure environment

```bash
cp dev-env/.env.example dev-env/.env
```

Edit `dev-env/.env` and fill in your values:

```
INFLUXDB_ADMIN_USER=admin
INFLUXDB_ADMIN_PASSWORD=yourpassword
INFLUXDB_ORG=telecom
INFLUXDB_BUCKET=telecom_metrics
INFLUXDB_TOKEN=your-influxdb-token
GRAFANA_PASSWORD=yourpassword
GOOGLE_API_KEY=your-google-api-key
```

### 2. Start the stack

```bash
cd dev-env
docker compose up --build
```

First startup builds all images. The KPI agent trains its LSTM model in the background (~3 minutes). All 15 containers will be running:

| Container | Port | Purpose |
|---|---|---|
| `influxdb` | 8086 | Time-series KPI storage |
| `grafana` | 3000 | Dashboards (admin / your password) |
| `controller` | 8080 | Topology control plane |
| `planning-api` | 8081 | Network planning engine |
| `orchestrator` | 8082 | LLM chat agent |
| `core-sim` | — | AMF/SMF/UPF simulator |
| `cu-north`, `cu-south` | — | CU simulators |
| `du-north-1/2`, `du-central`, `du-east-1`, `du-south-1/2` | — | DU simulators |
| `kpi-agent` | — | KPI monitoring + anomaly detection |

### 3. Chat with the network

**Option A — terminal chat client:**

```bash
py chat.py
```

```
> show me all overloaded cells
> move BLR_KRM_01 to DU-NORTH-2
> plan a network for Whitefield with 800 users/km²
> what alerts fired in the last hour?
```

**Option B — HTTP directly:**

```bash
curl -N -X POST http://localhost:8082/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "show network status", "session_id": "ops1"}'
```

### 4. View dashboards

Open [http://localhost:3000](http://localhost:3000) → log in with `admin` / your `GRAFANA_PASSWORD`.

InfluxDB UI is at [http://localhost:8086](http://localhost:8086).

---

## API Reference

### Orchestrator (`localhost:8082`)

```
POST   /chat          {"message": "...", "session_id": "default"}  → streaming text
GET    /history?session_id=default
DELETE /history?session_id=default
GET    /tools
GET    /health
```

### Controller (`localhost:8080`)

```
GET    /health
GET    /topology                           raw topology.json
GET    /network                            full state + live KPIs
GET    /cells?area=&du_id=&cu_id=          filtered cell list
GET    /cells/{cell_id}                    cell detail + 30-min time series
GET    /dus                                DU list with KPIs
GET    /cus                                CU list with KPIs
POST   /move/cell   {"cell_id":"...", "to_du_id":"..."}
POST   /move/du     {"du_id":"...",   "to_cu_id":"..."}
```

### Planning API (`localhost:8081`)

```
POST   /plan          {geographic_area, expected_user_density, traffic_profile,
                       spectrum_bands, latency_constraints,
                       compute_resources, deployment_budget}
POST   /plan/apply    {"plan_id": "..."}
GET    /plan/{id}
GET    /health
```

---

## Example chat interactions

```
> what is the current network status?
  [calls query_network, summarises cells by load and SINR]

> which cells are overloaded right now?
  [calls list_cells, filters by PRB > 85%]

> move BLR_WFD_01 to DU-NORTH-1
  [asks for confirmation, then calls move_cell]

> plan a 5G network for Electronic City with 1000 users/km²
  and 70% eMBB, 20% URLLC, budget $3M
  [calls plan_network, summarises plan, asks if you want to apply it]

> apply the plan
  [calls apply_plan with the plan_id from above]

> show me CRITICAL alerts from the last 2 hours
  [calls get_alerts with severity=CRITICAL, last_minutes=120]
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_API_KEY` | required | Google AI Studio key for Gemini |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model to use |
| `INFLUXDB_TOKEN` | required | InfluxDB auth token |
| `INFLUXDB_ORG` | `telecom` | InfluxDB organisation |
| `INFLUXDB_BUCKET` | `telecom_metrics` | InfluxDB bucket |
| `CONTROLLER_URL` | `http://controller:8080` | Controller API base URL |
| `PLANNING_URL` | `http://planning-api:8081` | Planning API base URL |
| `POLL_INTERVAL_SEC` | `30` | KPI agent poll interval |
| `OVERLOAD_PRB_PCT` | `85` | PRB threshold for overload detection |
| `UNDERLOAD_PRB_PCT` | `20` | PRB threshold for underload detection |
| `SINR_MIN_DB` | `5` | SINR floor for SINR_LOW detection |

---

## Project Structure

```
telecom-automation/
├── agents/
│   ├── orchestrator/          Gemini chat agent (FastAPI, port 8082)
│   │   ├── orchestrator.py    main app + tool-calling loop
│   │   ├── tools.py           tool implementations + JSON schemas
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── kpi_agent/             KPI monitoring + LSTM anomaly detection
│       ├── kpi_agent.py       polling loop, inference, alerting
│       ├── model.py           BiLSTM model definition + normalisation
│       ├── train.py           synthetic data generation + training
│       ├── Dockerfile
│       └── requirements.txt
├── planning/                  Network planning engine (FastAPI, port 8081)
│   ├── planner_api.py         REST endpoints
│   ├── placement.py           cell placement algorithm
│   ├── pci_planner.py         graph-coloring PCI assignment
│   ├── slice_allocator.py     PRB slice allocation
│   ├── Dockerfile
│   └── requirements.txt
├── dev-env/
│   ├── docker-compose.yml     15-container stack definition
│   ├── .env.example           environment variable template
│   ├── config/
│   │   └── topology.json      14-cell Bangalore topology (source of truth)
│   ├── grafana/               Grafana datasource provisioning
│   └── simulators/
│       ├── controller/        topology control plane (port 8080)
│       ├── core/              AMF/SMF/UPF KPI simulator
│       ├── cu/                CU simulator (reused for CU-NORTH and CU-SOUTH)
│       └── du/                DU simulator (reused for all 6 DUs)
├── chat.py                    interactive terminal chat client
├── spec.md                    full project specification
├── prerequisites.md           O-RAN and 5G background reading
└── CLAUDE.md                  AI assistant instructions for this repo
```

---

## Useful commands

```bash
# Start everything
cd dev-env && docker compose up --build -d

# Tail logs for a specific container
docker logs -f kpi-agent
docker logs -f orchestrator

# Stop everything
docker compose down

# Stop and wipe all volumes (reset InfluxDB state)
docker compose down -v

# Rebuild a single service after code changes
docker compose up --build orchestrator

# Check all container health
docker compose ps

# Query InfluxDB directly (get recent cell KPIs)
curl "http://localhost:8086/api/v2/query?org=telecom" \
  -H "Authorization: Token your-influxdb-token" \
  -H "Content-Type: application/vnd.flux" \
  -d 'from(bucket:"telecom_metrics") |> range(start:-5m) |> filter(fn:(r)=>r._measurement=="cell_kpi")'
```
