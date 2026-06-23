# Planning Engine — Specification

FastAPI service on port 8081. Accepts deployment parameters, analyses the live network, and generates a conflict-free plan. Five plan types are supported: `reorganize`, `deploy`, `suspend`, `reactivate`, `reactivate_and_deploy`. All share an identical top-level schema. Plans are persisted in InfluxDB.

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | 0.115.5 | HTTP service framework |
| `uvicorn` | 0.32.0 | ASGI server |
| `pydantic` | 2.10.3 | Request / response model validation |
| `httpx` | 0.27.2 | Synchronous HTTP calls to Controller |
| `influxdb-client` | 1.44.0 | Plan persistence (write + Flux query) |

## Planning flow

```
POST /plan
      │
      ▼
Step 1 — Input validation (strict — no defaults, no assumptions)
      │
      │  Required fields — all must be explicitly provided:
      │    geographic_area         target deployment zone (string)
      │    expected_user_density   users per km² (number)
      │    traffic_profile         {eMBB, URLLC, mMTC} fractions summing to 1.0
      │    spectrum_bands          licensed bands e.g. ["n78", "B3"]
      │    deployment_budget       CAPEX envelope (USD)
      │    latency_constraints     {e2e_ms, fronthaul_us}
      │
      │  Enforcement: server-side check at the top of /plan.
      │  If any required field is absent or null, the endpoint returns HTTP 200
      │  with a structured missing-fields response (NOT an error / HTTP 422):
      │
      │    {
      │      "status":  "missing_fields",
      │      "missing": ["<field_name>", ...],
      │      "message": "The following required fields were not provided: ..."
      │    }
      │
      │  The orchestrator receives this as a normal tool result, reads the
      │  "missing" list, and asks the operator for the absent values in the next
      │  conversation turn — no exception, no stack trace visible to the user.
      │  No server-side defaults are applied; fields that are missing stay missing.
      │
      │  See agents/orchestrator/spec.md § Tool schema design for the LLM-layer
      │  behaviour that ensures incomplete calls actually reach the server.
      │
      ▼
Step 2 — Sufficiency analysis  (query Controller /network for live state)
      │
      │  Area resolution (geographic_area string → area metadata):
      │    Search MALLESWARAM_AREAS for a case-insensitive substring match
      │    on name or area_id.
      │    If found:     area_km²   = π × area.radius_km²
      │    If not found: area_km²   = bounding-box of matching CANDIDATE_CELLS
      │                               (max_lat−min_lat) × (max_lon−min_lon) in km²
      │                               floor at 0.01 km²
      │
      │  Compute:
      │    required_ues   = expected_user_density × area_km²
      │                     × HOURLY_LOAD[traffic_profile.peak_hour]
      │    covering_cells = cells_covering_area(area, live_cells, min_fraction=0.20)
      │                     — deployed cells whose coverage circle overlaps
      │                       ≥ 20 % of the area circle
      │                     — uses coverage_radius_km(tx_power_w, freq_mhz) per cell
      │                     — falls back to all live cells if area not in MALLESWARAM_AREAS
      │    deployed_ues   = Σ max_ues  for cells in covering_cells
      │
      │  Split covering_cells by active flag:
      │    active_covering  = [c for c in covering_cells if c.active != false]
      │    deployed_ues     = Σ max_ues for cells in active_covering
      │
      │  Also query InfluxDB suspended cell registry for this area:
      │    suspended_cells = _get_suspended_cells(geographic_area)
      │    suspended_ues   = Σ max_ues for cells in suspended_cells
      │
      │  Decision (top-to-bottom, first match wins):
      │
      │    deployed_ues ≥ required_ues × SUSPENSION_RATIO (2.0)
      │    AND len(active_covering) > MIN_ACTIVE_CELLS (1)
      │        → Suspend mode   (demand dropped; park excess hardware)
      │
      │    deployed_ues ≥ required_ues
      │        → Reorganize mode  (sufficient capacity; rebalance only)
      │
      │    deployed_ues + suspended_ues ≥ required_ues
      │        → Reactivate mode  (wake suspended cells; no new hardware)
      │
      │    suspended_ues > 0  (but not enough alone)
      │        → Reactivate+Deploy mode  (wake all + deploy delta)
      │
      │    else
      │        → Deploy mode  (no suspended cells; new hardware required)
      │
      ├──────┬──────┬──────┬──────┬──────
      │      │      │      │      │
      ▼      ▼      ▼      ▼      ▼
   Suspend Reorg React  R+D   Deploy

Suspend mode
  Sort active_covering by max_ues desc.
  Greedily keep cells until Σ max_ues ≥ required_ues × MIN_CAPACITY_BUFFER (1.1).
  Always keep ≥ MIN_ACTIVE_CELLS (1) cell active.
  Remaining cells → suspended (write to InfluxDB suspended_cells; exclude from topology).
  Run _run_pipeline over kept cells only.
  plan_type = "suspend", planning_method = "suspension"
  suspended_cells[] in plan lists suspended cell_ids.

Reorganize mode
  Keep PCIs unchanged (keep_pcis = {cell_id: _existing_pci}).
  Run _run_pipeline over active_covering.
  plan_type = "reorganize", planning_method = "power_rebalance", n_new_cells = 0.

Reactivate mode
  Sort suspended_cells by max_ues desc.
  Greedily wake cells until deployed_ues + Σ reactivated ≥ required_ues.
  Write reactivation events to InfluxDB.
  Merge active_covering + reactivated → _run_pipeline, preserving PCIs.
  plan_type = "reactivate", planning_method = "reactivation"
  reactivated_cells[] in plan lists reactivated cell_ids.

Reactivate+Deploy mode
  Reactivate ALL suspended cells. Write reactivation events to InfluxDB.
  Compute remaining_deficit = required_ues − (deployed + suspended).
  Select new cells for deficit from CANDIDATE_CELLS (proximity pre-filter if area known).
  Merge active_covering + reactivated + new → _run_pipeline, fresh PCI assignment.
  Mark new cells is_new=true in plan.
  plan_type = "reactivate_and_deploy", planning_method = "reactivation_and_heuristic"

Deploy mode
  Candidate pre-filter: if area in MALLESWARAM_AREAS, restrict CANDIDATE_CELLS to within
    area.radius_km + 0.5 km of center; else use all.
  select_cells(density, budget, bands, candidate_pool, area_center) → scored + ranked.
  Run _run_pipeline (fresh PCI assignment), is_new=true.
  plan_type = "deploy", planning_method = "heuristic"

All modes feed into:
  _run_pipeline → 1. assign_pcis / keep_pcis  2. validate_plan  3. assign_dus
                  4. assign_cus  5. DU/CU centroids  6. timing_sync_strategy
                  7. allocate_slices per cell  8. fronthaul/midhaul latency

      └─────────────────────────────────┘
                        │
                        ▼
Step 3 — plan_to_topology()
      converts plan to topology.json format; preserves all hardware fields

Step 4 — _store_plan(plan): write to session cache (_plans dict) AND persist to InfluxDB
           measurement "plans" | tags: plan_id, plan_type | fields: plan_json (full JSON),
           geographic_area, planning_method | timestamp: now
           InfluxDB write failure is logged as WARNING; plan still returned to caller.

Step 5 — return Plan schema (see below)
```

