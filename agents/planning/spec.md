# Planning Engine — Specification

FastAPI service on port 8081. Accepts deployment parameters, decides whether existing cells can satisfy demand or new infrastructure is required, then generates a complete conflict-free plan. All plan types return an identical schema. Plans are stored in-memory until applied.

## Planning flow

```
POST /plan  or  POST /plan/multi-period
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
      │  Enforcement: server-side check at the top of /plan and /plan/multi-period.
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
      │  Compute:
      │    area_km²       = bounding-box area of all candidate cells whose area
      │                     field matches geographic_area, derived from their
      │                     lat/lon coordinates via Haversine:
      │                     (max_lat−min_lat) × (max_lon−min_lon) in km²
      │    required_ues   = expected_user_density × area_km²
      │                     × HOURLY_LOAD[traffic_profile.peak_hour]
      │    deployed_ues   = Σ max_ues for cells whose area matches geographic_area
      │    power_headroom = for each deployed cell, can increasing tx_power_w to
      │                     hardware maximum expand coverage to fill demand gaps
      │                     without pushing neighbour SINR below sinr_min_db?
      │
      │  Decision:
      │    deployed_ues ≥ required_ues  AND  power_headroom is sufficient
      │        → Reorganize mode  (no new cells; adjust power + rebalance)
      │    otherwise
      │        → Deploy mode  (new cells required)
      │
      ├─────────────────────────────────┬─────────────────────────────────────
      │                                 │
      ▼                                 ▼
Reorganize mode                    Deploy mode
      │                                 │
      │ 1. Power optimization           ├─ Single-period  (POST /plan)
      │    compute optimal tx_power_w   │    heuristic: density-weighted scoring
      │    per existing cell to meet    │    OR MIP: Almoghathawi et al. 2024
      │    coverage without SINR clash  │
      │                                 ├─ Multi-period  (POST /plan/multi-period)
      │ 2. Load rebalancing             │    MIP across 2–3 time periods
      │    compute new DU assignments   │    Case A (phased rollout) or
      │    to equalise PRB across DUs   │    Case B (diurnal demand shift)
      │                                 │
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

Step 4 — store in _plans[plan_id]; return unified plan schema (see below)
```

## Unified plan schema

Both `reorganize` and `deploy` modes, both single-period and multi-period, return this identical top-level structure:

```json
{
  "plan_id":          "string (8-char UUID)",
  "plan_type":        "reorganize" | "deploy",
  "planning_method":  "power_rebalance" | "heuristic" | "mip" | "mip_multi_period",
  "timestamp":        "ISO-8601",
  "geographic_area":  "string",
  "demand_mode":      "single" | "permanent" | "temporary",
  "n_periods":        1,
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
      "built_in_period": null,
      "is_new":          true | false
    }
  ],
  "dus": [
    {"du_id", "cu_id", "cell_ids", "centroid_lat", "centroid_lon", "midhaul_latency_ms"}
  ],
  "cus": [
    {"cu_id", "du_ids", "centroid_lat", "centroid_lon"}
  ],
  "build_schedule":    {},
  "period_assignments": {},
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

Multi-period deploy plans additionally populate:
- `n_periods` — number of time periods in the MIP
- `build_schedule` — `{cell_id: period}` (1-indexed) for each deployed site
- `period_assignments` — `{"(cluster_id, site_id, period)": 1}` from MIP solution
- `cells[*].built_in_period` — which period this cell is activated

## File structure

| File | Purpose |
|---|---|
| `planner_api.py` | FastAPI service — input validation, sufficiency analysis, route handlers |
| `placement.py` | `select_cells` (heuristic), `select_cells_mip` wrapper, `assign_dus`, `assign_cus`, `du_centroid`, latency helpers, `CANDIDATE_CELLS` |
| `pci_planner.py` | `build_adjacency`, `assign_pcis` (graph-coloring), `validate_plan` |
| `slice_allocator.py` | `allocate` (PRB budget per slice), `timing_sync_strategy` |
| `mip_placer.py` | `solve_bs_placement_mip`, COST-231-WI propagation, `DemandCluster`, `CandidateSite`, Malleswaram demand clusters |

## Propagation models

| Model | Used by | Notes |
|---|---|---|
| COST-231-Hata | DU/CU simulators, Map Server | Coverage radius, KPI simulation — urban macro empirical |
| COST-231-Walfisch-Ikegami | MIP placer (`mip_placer.py`) | NLOS urban path loss for link-budget feasibility and SINR constraints |

## MIP-based placement (Almoghathawi et al., 2024)

Reference: Almoghathawi Y., Bin Obaid H., Selim S. — *"Optimal location of base stations for cellular mobile network considering changes in users locations"*, Journal of Engineering Research 13 (2025) 561–567. DOI: 10.1016/j.jer.2024.04.020

**Formulation** — Mixed integer programming minimising total network cost:

```
Minimise  Σ_j Σ_t ( c_jt·z_jt  +  r_jt·y_jt )
```

where `z_jt` = build BS at site j in period t (one-time CAPEX), `y_jt` = BS active in period t (per-period OPEX).

**Decision variables:**
- `x[i,j,t]` — binary: demand cluster i assigned to site j at period t
- `y[j,t]` — binary: site j active at period t
- `z[j,t]` — binary: site j built at period t

**Constraints (paper numbering preserved):**
- **(2) Single-build**: each candidate site built at most once across all periods
- **(3) Coverage**: every demand cluster served by a BS built in this or an earlier period
- **(4) Activation**: BS can operate only after it has been built
- **(5) Unique assignment**: each demand cluster assigned to exactly one BS per period
- **(6) Implies-active**: assigning demand to a site forces that site active
- **(7) Capacity**: channel demand at each BS ≤ δ_j (max UE capacity)
- **(8) SINR QoS**: received SINR at each demand cluster ≥ SINR_min; linearised as:
  `α(i,t)·(1 + SINR_lin) ≥ SINR_lin·P_noise + SINR_lin·β(i,t)`

**Demand clusters** (Tutschku 1998 concept): 3 pre-defined Malleswaram clusters (North / Central / South), each with channel demand ρ_i. Separate from candidate sites.

**Multi-period modes:**
- **Case A — permanent/expanding**: period 1 = central commercial core, period 2 = north + south. BSs built in period 1 serve later periods.
- **Case B — temporary/shifting**: period 1 = morning railway station peak, period 2 = daytime commercial, period 3 = full-area evening. Solver minimises total cost across all scenarios.

**Cost model:**
- `install_cost` (c_jt): one-time CAPEX; incurred once when the site is built
- `op_cost` (r_jt): per-period OPEX; incurred every period the site is active

**Solver**: CBC (via `pulp`). Falls back to heuristic on timeout/infeasibility (single-period only). Multi-period returns HTTP 422 on solver failure.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CONTROLLER_URL` | `http://controller:8080` | sufficiency analysis (`/network`) + `plan/apply` |

`sinr_min_db` and `mip_time_limit_sec` are request fields, not env vars.

## Routes

```
GET  /health
GET  /candidates               list all candidate cell sites with lat/lon and area
GET  /demand-clusters          list Malleswaram demand clusters + preset period profiles

POST /plan
     Body: {geographic_area, expected_user_density, traffic_profile,
            spectrum_bands, latency_constraints, deployment_budget,
            use_mip, sinr_min_db, mip_time_limit_sec}
     Returns (missing fields):
       HTTP 200  {"status": "missing_fields", "missing": [...], "message": "..."}
     Returns (success):
       HTTP 200  unified plan schema
         plan_type = "reorganize" if existing cells are sufficient
         plan_type = "deploy"     if new cells are required

POST /plan/multi-period
     Body: {geographic_area, expected_user_density, traffic_profile,
            spectrum_bands, latency_constraints, deployment_budget,
            demand_mode, time_periods, sinr_min_db, mip_time_limit_sec}
     Returns (missing fields):
       HTTP 200  {"status": "missing_fields", "missing": [...], "message": "..."}
     Returns (success):
       HTTP 200  unified plan schema with n_periods, build_schedule,
                 period_assignments populated

GET  /plan/{plan_id}           retrieve stored plan by ID

POST /plan/apply
     Body: {plan_id}
     Action: calls POST /topology/replace on Controller with plan topology
     Returns: Controller's topology/replace response
```
