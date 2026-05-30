# Telecom Network Automation

An AI agent system for automated 4G/5G NSA network planning, deployment, and continuous optimization across Bangalore. Accepts geographic and operational parameters and autonomously plans cell placement, applies live topology changes, monitors KPIs with a bidirectional LSTM model, and serves a live interactive map — all controllable through a natural language chat interface.

Built as an IISc course project demonstrating end-to-end O-RAN-aligned network automation.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│               Orchestrator Agent  :8082                      │
│          Gemini 2.5 Flash  +  tool-calling (streaming)       │
└────────┬───────────────┬───────────────┬─────────────────────┘
         │               │               │
  ┌──────▼──────┐  ┌─────▼──────┐  ┌────▼──────────┐
  │ Planning    │  │ Controller │  │  KPI Agent    │
  │ Agent :8081 │  │ Agent :8080│  │  (background) │
  └──────┬──────┘  └─────┬──────┘  └────┬──────────┘
         │               │              │
  ┌──────▼───────────────▼──────────────▼──────────┐
  │                 InfluxDB  :8086                 │
  └──────────────────────┬─────────────────────────┘
                         │  topology.json
           ┌─────────────┼───────────────────┐
     ┌─────▼──────┐ ┌────▼──────┐ ┌──────────▼────┐
     │ 14× DU sims│ │ 4× CU sims│ │  Core sim     │
     └────────────┘ └───────────┘ └───────────────┘
                         │
                  ┌──────▼──────┐
                  │ Map Server  │
                  │   :8083     │
                  └─────────────┘