## Plan schema

All plan types share this top-level structure:

```json
{
  "plan_id":          "string (8-char UUID)",
  "plan_type":        "reorganize" | "deploy" | "suspend" | "reactivate" | "reactivate_and_deploy",
  "planning_method":  "power_rebalance" | "heuristic" | "suspension" | "reactivation" | "reactivation_and_heuristic",
  "timestamp":        "ISO-8601",
  "geographic_area":  "string",
  "timing_sync":      "IEEE-1588-PTP-Class-C" | "IEEE-1588-PTP-Class-B" | "SyncE",
  "pci_violations":   [],

  "suspended_cells":   ["cell_id", ...],   // present only in suspend plans
  "reactivated_cells": ["cell_id", ...],   // present in reactivate / reactivate_and_deploy

  "cells": [
    {
      "cell_id", "area", "lat", "lon", "band", "freq_mhz", "max_ues",
      "generation", "vendor", "hardware_model", "antenna_config",
      "tx_power_w", "idle_power_w", "peak_dl_mbps", "pci",
      "du_id", "cu_id", "fronthaul_latency_us",
      "active":         true | false,      // false = suspended; absent means true
      "slices":         {"eMBB": {...}, "URLLC": {...}, "mMTC": {...}},
      "slice_warnings": [],
      "is_new":         true | false,
      "suspended_reason": "demand_reduction"   // present only on suspended entries
    }
  ],
  "dus": [
    {"du_id", "cu_id", "cell_ids", "centroid_lat", "centroid_lon", "midhaul_latency_ms"}
  ],
  "cus": [
    {"cu_id", "du_ids", "centroid_lat", "centroid_lon"}
  ],
  "summary": {
    "n_cells":                   0,
    "n_new_cells":               0,
    "n_cells_kept_active":       0,   // suspend plans only
    "n_cells_suspended":         0,   // suspend plans only
    "n_reactivated_cells":       0,   // reactivate / reactivate_and_deploy plans only
    "n_dus":                     0,
    "n_cus":                     0,
    "total_capacity_ues":        0,
    "estimated_cost_usd":        0.0,
    "budget_utilisation_pct":    0.0,
    "placement_method":          "string",
    "sufficiency_analysis": {
      "required_ues":       0,
      "active_capacity":    0,   // Σ max_ues of active covering cells
      "suspended_capacity": 0,   // Σ max_ues of suspended cells in area
      "mode_chosen":        "reorganize" | "deploy" | "suspend" | "reactivate" | "reactivate_and_deploy"
    }
  }
}
```

**Notes on suspended cells in plans:**
- Suspended cells appear in `plan["cells"]` with `active: false` for transparency, but `plan_to_topology` filters them out before writing to the Controller.
- `dus[]` and `cus[]` only cover active cells (the pipeline runs only over kept/active cells).
- Suspended cells are stored in InfluxDB `suspended_cells` measurement with their full hardware spec so they can be reconstructed by a future reactivate plan.

## File structure

| File | Purpose |
|---|---|
| `planner_api.py` | FastAPI service — input validation, sufficiency analysis, route handlers |
| `placement.py` | `select_cells` (heuristic), `assign_dus`, `assign_cus`, `du_centroid`, coverage helpers, `CANDIDATE_CELLS`, `MALLESWARAM_AREAS` |
| `pci_planner.py` | `build_adjacency`, `assign_pcis` (graph-coloring), `validate_plan` |
| `slice_allocator.py` | `allocate` (PRB budget per slice), `timing_sync_strategy` |

