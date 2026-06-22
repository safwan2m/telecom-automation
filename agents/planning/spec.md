# Planning Engine — Specification

FastAPI service on port 8081. Accepts deployment parameters, decides whether existing cells can satisfy demand or new infrastructure is required, then generates a complete conflict-free plan. All plan types return an identical schema. Plans are stored in-memory until applied.

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | 0.115.5 | HTTP service framework |
| `uvicorn` | 0.32.0 | ASGI server |
| `pydantic` | 2.10.3 | Request / response model validation |
| `httpx` | 0.27.2 | Synchronous HTTP calls to Controller |

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
      │  Decision:
      │    deployed_ues ≥ required_ues
      │        → Reorganize mode  (no new cells; adjust DU assignments + slices)
      │    otherwise
      │        → Deploy mode  (new cells required)
      │
      ├─────────────────────────────────┬─────────────────────────────────────
      │                                 │
      ▼                                 ▼
Reorganize mode                    Deploy mode
      │                                 │
      │ 1. Power optimization           ├─ Single-period  (POST /plan)
      │    compute optimal tx_power_w   │    Candidate pre-filter:
      │    per existing cell to meet    │      if area in MALLESWARAM_AREAS:
      │    coverage without SINR clash  │        keep CANDIDATE_CELLS within
      │                                 │        area.radius_km + 0.5 km of center
      │ 2. Load rebalancing             │      else: use all CANDIDATE_CELLS
      │    compute new DU assignments   │    heuristic: proximity-then-density score
      │    to equalise PRB across DUs   │
      │ 3. Slice reallocation           │ Cell selection output feeds into
      │    recompute PRB budgets per    │ the shared 8-step downstream pipeline:
      │    traffic_profile              │
      │                                 │  1. assign_pcis
      │ plan_type = "reorganize"        │  2. validate_plan
      │ is_new = false for all cells    │  3. assign_dus
      │ n_new_cells = 0                 │  4. assign_cus
      │                                 │  5. compute DU/CU centroids
      │                                 │  6. timing_sync_strategy
      │                                 │  7. allocate_slices (per cell)
      │                                 │  8. fronthaul / midhaul latency
      │                                 │
      │                                 │  plan_type = "deploy"
      │                                 │  is_new = true for selected cells
      │                                 │
      └─────────────────────────────────┘
                        │
                        ▼
Step 3 — plan_to_topology()
      converts plan to topology.json format; preserves all hardware fields