```

### Bangalore deployment topology

48 cells across 18 zones, grouped into 14 DUs under 4 CUs:

| CU | DUs | Areas |
|---|---|---|
| CU-NORTH | DU-NORTH-1/2/3/4 | Hebbal, Yelahanka, Sadashivanagar, Yeshwanthpur, Rajajinagar, Vijayanagar |
| CU-CENTRAL | DU-CENTRAL-1/2/3 | MG Road, Indiranagar, Koramangala |
| CU-EAST | DU-EAST-1/2/3/4 | Whitefield, Marathahalli, KR Puram, Bellandur |
| CU-SOUTH | DU-SOUTH-1/2/3 | Electronic City, Jayanagar, BTM Layout, Banashankari, JP Nagar |

**RAN:** 4G/5G NSA — LTE anchor + 5G NR secondary, shared AMF/SMF/UPF core.  
**Vendor split (25% each — 12 cells per vendor):**

| Vendor | 5G Hardware | 4G Hardware | Peak DL | Max UEs | System Power |
|---|---|---|---|---|---|
| Nokia | AirScale MAA 64T64R | AWHFA | 3800 Mbps | 800 | 1000 W |
| Ericsson | AIR 6449 / AIR 3221 | RBS 6402 | 3600 Mbps | 750 | 950 W |
| Samsung | TM500 64T64R | RRU | 3400 Mbps | 700 | 900 W |
| ZTE | AAU 5614 | RRU | 3200 Mbps | 680 | 1000 W |

All containers stream KPIs to InfluxDB every 10 seconds. Topology changes propagate within 5 seconds.

---

## Components

### Orchestrator Agent (`agents/orchestrator/`) — port 8082
Accepts natural language from an operator, routes to tools via Gemini function-calling, and streams responses. Injects a live network snapshot into every system prompt.

**Available tools:**

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

### Controller Agent (`agents/controller/`) — port 8080
Single control plane and source of truth (backed by `topology.json`). Merges live KPIs from InfluxDB into every GET response. Atomic topology writes propagate to all DU/CU containers.

### Planning Agent (`agents/planning/`) — port 8081
Takes network parameters and runs four algorithms in sequence:
1. **Placement** — density-weighted heuristic with Haversine distance
2. **PCI planner** — graph-coloring (collision and confusion free)
3. **Slice allocator** — PRB budget per slice (eMBB / URLLC / mMTC)
4. **DU/CU grouping** — geographic proximity with configurable capacity limits

### KPI Monitoring Agent (`agents/kpi_agent/`) — background
Polls InfluxDB every 30 seconds. Maintains a 6-step (60 s) sliding window per cell and classifies state using a local bidirectional LSTM model. Takes autonomous corrective actions.

**AI model — bidirectional LSTM classifier:**
- Input: 6 consecutive KPI readings × 6 features = 60 seconds of history per cell
- Features: `prb_dl_pct`, `sinr_db`, `connected_ues`, `power_w`, `packet_loss_pct`, `dl_throughput_mbps`
- Architecture: 2-layer BiLSTM (hidden=64) → MLP head → 5-class softmax
- Classes: `NORMAL`, `OVERLOAD`, `UNDERLOAD`, `SINR_LOW`, `POWER_WASTE`
- Trained from synthetic data at container startup (~3 min); weights cached to `kpi_model.pt`
- Falls back to rule-based detection (tagged `[RULE]`) for first 60 s; switches to model (tagged `[AI]`) once buffer fills

**Autonomous actions:**
- `OVERLOAD` (PRB > 85%) → calls `POST /move/cell` to rebalance load
- `SINR_LOW` (SINR < 5 dB) → writes `CRITICAL` alert
- `UNDERLOAD` (PRB < 20%) → writes `INFO` alert (sleep candidate)
- `POWER_WASTE` (high power, very few UEs) → writes `WARNING` alert

### Map Server (`agents/map_server/`) — port 8083
Serves a Leaflet.js interactive map of all 48 cells on a dark Bangalore basemap. Auto-refreshes every 30 seconds. Filter by vendor or generation; click any cell for full KPI details.

- **Colour by vendor:** Nokia=blue, Ericsson=green, Samsung=purple, ZTE=orange
- **Opacity by generation:** 5G NR = solid, 4G LTE = faded
- **Status fill:** red = overloaded (PRB > 85%), amber = SINR low (< 5 dB)
- **Click popup:** vendor, hardware model, band, DU/CU, PCI, connected UEs, PRB, SINR, RSRP, power, throughput

---

## Getting Started

### Prerequisites
- Docker Desktop (with Compose v2)
- Google AI Studio API key (free tier): [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey)

### 1. Configure environment

```bash
cp dev-env/.env.example dev-env/.env
# Edit dev-env/.env and fill in all values
```

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

First startup builds all images and trains the KPI LSTM model (~3 minutes). All 26 containers start in dependency order.

| Service | URL |
|---|---|
| Live cell map | http://localhost:8083 |
| Chat interface | `py chat.py` or `POST http://localhost:8082/chat` |
| Grafana dashboards | http://localhost:3000 (admin / your password) |
| Controller API | http://localhost:8080 |
| Planning API | http://localhost:8081 |
| InfluxDB UI | http://localhost:8086 |

### 3. Open the map

Navigate to **http://localhost:8083** to see all 48 cells plotted on Bangalore with live KPI colour coding. The page auto-refreshes every 30 seconds.

### 4. Chat with the network

```bash
py chat.py
```

```
> show me all overloaded cells
> move BLR_KRM_01 to DU-CENTRAL-2
> plan a network for Electronic City with 1000 users/km²
> apply the plan
> show CRITICAL alerts from the last 2 hours
```

Or via HTTP:

```bash
curl -N -X POST http://localhost:8082/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "show network status", "session_id": "ops1"}'
```

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
GET    /cells?area=&du_id=&cu_id=
GET    /cells/{cell_id}                    30-min KPI time series
GET    /dus
GET    /cus
POST   /move/cell   {"cell_id":"...", "to_du_id":"..."}
POST   /move/du     {"du_id":"...",   "to_cu_id":"..."}
```

### Planning Agent (`localhost:8081`)
```
POST   /plan          {geographic_area, expected_user_density, traffic_profile,
                       spectrum_bands, latency_constraints, compute_resources, deployment_budget}