## Malleswaram area inventory

12 named geographic zones covering Malleswaram (PIN 560003). These are independent of any deployment — an area may have 0, 1, or several cells already covering it. Each area is defined by a center lat/lon and a radius that approximates the extent of the sub-locality.

`MALLESWARAM_AREAS` in `placement.py` is the authoritative list.

| area_id | name | lat | lon | radius_km | Notes |
|---|---|---|---|---|---|
| `MLS-RWS` | Malleswaram Railway Station | 13.0127 | 77.5707 | 0.40 | NE; commuter hub, highest footfall |
| `MLS-KMT` | Kadu Malleshwara Temple | 13.0097 | 77.5718 | 0.30 | Heritage core; 2nd Temple St |
| `MLS-BEL` | BEL Road | 13.0110 | 77.5632 | 0.35 | NW arterial; IISc-adjacent |
| `MLS-18C` | Malleswaram 18th Cross | 13.0080 | 77.5663 | 0.30 | N; Sampige Road corridor |
| `MLS-SNK` | Shankar Mutt Road | 13.0062 | 77.5742 | 0.30 | Central-east; Margosa Road side |
| `MLS-MGR` | Margosa Road Central | 13.0055 | 77.5692 | 0.30 | Central arterial |
| `MLS-10C` | 10th Cross | 13.0040 | 77.5707 | 0.30 | Central; 8th–10th Cross strip |
| `MLS-CIR` | Malleswaram Circle | 13.0022 | 77.5718 | 0.30 | South commercial hub |
| `MLS-SPG` | Sampige Road South | 13.0025 | 77.5660 | 0.30 | S; near Mantri Square |
| `MLS-3MN` | 3rd Main Road | 13.0012 | 77.5598 | 0.30 | SW; residential grid / Kodandarampura |
| `MLS-6CR` | 6th Cross Road | 12.9968 | 77.5638 | 0.30 | S; 6th Cross residential |
| `MLS-CHD` | Chowdaiah Road | 12.9932 | 77.5562 | 0.35 | Southernmost; Chowdaiah Memorial |

**Bounding box:** N 13.013 / S 12.993 / E 77.576 / W 77.556 — covers ≈ 2.4 km × 1.8 km.

## Coverage-based area query

`cells_covering_area(area, live_cells, min_fraction=0.20)` in `placement.py` returns the subset of deployed cells that cover **at least 20 % of the area circle**.

```
For each live cell:
  1. coverage_radius_km  ← COST-231-Hata(tx_power_w, freq_mhz)
       tx_power_dbm  = 10·log10(tx_power_w · 1000)          [W → dBm]
       EIRP_dbm      = tx_power_dbm + tx_gain_dbi            [default 18 dBi, 64T64R]
       max_path_loss = EIRP_dbm − rx_threshold_dbm           [default −100 dBm]
       solve  L(d) = max_path_loss  for d  (COST-231-Hata urban macro)

  2. d = haversine(cell.lat, cell.lon, area.lat, area.lon)

  3. fraction = circle_overlap_fraction(d, r_area=area.radius_km,
                                            r_cell=coverage_radius_km)

       Two-circle intersection area  A_int :
         if d ≥ r_cell + r_area          → A_int = 0
         if d + r_area ≤ r_cell          → A_int = π·r_area²   (area fully inside cell)
         if d + r_cell ≤ r_area          → A_int = π·r_cell²   (cell fully inside area)
         otherwise (partial overlap):
           α  = arccos((d²+r_area²−r_cell²) / (2·d·r_area))   [half-angle at area centre]
           β  = arccos((d²+r_cell²−r_area²) / (2·d·r_cell))   [half-angle at cell centre]
           A_int = r_area²·α + r_cell²·β
                   − 0.5·√[(−d+r_area+r_cell)(d+r_area−r_cell)
                             (d−r_area+r_cell)(d+r_area+r_cell)]

       fraction = A_int / (π·r_area²)

  4. include cell if fraction ≥ 0.20
```

Each result entry adds `coverage_radius_km`, `distance_to_area_km`, and `area_coverage_fraction` to the cell dict.

## Candidate cell inventory

10 macro tower candidate sites — physical locations where a cell is already deployed or can be deployed. Distinct from `MALLESWARAM_AREAS` (which are demand zones). The `area` field names the sub-locality the tower sits in; `_area_matches()` uses substring matching so `geographic_area = "Malleswaram"` matches all of them.

`CANDIDATE_CELLS` in `placement.py` is the authoritative list.

| cell_id | area (sub-locality) | lat | lon | density_weight |
|---|---|---|---|---|
| `MLS_RWS_01` | Malleswaram Railway Station | 13.0080 | 77.5760 | 1.5 |
| `MLS_18C_01` | Malleswaram 18th Cross | 13.0030 | 77.5670 | 1.4 |
| `MLS_SPG_01` | Sampige Road South | 12.9990 | 77.5700 | 1.3 |
| `MLS_BEL_01` | BEL Road | 13.0110 | 77.5630 | 1.1 |
| `MLS_SNK_01` | Shankar Mutt Road | 13.0060 | 77.5740 | 1.2 |
| `MLS_3MN_01` | 3rd Main Road | 13.0010 | 77.5600 | 1.2 |
| `MLS_MGR_01` | Margosa Road | 12.9960 | 77.5640 | 1.0 |
| `MLS_CHD_01` | Chowdaiah Road | 12.9930 | 77.5560 | 0.9 |
| `MLS_10C_01` | 10th Cross | 13.0040 | 77.5710 | 1.3 |
| `MLS_6CR_01` | 6th Cross Road | 12.9970 | 77.5580 | 1.0 |

