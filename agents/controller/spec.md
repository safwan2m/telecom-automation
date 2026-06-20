# Controller Agent — Specification

FastAPI service on port 8080. The single control plane for the live network — all topology mutations go through the Controller. No other service writes to `topology.json`.

## Internal flow

```
POST /move/cell  {"cell_id": "MLS_RWS_01", "to_du_id": "DU-MLS-2"}
      │
      ├─► load topology.json
      ├─► validate cell_id and to_du_id exist
      ├─► update cell["du_id"] in-memory
      ├─► write topology.json atomically (.tmp → rename)
      ├─► write topology_event to InfluxDB
      └─► return {"status": "ok", "cell_id": ..., "from_du": ..., "to_du": ...}

DU/CU simulators poll topology.json every TOPO_POLL_SEC (5 s) → reconfigure live.
```

## KPI merging

`GET /network` and `GET /cells` merge live KPI data from InfluxDB into each cell record. The Controller queries `cell_kpi` (last 3 min) via Flux and joins on `cell_id`. The response shape for each cell includes both config fields (`vendor`, `band`, `du_id`, etc.) and a nested `kpi` dict (`connected_ues`, `prb_dl_pct`, `sinr_db`, `power_w`, `dl_throughput_mbps`, etc.).

`GET /cells/{cell_id}` returns the cell config plus a 30-minute time series: one record per InfluxDB data point, sorted ascending by time.

## PCI auto-assignment

`POST /cells/add` automatically assigns a PCI if the request body sends `pci: 0`. It finds the smallest PCI value (starting from 1) not already used by any existing cell. The cell is inserted into `topology.json` and the DU simulator picks it up within `TOPO_POLL_SEC`.

## Congestion scoring

`GET /congestion` computes a weighted multi-factor score per cell from live KPIs:

| Factor | Weight |
|---|---|
| PRB utilisation | 40% |
| SINR (inverted) | 20% |
| BLER | 20% |
| Latency | 20% |

Returns cells ranked by score with severity levels CRITICAL / HIGH / MODERATE / LOW. For the top 5 critical cells, neighbour headroom is fetched via `GET /neighbors/{cell_id}` and attached as hints for the `optimize_congestion` orchestrator tool.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `INFLUX_URL` | `http://influxdb:8086` | KPI merging on /network and /cells |
| `INFLUX_TOKEN` | `telecom-super-secret-auth-token-2026` | InfluxDB auth |
| `INFLUX_ORG` | `telecom` | InfluxDB org |
| `INFLUX_BUCKET` | `telecom_metrics` | KPI bucket |
| `TOPOLOGY_FILE` | `/config/topology.json` | Path to topology source of truth |

## Routes

```
GET  /health
GET  /topology                          raw topology.json (no KPI merge)
GET  /network                           full state: all cells + DUs + CUs with live KPIs
GET  /cells?area=&du_id=&cu_id=         filtered cell list with live KPIs
GET  /cells/{cell_id}                   cell config + 30-min KPI time series
GET  /dus                               DU list with live KPIs
GET  /cus                               CU list with live KPIs
GET  /neighbors/{cell_id}?max_neighbors=6  Haversine geographic neighbour list
GET  /congestion?top_n=10              cells ranked by congestion score with severity

POST /move/cell   {"cell_id": "...", "to_du_id": "..."}
POST /move/du     {"du_id": "...", "to_cu_id": "..."}
POST /topology/replace  {"cus": {...}, "dus": {...}, "cells": {...}}
     └── full topology swap; used by plan/apply; validates structure before writing

POST /cells/add   {cell_id, du_id, area, lat, lon, generation, band, vendor,
                   freq_mhz, pci, hardware_model, antenna_config, peak_dl_mbps,
                   tx_power_w, idle_power_w, max_ues}
     └── pci=0 → auto-assigned; DU picks up within TOPO_POLL_SEC

DELETE /cells/{cell_id}
     └── removes cell from topology.json; DU deregisters within TOPO_POLL_SEC
```

## Design decisions

- **File, not DB**: runtime topology lives in `topology.json`. Atomic writes (`.tmp` → rename) prevent partial reads by simulator pollers.
- **LLM never writes directly**: Gemini calls typed tools; tools call the Controller or Planning API over HTTP. The Controller is the only writer.
- **InfluxDB for time-series only**: topology is the Controller's domain; KPIs are InfluxDB's domain. The Controller merges them at query time.