POST   /plan/apply    {"plan_id": "..."}
GET    /plan/{id}
GET    /health
```

### Map Server (`localhost:8083`)
```
GET    /             Leaflet map HTML (auto-refresh 30 s)
GET    /api/cells    Cell list + live KPIs (JSON)
GET    /health
```

---

## Example chat interactions

```
> what is the current network status?
  → calls query_network, summarises cells by load, SINR, power

> which 5G cells are overloaded right now?
  → calls list_cells, filters by PRB > 85% and generation = 5G

> move BLR_WFD_01 to DU-EAST-2
  → asks for confirmation, then calls move_cell

> plan a 5G network for Whitefield with 800 users/km²,
  70% eMBB, 20% URLLC, budget $3M
  → calls plan_network, summarises plan, asks if you want to apply it

> apply the plan
  → calls apply_plan with the plan_id

> show me CRITICAL alerts from the last 2 hours
  → calls get_alerts with severity=CRITICAL, last_minutes=120

> which Nokia cells have the highest power draw?
  → calls query_network, filters by vendor, sorts by power_w
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_API_KEY` | required | Google AI Studio key for Gemini |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model |
| `INFLUXDB_TOKEN` | required | InfluxDB auth token |
| `INFLUXDB_ORG` | `telecom` | InfluxDB organisation |
| `INFLUXDB_BUCKET` | `telecom_metrics` | InfluxDB bucket |
| `CONTROLLER_URL` | `http://controller:8080` | Controller base URL |
| `PLANNING_URL` | `http://planning-api:8081` | Planning API base URL |
| `POLL_INTERVAL_SEC` | `30` | KPI agent poll interval |
| `OVERLOAD_PRB_PCT` | `85` | PRB overload threshold |
| `UNDERLOAD_PRB_PCT` | `20` | PRB underload threshold |
| `SINR_MIN_DB` | `5` | SINR floor for SINR_LOW |
| `POWER_WASTE_W` | `500` | Power threshold for POWER_WASTE |
| `POWER_WASTE_MIN_UES` | `15` | Max UEs for POWER_WASTE trigger |
| `MIN_CONFIDENCE` | `0.70` | LSTM confidence gate |

---

## Project Structure

```
telecom-automation/
├── agents/
│   ├── orchestrator/          Gemini chat agent (FastAPI, port 8082)
│   │   ├── orchestrator.py
│   │   ├── tools.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── controller/            Topology control plane (FastAPI, port 8080)
│   │   ├── controller.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── planning/              Network planning engine (FastAPI, port 8081)
│   │   ├── planner_api.py
│   │   ├── placement.py
│   │   ├── pci_planner.py
│   │   ├── slice_allocator.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── kpi_agent/             KPI monitoring + BiLSTM anomaly detection (background)
│   │   ├── kpi_agent.py
│   │   ├── model.py
│   │   ├── train.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── map_server/            Leaflet.js live cell map (FastAPI, port 8083)
│       ├── map_server.py
│       ├── Dockerfile
│       └── requirements.txt
├── dev-env/
│   ├── docker-compose.yml     26-container stack
│   ├── .env.example           Environment variable template
│   ├── config/
│   │   └── topology.json      48-cell Bangalore topology (source of truth)
│   ├── grafana/               Grafana datasource provisioning
│   └── simulators/
│       ├── core/              AMF/SMF/UPF KPI simulator
│       ├── cu/                CU simulator (shared image for all 4 CUs)
│       └── du/                DU simulator (shared image for all 14 DUs)
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
docker logs -f kpi-agent
docker logs -f orchestrator
docker logs -f map-server

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
  -H "Authorization: Token your-influxdb-token" \
  -H "Content-Type: application/vnd.flux" \
  -d 'from(bucket:"telecom_metrics") |> range(start:-5m) |> filter(fn:(r)=>r._measurement=="cell_kpi") |> last()'

# Get raw network state from Controller
curl http://localhost:8080/network | python -m json.tool

# Trigger a manual cell move
curl -X POST http://localhost:8080/move/cell \
  -H "Content-Type: application/json" \
  -d '{"cell_id":"BLR_KRM_01","to_du_id":"DU-CENTRAL-2"}'
```
