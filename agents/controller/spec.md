# Controller Agent — Specification

FastAPI service on port 8080. Single control plane for the live network — the only service that writes `topology.json`. Serves topology + live KPIs over REST; all topology mutations (moves, add, delete, replace) go through here.

## Routes

```
GET    /health
GET    /topology
GET    /network
GET    /cells?area=&du_id=&cu_id=
GET    /cells/{cell_id}
GET    /dus
GET    /cus
GET    /neighbors/{cell_id}?max_neighbors=6
GET    /congestion

POST   /move/cell          {"cell_id", "to_du_id"}
POST   /move/du            {"du_id", "to_cu_id"}
POST   /topology/replace   {"cus": {...}, "dus": {...}, "cells": {...}}
POST   /cells/add          {cell_id, du_id, area, lat, lon, ...}

DELETE /cells/{cell_id}
```

## topology.json schema

The single source of truth. Written atomically (`TOPOLOGY_FILE.with_suffix(".tmp")` → `shutil.move`).

Top-level keys written by `write_topology()`:

```json
{
  "version":      <int — incremented by 1 on every write>,
  "last_updated": "<UTC ISO-8601 timestamp>",
  "updated_by":   "<caller string e.g. move_cell:MLS_RWS_01>",
  "meta":         { ... },
  "cus": {
    "<cu_id>": { "host": "...", "region": "...", "du_ids": ["DU-MLS-1", ...] }
  },
  "dus": {
    "<du_id>": { "host": "...", "cu_id": "<cu_id>", "cell_ids": ["MLS_RWS_01", ...] }
  },
  "cells": {
    "<cell_id>": {
      "area", "lat", "lon", "generation", "band", "freq_mhz", "pci",
      "vendor", "hardware_model", "antenna_config",
      "peak_dl_mbps", "tx_power_w", "idle_power_w", "max_ues"
    }
  }
}
```

`meta` is optional (written by planning-api:apply; preserved across moves if present).

## InfluxDB connection

Lazy singleton `_influx`: created on first call to `get_influx()`, reused for the process lifetime. `write_api(SYNCHRONOUS)` is instantiated per write call (not cached).

## InfluxDB KPI queries

All four helpers follow the same pattern: `range(start: -5m)`, `last()`, `pivot()`, then strip rows where the key field is absent. `_`-prefixed columns are stripped from returned dicts **except** `_time`; `result` and `table` columns are also stripped.

### `latest_cell_kpis()` → `dict[cell_id, kpi_dict]`

```flux
from(bucket: "telecom_metrics")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "cell_kpi")
  |> last()
  |> pivot(rowKey: ["_time","cell_id","area","band","pci","du_id","cu_id"],
           columnKey: ["_field"], valueColumn: "_value")
```

Keyed by `cell_id`.

### `latest_du_kpis()` → `dict[du_id, kpi_dict]`

```flux
  |> filter(fn: (r) => r._measurement == "du_kpi")
  |> pivot(rowKey: ["_time","du_id","cu_id"], ...)
```

Keyed by `du_id`.

### `latest_cu_kpis()` → `dict[cu_id, kpi_dict]`

```flux
  |> filter(fn: (r) => r._measurement == "cu_kpi")
  |> pivot(rowKey: ["_time","cu_id"], ...)
```

Keyed by `cu_id`.

### `latest_core_kpis()` → `dict[component, kpi_dict]`

```flux
  |> filter(fn: (r) => r._measurement == "core_kpi")
  |> pivot(rowKey: ["_time","component","instance_id"], ...)
```

Keyed by `component`. All query failures return `{}` (logged as WARNING).

## topology_event InfluxDB writes

`_record_event(event_type, details)` — writes to measurement `topology_event`:

| Kind | Name | Value |
|---|---|---|
| tag | `event_type` | `cell_move` / `cell_add` / `cell_remove` / `du_move` / `topology_replace` |
| fields | `<key>` | every `details` dict key, cast to `str(v)` |

Exceptions swallowed (logged as WARNING). Uses `SYNCHRONOUS` write mode.

---

## Route details

### GET /health

```json
{"status": "ok", "influxdb": <bool>, "topology_exists": <bool>}
```

`influxdb` is `True` if `client.ping()` succeeds. `topology_exists` is `TOPOLOGY_FILE.exists()`.

### GET /topology

Returns `read_topology()` verbatim — raw `topology.json` dict. No KPI merge.

### GET /network

Merges topology with all four KPI sets. Response:

```json
{
  "cells": {
    "<cell_id>": {
      ...all cell config fields...,
      "du_id": "<resolved from topo["dus"] cell_ids scan>",
      "cu_id": "<topo["dus"][du_id]["cu_id"]>",
      "kpi":  { ...latest_cell_kpis() or {} ... }
    }
  },
  "dus": {
    "<du_id>": { ...du config..., "kpi": { ...latest_du_kpis() or {}... } }
  },
  "cus": {
    "<cu_id>": { ...cu config..., "kpi": { ...latest_cu_kpis() or {}... } }
  },
  "core": { ...latest_core_kpis()... },
  "topology_version": <int>,
  "last_updated": "<str>"
}
```

`du_id` per cell is resolved by scanning `topo["dus"]` for which DU's `cell_ids` list contains the cell. `cu_id` follows from `topo["dus"][du_id]["cu_id"]`.

### GET /cells

Query params (all optional, exact-match): `area`, `du_id`, `cu_id`.

Returns a **list** (not dict). Each element:

```json
{
  ...all cell config fields...,
  "cell_id": "<id>",
  "du_id":   "<resolved>",
  "cu_id":   "<resolved>",
  "kpi":     { ...or {}... }
}
```

### GET /cells/{cell_id}

404 if not in `topo["cells"]`. Returns cell config + resolved `du_id`/`cu_id` + `series`:

```flux
from(bucket: "telecom_metrics")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "cell_kpi" and r.cell_id == "<id>")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
```

Series records keep `_time`; all other `_`-prefixed keys are stripped.

### GET /dus

Returns a list: `[{...du_config..., "du_id": id, "kpi": {...}}]`.

### GET /cus

Returns a list: `[{...cu_config..., "cu_id": id, "kpi": {...}}]`.

### GET /neighbors/{cell_id}

404 if cell not in topology. Returns the `max_neighbors` geographically closest cells (excluding self), sorted ascending by Haversine distance.

**Haversine formula** (Earth radius R = 6371.0 km):

```
dlat = radians(lat2 − lat1)
dlon = radians(lon2 − lon1)
a    = sin(dlat/2)² + cos(radians(lat1)) × cos(radians(lat2)) × sin(dlon/2)²
d_km = R × 2 × asin(sqrt(a))
```

Response:

```json
{
  "cell_id": "<source>",
  "neighbors": [
    {"cell_id": "...", "distance_km": 0.413, ...all cell config fields...},
    ...
  ]
}
```

`distance_km` rounded to 3 decimal places.

### GET /congestion

Computes congestion score per cell from live KPIs. No query parameters.

**Score formula** (same as KPI agent):

```
PRB     = min(prb_dl_pct / 100, 1.0)           weight 40%
SINR    = max(0,  1 − sinr_db / 25)             weight 20%   (25 dB → 0 contribution)
BLER    = min(bler_pct / 20,   1.0)             weight 20%   (20% → max)
LATENCY = min(latency_ms / 150, 1.0)            weight 20%   (150 ms → max)

score = 0.40×PRB + 0.20×SINR + 0.20×BLER + 0.20×LATENCY   (rounded 3 dp)
```

**Null defaults** applied before scoring: `prb_dl_pct→0`, `sinr_db→20`, `bler_pct→1.0`, `latency_ms→15`.

**Severity thresholds:**

| Level | Condition |
|---|---|
| CRITICAL | score > 0.75 |
| HIGH | score > 0.55 |
| MODERATE | score > 0.35 |
| LOW | score ≤ 0.35 |

Cells sorted descending by score. `area`, `du_id`, `band` fall back to topology config if absent from KPI row.

Response:

```json
{
  "cells": [
    {
      "cell_id", "area", "du_id", "band",
      "congestion_score", "level",
      "prb_dl_pct", "sinr_db", "bler_pct", "latency_ms", "connected_ues"
    }, ...
  ],
  "summary": {"CRITICAL": N, "HIGH": N, "MODERATE": N, "LOW": N},
  "total_cells": N
}
```

---

## POST /move/cell

Request: `{"cell_id": "...", "to_du_id": "..."}`.

- 404 if `cell_id` not in `topo["cells"]` or `to_du_id` not in `topo["dus"]`.
- Returns `{"status": "no-op", "message": "..."}` if cell already on `to_du_id`.
- Otherwise:
  1. Remove `cell_id` from `topo["dus"][from_du]["cell_ids"]`.
  2. Append `cell_id` to `topo["dus"][to_du_id]["cell_ids"]`.
  3. `write_topology(topo, updated_by="move_cell:<cell_id>")`.
  4. `_record_event("cell_move", {cell_id, from_du, to_du, from_cu, to_cu})`.