All candidates share: `band="n78"`, `freq_mhz=3500`, `max_ues=900`, `generation="5G"`.  
Hardware is assigned by index cycling through Nokia → Ericsson → Samsung → ZTE:

| Vendor | hardware_model | tx_power_w | idle_power_w | peak_dl_mbps |
|---|---|---|---|---|
| Nokia | AirScale MAA 64T64R | 1000 | 250 | 3800 |
| Ericsson | AIR 6449 | 950 | 240 | 3600 |
| Samsung | TM500 64T64R | 900 | 225 | 3400 |
| ZTE | AAU 5614 | 1000 | 250 | 3200 |

All use `antenna_config="64T64R"`.

## Propagation models

| Model | Used by | Notes |
|---|---|---|
| COST-231-Hata | DU/CU simulators, Map Server, `coverage_radius_km()` | Coverage radius estimation — urban macro empirical |

## Implementation reference

### placement.py — constants

| Constant | Value | Purpose |
|---|---|---|
| `COST_PER_CELL_USD` | 50,000 | Cell CAPEX for cost estimation |
| `COST_PER_DU_USD` | 30,000 | DU CAPEX |
| `COST_PER_CU_USD` | 80,000 | CU CAPEX |
| `FRONTHAUL_RADIUS_KM` | 5.0 | Max distance cell→DU anchor for grouping |
| `MIDHAUL_RADIUS_KM` | 25.0 | Max distance DU→CU anchor for grouping |
| `MIN_AREA_COVERAGE_FRACTION` | 0.20 | Minimum overlap fraction to count a cell as covering an area |

### placement.py — functions

**`haversine_km(lat1, lon1, lat2, lon2) → float`**  
Great-circle distance in km. Uses `R=6371`, Haversine formula: `2R·arcsin(√(sin²(Δlat/2) + cos(lat1)·cos(lat2)·sin²(Δlon/2)))`.

**`coverage_radius_km(tx_power_w, freq_mhz, tx_gain_dbi=18.0, rx_threshold_dbm=-100.0, h_tx_m=25.0, h_rx_m=1.5) → float`**  
COST-231-Hata urban macro coverage radius (km).
```
tx_power_dbm  = 10·log10(tx_power_w × 1000)
max_path_loss = tx_power_dbm + tx_gain_dbi − rx_threshold_dbm
a_hrx = 3.2·(log10(11.75·h_rx_m))² − 4.97     [large city, f > 300 MHz]
C_m   = 3.0                                      [metropolitan correction]
A = 46.3 + 33.9·log10(f) − 13.82·log10(h_tx) − a_hrx + C_m
B = 44.9 − 6.55·log10(h_tx)
d = 10^((max_path_loss − A) / B)   [km; floor 0.05]
```

**`circle_overlap_fraction(d, r_area, r_cell) → float`**  
Fraction of area circle (r_area) covered by cell circle (r_cell), centers distance d apart.  
See § Coverage-based area query for the full formula. Returns value in [0, 1].

**`cells_covering_area(area, live_cells, min_fraction=0.20) → list[dict]`**  
Filters `live_cells` to those whose coverage circle overlaps ≥ `min_fraction` of `area`. Enriches each result dict with `coverage_radius_km`, `distance_to_area_km`, `area_coverage_fraction`.

**`select_cells(user_density, budget, spectrum_bands, candidate_pool=None, area_center=None) → list[dict]`**  
Heuristic cell selection. `candidate_pool` defaults to all `CANDIDATE_CELLS`.
```
max_cells = max(1, int(budget × 0.6 / (COST_PER_CELL_USD + COST_PER_DU_USD)))
score(c) = density_weight × band_bonus × (max_ues/300) × (user_density/500)
           × (1 + 1/(1 + dist_km))   [proximity bonus, only if area_center given]
band_bonus = 1.5 if c.band in spectrum_bands else 0.6
```
Returns top `max_cells` candidates by descending score.

**`assign_dus(cells, max_cells_per_du=3) → dict[str, list[str]]`**  
Greedy geographic DU grouping. Returns `{du_id: [cell_ids]}`.
- Sort cells by `density_weight` descending; each unassigned cell becomes a DU anchor.
- Other unassigned cells within `FRONTHAUL_RADIUS_KM` (5.0 km) join, up to `max_cells_per_du`.
- DU IDs: `DU-BLR-01`, `DU-BLR-02`, ... (sequential, abstract planning IDs).
- **Note**: these abstract IDs are remapped to existing infrastructure IDs by `_remap_infrastructure_ids()` before any live topology write.

**`du_centroid(du_id, cell_ids, cell_map) → tuple[float, float]`**  
Mean (lat, lon) of member cells. Used for midhaul distance computation.