Step 4 — store in _plans[plan_id]; return Plan schema (see below)
```

## Plan schema

Both `reorganize` and `deploy` modes return this identical top-level structure:

```json
{
  "plan_id":          "string (8-char UUID)",
  "plan_type":        "reorganize" | "deploy",
  "planning_method":  "power_rebalance" | "heuristic",
  "timestamp":        "ISO-8601",
  "geographic_area":  "string",
  "timing_sync":      "IEEE-1588-PTP-Class-C" | "IEEE-1588-PTP-Class-B" | "SyncE",
  "pci_violations":   [],
  "cells": [
    {
      "cell_id", "area", "lat", "lon", "band", "freq_mhz", "max_ues",
      "generation", "vendor", "hardware_model", "antenna_config",
      "tx_power_w", "idle_power_w", "peak_dl_mbps", "pci",
      "du_id", "cu_id", "fronthaul_latency_us",
      "slices":         {"eMBB": {...}, "URLLC": {...}, "mMTC": {...}},
      "slice_warnings": [],
      "is_new":          true | false
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
    "n_dus":                     0,
    "n_cus":                     0,
    "total_capacity_ues":        0,
    "estimated_cost_usd":        0.0,
    "budget_utilisation_pct":    0.0,
    "placement_method":          "string",
    "sufficiency_analysis": {
      "required_ues":      0,
      "current_capacity":  0,
      "mode_chosen":       "reorganize" | "deploy"
    }
  }
}
```

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
- DU IDs: `DU-BLR-01`, `DU-BLR-02`, ... (sequential).

**`du_centroid(du_id, cell_ids, cell_map) → tuple[float, float]`**  
Mean (lat, lon) of member cells. Used for midhaul distance computation.

**`assign_cus(dus, cell_map, max_dus_per_cu=4) → dict[str, list[str]]`**  
Greedy geographic CU grouping over DUs. Returns `{cu_id: [du_ids]}`.
- Compute each DU's centroid (via `du_centroid`).
- Sort DU IDs alphabetically; each unassigned DU becomes a CU anchor.
- Other DUs within `MIDHAUL_RADIUS_KM` (25.0 km) join, up to `max_dus_per_cu`.
- CU IDs: `CU-BLR-01`, `CU-BLR-02`, ...

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

**`validate_plan(cells, pcis) → list[str]`**  
Returns violation strings for each adjacent pair:
- **Collision**: both cells have the same PCI.
- **Confusion**: cells have the same `pci % 3`.

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

**`_sufficiency_check(req, area_meta=None) → (dict, list[dict])`**  
Fetches live cells from `GET /network` on Controller.
```
area_km2     = _area_km2(geographic_area, area_meta)
required_ues = density × area_km2 × HOURLY_LOAD[peak_hour]
covering     = cells_covering_area(area_meta, all_live)   # if area_meta known
             = string-match filter on cell.area field       # fallback
deployed_ues = Σ max_ues for covering cells
mode         = "reorganize" if deployed_ues ≥ required_ues else "deploy"
```
Returns `(analysis_dict, live_cells)`. `live_cells` items include `_existing_pci` for reorganize.  
`analysis_dict` keys: `area_km2`, `required_ues`, `current_capacity`, `peak_hour`, `load_factor`, `mode_chosen`.

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
Returns `{cell_plans, du_plans, cu_plans, timing_sync, violations, du_cells}`.

### planner_api.py — planning logic

**`generate_plan(req) → dict`**  
Entry point. Calls `_resolve_area`, `_sufficiency_check`, then routes to `_reorganize_plan` or `_deploy_plan`.

**`_reorganize_plan(req, sa, live_cells, traffic, lat_constraints) → dict`**  
Rebalances DU/CU assignments and slice allocations for existing cells.  
Passes `keep_pcis = {cell_id: _existing_pci}` to `_run_pipeline` so PCIs are preserved.  
Returns plan with `plan_type="reorganize"`, `planning_method="power_rebalance"`, `is_new=False`.

**`_deploy_plan(req, sa, traffic, lat_constraints, area_meta=None) → dict`**  
Selects new candidate cells and builds full plan.
```
if area_meta:
    candidate_pool = CANDIDATE_CELLS within (area.radius_km + 0.5) km of area center
    (falls back to all CANDIDATE_CELLS if filter leaves none)
cells = select_cells(density, budget, bands, candidate_pool, area_center)
plan  = _run_pipeline(cells, ..., is_new=True)
cost  = estimate_cost(n_cells, n_dus, n_cus)
```
Returns plan with `plan_type="deploy"`, `planning_method="heuristic"`, `is_new=True`.

**`plan_to_topology(plan) → dict`**  
Converts a plan to `topology.json` format for `POST /topology/replace` on Controller.  
Structure: `{version:1, last_updated, updated_by, cus:{cu_id:{du_ids}}, dus:{du_id:{cu_id, cell_ids}}, cells:{cell_id:{area, pci, lat, lon, band, freq_mhz, max_ues, generation, vendor, hardware_model, antenna_config, tx_power_w, idle_power_w, peak_dl_mbps}}}`.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CONTROLLER_URL` | `http://controller:8080` | sufficiency analysis (`/network`) + `plan/apply` |

## Routes

```
GET  /health
GET  /areas                    list all Malleswaram sub-locality areas (MALLESWARAM_AREAS)
GET  /areas/{area_id}/cells    accepts area_id (e.g. "MLS-RWS") OR area name substring
                               (e.g. "Railway Station"); returns deployed cells covering
                               ≥ 20 % of the area circle with coverage_radius_km,
                               distance_to_area_km, area_coverage_fraction per cell
GET  /candidates               list all candidate cell sites with lat/lon and area

POST /plan
     Body: {geographic_area, expected_user_density, traffic_profile,
            spectrum_bands, latency_constraints, deployment_budget}
     Returns (missing fields):
       HTTP 200  {"status": "missing_fields", "missing": [...], "message": "..."}
     Returns (success):
       HTTP 200  Plan schema
         plan_type = "reorganize" if existing cells are sufficient
         plan_type = "deploy"     if new cells are required

GET  /plan/{plan_id}           retrieve stored plan by ID

POST /plan/apply
     Body: {plan_id}
     Action: calls POST /topology/replace on Controller with plan topology
     Returns: Controller's topology/replace response
```