Response:

```json
{
  "status": "ok",
  "cell_id":    "MLS_RWS_01",
  "from_du":    "DU-MLS-1",
  "to_du":      "DU-MLS-2",
  "cu_change":  false,
  "new_cu":     "CU-MLS",
  "topology_version": 12
}
```

`cu_change` is `True` when moving to a DU under a different CU. The CU's `du_ids` list is **not** mutated — only the DU's `cell_ids` and the cell's logical `du_id` (resolved at query time from `cell_ids`).

## POST /move/du

Request: `{"du_id": "...", "to_cu_id": "..."}`.

- 404 if `du_id` or `to_cu_id` not found.
- Returns no-op if already under `to_cu_id`.
- Otherwise three mutations:
  1. Remove `du_id` from `topo["cus"][from_cu]["du_ids"]`.
  2. Append `du_id` to `topo["cus"][to_cu_id]["du_ids"]`.
  3. Set `topo["dus"][du_id]["cu_id"] = to_cu_id`.
- `write_topology(topo, updated_by="move_du:<du_id>")`.

Response: `{"status": "ok", "du_id", "from_cu", "to_cu", "topology_version"}`.

## POST /topology/replace

Validates presence of `{"cus", "dus", "cells"}` — raises 422 if any missing.

Preserves `meta` from the current topology if the incoming payload omits it.

`write_topology(new_topo, updated_by="planning-api:apply")`.

Response:

```json
{
  "status": "ok",
  "cells_deployed": N,
  "dus_deployed":   N,
  "cus_deployed":   N,
  "topology_version": N
}
```

## POST /cells/add

`AddCellRequest` Pydantic model with these defaults:

| Field | Default |
|---|---|
| `generation` | `"5G"` |
| `band` | `"n78"` |
| `freq_mhz` | `3500` |
| `pci` | `0` |
| `vendor` | `"Nokia"` |
| `hardware_model` | `"AirScale MAA 64T64R"` |
| `antenna_config` | `"64T64R"` |
| `peak_dl_mbps` | `3800` |
| `tx_power_w` | `1000` |
| `idle_power_w` | `250` |
| `max_ues` | `900` |

- 409 if `cell_id` already in `topo["cells"]`.
- 404 if `du_id` not in `topo["dus"]`.
- **PCI auto-assign** when `pci == 0`: iterates `range(1, 1024)`, picks the smallest integer not in `{c["pci"] for c in topo["cells"].values()}`.
- Stored cell dict keys: `area`, `lat`, `lon`, `generation`, `band`, `freq_mhz`, `pci`, `vendor`, `hardware_model`, `antenna_config`, `peak_dl_mbps`, `tx_power_w`, `idle_power_w`, `max_ues`. (`cell_id` is the dict key, not stored inside the value.)
- Appends `cell_id` to `topo["dus"][du_id]["cell_ids"]`.
- `updated_by = "add_cell:<cell_id>"`.

Response: `{"status": "ok", "cell_id", "pci", "du_id", "topology_version"}`.

## DELETE /cells/{cell_id}

- 404 if not in topology.
- Removes `cell_id` from its DU's `cell_ids` (if found in any DU).
- Deletes `topo["cells"][cell_id]`.
- `updated_by = "remove_cell:<cell_id>"`.

Response: `{"status": "ok", "cell_id", "removed_from_du", "topology_version"}`.

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `INFLUX_URL` | `http://influxdb:8086` | KPI reads + topology event writes |
| `INFLUX_TOKEN` | `telecom-super-secret-auth-token-2026` | InfluxDB auth |
| `INFLUX_ORG` | `telecom` | InfluxDB org |
| `INFLUX_BUCKET` | `telecom_metrics` | KPI bucket |
| `TOPOLOGY_FILE` | `/config/topology.json` | Path to topology source of truth |

## Design decisions

- **File, not DB**: runtime topology lives in `topology.json`. Atomic write (`.tmp` → rename) prevents partial reads by simulator pollers. `version` field lets simulators detect staleness.
- **LLM never writes directly**: all mutations go through typed HTTP routes. The Controller is the only writer.
- **InfluxDB for time-series only**: topology is the Controller's domain; KPIs are InfluxDB's domain. The Controller merges them at query time with a `-5m` window (not persistent cache).
- **DU/CU resolution is query-time**: `du_id` is not stored inside the cell dict — it is derived by scanning `topo["dus"][*]["cell_ids"]` on each `/network` or `/cells` request. This keeps `topology.json` consistent after a move without updating the cell dict.