**`assign_cus(dus, cell_map, max_dus_per_cu=4) → dict[str, list[str]]`**  
Greedy geographic CU grouping over DUs. Returns `{cu_id: [du_ids]}`.
- Compute each DU's centroid (via `du_centroid`).
- Sort DU IDs alphabetically; each unassigned DU becomes a CU anchor.
- Other DUs within `MIDHAUL_RADIUS_KM` (25.0 km) join, up to `max_dus_per_cu`.
- CU IDs: `CU-BLR-01`, `CU-BLR-02`, ... (abstract planning IDs; remapped by `_remap_infrastructure_ids()` before live write).

**`estimate_cost(n_cells, n_dus, n_cus) → float`**  
`n_cells × 50,000 + n_dus × 30,000 + n_cus × 80,000` (USD).

**`fronthaul_latency_us(cell, du_centroid_pos) → float`**  
`haversine(cell, du_centroid) × 5.0 + 10.0` µs. (5 µs/km fiber propagation + 10 µs processing.)

**`midhaul_latency_ms(du_pos, cu_pos) → float`**  
`haversine(du, cu) × 0.01 + 0.5` ms. (0.01 ms/km + 0.5 ms processing.)

---

### pci_planner.py — constants & functions

| Constant | Value | Meaning |
|---|---|---|
| `ADJACENCY_RADIUS_KM` | 3.0 | Cells within this range are RF neighbours |
| `PCI_MAX` | 1007 | 3GPP TS 38.211 PCI range 0–1007 |

**`build_adjacency(cells) → dict[str, set[str]]`**  
Returns `{cell_id: {neighbour_ids}}` for all pairs within `ADJACENCY_RADIUS_KM`.

**`assign_pcis(cells) → dict[str, int]`**  
Greedy graph-colouring PCI assignment. Returns `{cell_id: pci}`.
- Sort cells by neighbour degree descending (most-constrained first).
- For each cell: find the smallest `pci ∈ [0, 1007]` not in neighbour PCIs AND not sharing `pci % 3` with any neighbour PCI.
- Guarantees collision-free; minimises confusion pairs.

**`validate_plan(cells, pcis) → dict[str, list[str]]`**  
Returns `{"collisions": [...], "confusions": [...]}` for adjacent pairs:
- **Collision** (forbidden): both cells have the same PCI — always 0 after `assign_pcis`.
- **Confusion** (advisory): cells share `pci % 3` — unavoidable in dense networks; logged but not an error.

---

### slice_allocator.py — constants & functions

| Constant | Value |
|---|---|
| `MIN_PRB` | `{eMBB: 0.10, URLLC: 0.05, mMTC: 0.02}` |
| `LATENCY_TARGET_MS` | `{eMBB: 30.0, URLLC: 1.0, mMTC: 100.0}` |

**`allocate(traffic_profile, max_ues, latency_constraints) → dict`**  
Computes PRB fractions and UE capacity per slice for one cell.
```
1. Normalize fractions: norm[s] = traffic_profile[s] / Σ fractions
2. Apply floor:         prb[s]  = max(norm[s], MIN_PRB[s])
3. Re-normalize:        prb[s] /= Σ prb           → sums to 1.0
4. UE capacity:         ue_cap[s] = round(prb[s] × max_ues)
5. Warning if e2e_ms ≤ 1.0 and prb[URLLC] < 0.15
```
Returns `{"slices": {eMBB: {prb_fraction, max_ues, latency_target_ms}, ...}, "warnings": [...]}`.

**`timing_sync_strategy(latency_constraints, spectrum_bands) → str`**  
Selects timing sync scheme. TDD bands: `{n78, n41, n257, n258, n260, n261}`.
```
fronthaul_us ≤ 50  OR has TDD band  →  "IEEE-1588-PTP-Class-C"  (±100 ns, TDD required)
fronthaul_us ≤ 200                  →  "IEEE-1588-PTP-Class-B"  (±1 µs)
else                                →  "SyncE"                   (±4.6 ppm, FDD-only)
```

---

### planner_api.py — Pydantic models

```python
class TrafficProfile(BaseModel):
    eMBB: float = 0.70; URLLC: float = 0.20; mMTC: float = 0.10; peak_hour: int = 19

class LatencyConstraints(BaseModel):
    e2e_ms: float = 10.0; fronthaul_us: float = 100.0

class PlanRequest(BaseModel):
    # All Optional[...] = None — triggers missing-fields check when absent
    geographic_area: str | None = None
    expected_user_density: float | None = None
    traffic_profile: TrafficProfile | None = None
    spectrum_bands: list[str] | None = None
    latency_constraints: LatencyConstraints | None = None
    deployment_budget: float | None = None
    # Optional with defaults — not checked by missing-fields logic
    max_cells_per_du: int = 3
    max_dus_per_cu: int = 4
```

### planner_api.py — constants

```python
HOURLY_LOAD = [          # index = hour 0–23, matches core_simulator.py
    0.08, 0.06, 0.05, 0.05, 0.06, 0.12,   # 00–05 (night)
    0.30, 0.65, 0.85, 0.80, 0.70, 0.65,   # 06–11 (morning ramp)
    0.65, 0.60, 0.62, 0.68, 0.78, 0.90,   # 12–17 (day)
    0.95, 1.00, 0.97, 0.88, 0.62, 0.30,   # 18–23 (peak evening)
]

SUSPENSION_RATIO    = 2.0   # suspend when deployed_ues ≥ required_ues × 2.0
MIN_ACTIVE_CELLS    = 1     # always keep at least 1 cell active in the area
MIN_CAPACITY_BUFFER = 1.1   # keep 10 % headroom above required_ues when suspending
```

### planner_api.py — helper functions

**`_resolve_area(geographic_area) → dict | None`**  
Resolves a free-text string to a `MALLESWARAM_AREAS` entry.  
Tries (in order): exact `area_id` match, query-in-name, name-in-query, query-in-area_id. Case-insensitive.

**`_area_km2(geographic_area, area_meta=None) → float`**  
If `area_meta` given: `π × radius_km²`.  
Fallback: bounding box of substring-matched CANDIDATE_CELLS → `lat_km × lon_km`, floor 0.01.

**`_area_matches(cell_area, target) → bool`**  
`target.lower() in cell_area.lower() or cell_area.lower() in target.lower()`.

**`_missing_fields_response(req, required) → dict | None`**  
Returns `{"status": "missing_fields", "missing": [...], "message": "..."}` if any field in `required` is `None` on `req`, else `None`.

**`_get_influx() → InfluxDBClient`**  
Lazy singleton. Creates `InfluxDBClient(INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG)` on first call.

**`_write_plan(plan) → None`**  
Writes plan to InfluxDB measurement `plans`. Tags: `plan_id`, `plan_type`. Fields: `plan_json` (full JSON string), `geographic_area`, `planning_method`. Logs WARNING on failure; never raises.

**`_read_plan(plan_id) → dict | None`**  
Flux query: `range(-90d)`, filter `_measurement=="plans"`, `plan_id==plan_id`, `_field=="plan_json"`, `last()`. Deserializes the JSON string. Returns `None` on miss or error.

**`_store_plan(plan) → None`**  
Writes to `_plans[plan_id]` cache AND calls `_write_plan(plan)`.

**`_fetch_plan(plan_id) → dict | None`**  
Returns `_plans[plan_id]` if in cache; else calls `_read_plan`, warms cache on hit, returns `None` on miss.

**`_write_suspension_events(cells, geographic_area, action, plan_id) → None`**  
Writes one InfluxDB point per cell to `suspended_cells` measurement. `action` = `"suspended"` or `"reactivated"`. Strips internal/computed fields; stores `pci` from `_existing_pci`. Never raises — logs WARNING on failure.

**`_get_suspended_cells(geographic_area) → list[dict]`**  
Flux query: `range(-36500d)`, filter `_measurement=="suspended_cells"`, `geographic_area==<area>`, `_field=="cell_json"`, `group(cell_id)`, `last()`. Parses `cell_json` and returns cells where `action == "suspended"`. Returns `[]` on error.

**`_sufficiency_check(req, area_meta=None) → (dict, list[dict], list[dict])`**  
Fetches live cells from `GET /network` on Controller.
```
active_covering  = covering cells where active != false
deployed_ues     = Σ max_ues for active_covering
suspended_cells  = _get_suspended_cells(geographic_area)
suspended_ues    = Σ max_ues for suspended_cells

mode:
  deployed_ues ≥ required_ues × SUSPENSION_RATIO and n_active > MIN_ACTIVE_CELLS → "suspend"
  deployed_ues ≥ required_ues                                                      → "reorganize"
  deployed_ues + suspended_ues ≥ required_ues                                      → "reactivate"
  suspended_ues > 0                                                                 → "reactivate_and_deploy"
  else                                                                              → "deploy"
```
Returns `(analysis_dict, active_covering, suspended_cells)`.  
`analysis_dict` keys: `area_km2`, `required_ues`, `active_capacity`, `suspended_capacity`, `peak_hour`, `load_factor`, `mode_chosen`.

**`_run_pipeline(cells, traffic, lat_constraints, spectrum_bands, max_cells_per_du, max_dus_per_cu, is_new=True, keep_pcis=None) → dict`**  
Shared downstream pipeline run for both reorganize and deploy modes.
```
pcis        = keep_pcis (reorganize) or assign_pcis(cells) (deploy)
violations  = validate_plan(cells, pcis)
du_cells    = assign_dus(cells, max_cells_per_du)
cu_dus      = assign_cus(du_cells, cell_map, max_dus_per_cu)
du_cents    = {du_id: du_centroid(...)}
cu_cents    = {cu_id: mean(du centroids)}
timing_sync = timing_sync_strategy(lat_constraints, spectrum_bands)
per cell    → pci, du_id, cu_id, fronthaul_latency_us, slices (allocate), is_new
per DU      → cu_id, cell_ids, centroid_lat/lon, midhaul_latency_ms
per CU      → du_ids, centroid_lat/lon
```
Returns `{cell_plans, du_plans, cu_plans, timing_sync, violations, pci_confusions, du_cells}`.

### planner_api.py — planning logic

**`generate_plan(req) → dict`**  
Entry point. Calls `_resolve_area`, `_sufficiency_check`, then routes to one of the five plan functions based on `sa["mode_chosen"]`.

**`_reorganize_plan(req, sa, active_covering, traffic, lat_constraints) → dict`**  
Rebalances DU/CU assignments and slice allocations for existing active cells.  
Passes `keep_pcis = {cell_id: _existing_pci}` to `_run_pipeline` so PCIs are preserved.  
Returns plan with `plan_type="reorganize"`, `planning_method="power_rebalance"`, `n_new_cells=0`.

**`_deploy_plan(req, sa, traffic, lat_constraints, area_meta=None) → dict`**  
Selects new candidate cells and builds full plan.
```
candidate_pool = CANDIDATE_CELLS within (area.radius_km + 0.5) km of area center (if known)
cells = select_cells(density, budget, bands, candidate_pool, area_center)
plan  = _run_pipeline(cells, ..., is_new=True, keep_pcis=None)
cost  = estimate_cost(n_cells, n_dus, n_cus)
```
Returns plan with `plan_type="deploy"`, `planning_method="heuristic"`, `is_new=True`.

**`_suspend_plan(req, sa, active_covering, traffic, lat_constraints) → dict`**  
Sorts active_covering by max_ues descending. Keeps cells until Σ max_ues ≥ `required_ues × MIN_CAPACITY_BUFFER`, always keeping ≥ `MIN_ACTIVE_CELLS`. Calls `_write_suspension_events(to_suspend, ..., "suspended")`. Runs `_run_pipeline` over kept cells only with existing PCIs. Suspended cells are appended to `plan["cells"]` with `active=False, suspended_reason="demand_reduction"` for transparency, but `plan_to_topology` excludes them from the topology written to the Controller.

**`_reactivate_plan(req, sa, active_covering, suspended_cells, traffic, lat_constraints) → dict`**  
Sorts suspended_cells by max_ues descending. Greedily selects cells to reactivate until `deployed_ues + Σ reactivated ≥ required_ues`. Calls `_write_suspension_events(to_reactivate, ..., "reactivated")`. Merges active_covering + cleaned reactivated cells → `_run_pipeline` with `keep_pcis = {cell_id: _existing_pci or stored pci}`.  
Returns plan with `plan_type="reactivate"`, `reactivated_cells=[...]`, `estimated_cost=0`.

**`_reactivate_and_deploy_plan(req, sa, active_covering, suspended_cells, traffic, lat_constraints, area_meta) → dict`**  
Reactivates ALL suspended cells (writes events). Computes `remaining_deficit = required_ues − (active + suspended)`. Runs `select_cells` for deficit against filtered CANDIDATE_CELLS (excluding already-active and reactivated cell_ids). Merges all three cell groups → `_run_pipeline` with `keep_pcis=None` (fresh PCI assignment). Post-processes to set `is_new=True` on newly selected cells.  
Returns plan with `plan_type="reactivate_and_deploy"`, `reactivated_cells=[...]`, `n_new_cells=N`.

**`plan_to_topology(plan) → dict`**  
Converts a plan to `topology.json` format for `POST /topology/replace` on Controller.  
Filters `plan["cells"]` to only include entries where `c.get("active", True)` is truthy — suspended cells (active=False) are excluded.  
Structure: `{version:1, last_updated, updated_by, cus:{cu_id:{du_ids}}, dus:{du_id:{cu_id, cell_ids}}, cells:{cell_id:{area, pci, lat, lon, band, freq_mhz, max_ues, generation, vendor, hardware_model, antenna_config, tx_power_w, idle_power_w, peak_dl_mbps}}}`.  
**Important**: call `_remap_infrastructure_ids()` on the result before applying to a live deployment so that existing DU/CU hardware IDs are preserved.

**`_remap_infrastructure_ids(plan_topology, plan, existing_topo) → dict`**  
Merges a plan into the existing topology, preserving infrastructure IDs and all non-plan cells.

A plan frequently covers only a subset of the live network (cells in the queried area). Replacing the whole topology with the plan would delete cells not covered by the plan and overwrite hardware IDs (`DU-MLS-1` etc.) with abstract planner IDs (`DU-BLR-01` etc.).

Algorithm:
1. If `existing_topo` has no DUs, return `plan_topology` unchanged (fresh deployment).
2. Start from `deepcopy(existing_topo)` as the base.
3. Compute Haversine centroids for all existing DUs from their cell lat/lons.
4. For each plan DU, compute its centroid from `plan_topology["cells"]` and map it to the nearest existing DU.
5. Remove plan cells (active + suspended) from whichever existing DUs currently hold them.
6. Remove cells in `plan["suspended_cells"]` from `merged["cells"]`.
7. Upsert active plan cell configs into `merged["cells"]`.
8. Append each plan DU's cells to its mapped existing DU's `cell_ids`.
9. Drop DUs with empty `cell_ids`; rebuild each CU's `du_ids` list from DU `cu_id` assignments; drop CUs with no DUs.
10. Return merged topology.

Called in `apply_plan()` immediately after `plan_to_topology()`, fetching the current topology via `GET /topology` on the Controller (5 s timeout). On fetch failure the merge is skipped with a WARNING log and the raw plan topology is applied as-is.

## Plan persistence

Plans are written to InfluxDB measurement `plans` on creation and read back on `GET /plan/{id}` cache miss.

| InfluxDB field | Type | Content |
|---|---|---|
| `plan_json` | string field | Full plan JSON (serialized) |
| `geographic_area` | string field | Area name (indexed for list queries) |
| `planning_method` | string field | e.g. `power_rebalance`, `heuristic`, `suspension` |
| `plan_id` | tag | 8-char UUID — used for Flux filter |
| `plan_type` | tag | `reorganize` \| `deploy` \| `suspend` \| `reactivate` \| `reactivate_and_deploy` |

**Retrieval strategy** (`_fetch_plan`): check in-memory `_plans` session cache first; on miss, run Flux query (`range -90d`, filter by `plan_id` tag, `plan_json` field, `last()`), deserialize, warm cache.  
**Failure behaviour**: InfluxDB write/read failures are logged at WARNING level and do not fail the HTTP request. Plans created in the current session are always retrievable via the session cache even if InfluxDB is down.

## Suspended cell registry

Suspended cells are tracked in InfluxDB measurement `suspended_cells` so their hardware specs survive process restarts and are visible across reactivate plans.

Each write = one lifecycle event for one cell.

| Column | Kind | Value |
|---|---|---|
| `cell_id` | tag | e.g. `MLS_RWS_01` |
| `geographic_area` | tag | matches `PlanRequest.geographic_area` |
| `cell_json` | field (string) | Full cell dict JSON, including `action` (`"suspended"` or `"reactivated"`), `pci`, all hardware fields, and `plan_id`. Keys stripped before storage: `_existing_pci` (stored as `pci`), `active`, `coverage_radius_km`, `distance_to_area_km`, `area_coverage_fraction`, `slices`, `slice_warnings`, `is_new`, `du_id`, `cu_id`, `fronthaul_latency_us`. |
| timestamp | InfluxDB time | when the event occurred |

**Query for currently suspended cells in an area** — group by `cell_id`, take last event per cell, keep only those with `action == "suspended"`:
```flux
from(bucket: "telecom_metrics")
  |> range(start: -36500d)
  |> filter(fn: (r) => r._measurement == "suspended_cells")
  |> filter(fn: (r) => r.geographic_area == "<area>")
  |> filter(fn: (r) => r._field == "cell_json")
  |> group(columns: ["cell_id"])
  |> last()
  // action == "suspended" vs "reactivated" checked in Python after parsing cell_json
```

**Failure behaviour**: `_get_suspended_cells` returns `[]` on InfluxDB error — treated as "no suspended cells", which causes the planner to fall through to deploy mode (safe but not optimal). `_write_suspension_events` logs WARNING and never raises, so suspension events that fail to write will not be tracked across restarts.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CONTROLLER_URL` | `http://controller:8080` | sufficiency analysis (`/network`) + `plan/apply` |
| `INFLUX_URL` | `http://influxdb:8086` | Plan persistence + suspended cell registry |
| `INFLUX_TOKEN` | `telecom-super-secret-auth-token-2026` | InfluxDB auth |
| `INFLUX_ORG` | `telecom` | InfluxDB organisation |
| `INFLUX_BUCKET` | `telecom_metrics` | Bucket for `plans` and `suspended_cells` measurements |
| `SUSPENSION_RATIO` | `2.0` | Trigger threshold: suspend cells when `deployed_ues ≥ required_ues × SUSPENSION_RATIO` |

## Routes

```
GET  /health                   {"status": "ok", "influxdb": true|false}
GET  /areas                    list all Malleswaram sub-locality areas (MALLESWARAM_AREAS)
GET  /areas/{area_id}/cells    accepts area_id (e.g. "MLS-RWS") OR area name substring
                               (e.g. "Railway Station"); returns deployed cells covering
                               ≥ 20 % of the area circle with coverage_radius_km,
                               distance_to_area_km, area_coverage_fraction per cell
GET  /candidates               list all candidate cell sites with lat/lon and area

GET  /cells/suspended?area=    list currently suspended cells (all areas if ?area omitted)
                               Returns: {"suspended_cells": [...], "count": N}
                               Each entry: full cell dict + action + plan_id that suspended it

POST /plan
     Body: {geographic_area, expected_user_density, traffic_profile,
            spectrum_bands, latency_constraints, deployment_budget}
     Returns (missing fields):
       HTTP 200  {"status": "missing_fields", "missing": [...], "message": "..."}
     Returns (success):
       HTTP 200  Plan schema
         plan_type = "reorganize"           existing capacity sufficient; rebalance only
         plan_type = "deploy"               new cells required; no suspended cells available
         plan_type = "suspend"              excess capacity; park lowest-priority cells
         plan_type = "reactivate"           suspended cells can fill demand gap
         plan_type = "reactivate_and_deploy" partial reactivation + new cell deployment
     Side-effect: plan written to InfluxDB measurement "plans";
                  suspend/reactivate events written to "suspended_cells"

GET  /plans                    list persisted plans (last 90 days) sorted newest-first;
                               falls back to session cache on InfluxDB error
                               Returns: {"plans": [{plan_id, plan_type, geographic_area,
                               timestamp}], "count": N}

GET  /plan/{plan_id}           session cache first, then InfluxDB Flux query

POST /plan/apply
     Body: {plan_id}
     Action: 1. plan_to_topology(plan) — convert plan to topology.json format.
             2. _remap_infrastructure_ids(plan_topology, plan, GET /topology) —
                merge plan changes into the existing topology: maps abstract planner
                DU/CU IDs to existing hardware IDs, keeps all non-plan cells intact,
                removes suspended cells.  Skipped if Controller is unreachable
                (WARNING logged; raw plan topology applied as-is).
             3. POST /topology/replace — write merged topology to Controller.
             Suspended cells (active=false) are removed from the merged topology and
             live only in InfluxDB until a reactivate plan restores them.
     Returns: Controller's topology/replace response
```
