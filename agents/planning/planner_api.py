#!/usr/bin/env python3
"""
Planning API — FastAPI service that takes deployment parameters and returns
a complete, conflict-free network plan for the Bangalore region.

POST /plan        → generate plan
POST /plan/apply  → push plan to Controller (live topology update)
GET  /plan/{id}   → retrieve a stored plan
"""

import os
import uuid
import httpx
import logging
from datetime import datetime, timezone
from typing import Literal
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from placement import (
    select_cells, select_cells_mip, assign_dus, assign_cus, du_centroid,
    estimate_cost, fronthaul_latency_us, midhaul_latency_ms, CANDIDATE_CELLS
)
from pci_planner import assign_pcis, validate_plan
from slice_allocator import allocate, timing_sync_strategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://controller:8080")

app = FastAPI(title="Telecom Planning API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# In-memory plan store (replace with DB in prod)
_plans: dict[str, dict] = {}


# ── Request / Response models ────────────────────────────────────────────────

class TrafficProfile(BaseModel):
    eMBB:      float = 0.70
    URLLC:     float = 0.20
    mMTC:      float = 0.10
    peak_hour: int   = 19

class LatencyConstraints(BaseModel):
    e2e_ms:        float = 10.0
    fronthaul_us:  float = 100.0

class ComputeResources(BaseModel):
    cpu_cores_per_site: int   = 32
    ram_gb_per_site:    int   = 64

class PlanRequest(BaseModel):
    geographic_area:     str                = "Bangalore"
    expected_user_density: float            = Field(500.0, description="Users per km²")
    traffic_profile:     TrafficProfile     = Field(default_factory=TrafficProfile)
    fiber_availability:  list[str]          = Field(default_factory=list, description="Areas with fiber")
    spectrum_bands:      list[str]          = Field(default_factory=lambda: ["n78", "n28"])
    latency_constraints: LatencyConstraints = Field(default_factory=LatencyConstraints)
    compute_resources:   ComputeResources   = Field(default_factory=ComputeResources)
    deployment_budget:   float              = Field(2_000_000.0, description="USD")
    max_cells_per_du:    int                = 3
    max_dus_per_cu:      int                = 4
    use_mip:             bool               = Field(False, description="Use MIP-based optimal placement (Almoghathawi et al. 2024)")
    sinr_min_db:         float              = Field(10.0,  description="Minimum SINR enforced by MIP placer (dB)")
    mip_time_limit_sec:  int                = Field(120,   description="MIP solver wall-clock limit (s)")


class TimePeriodDemand(BaseModel):
    """One time period's active demand profile for multi-period planning."""
    period:       int
    cluster_ids:  list[str] = Field(
        description="Active demand cluster IDs (e.g. ['DC-KRM','DC-WFD']). "
                    "Use GET /demand-clusters to list available clusters."
    )
    description:  str = ""


class MultiPeriodPlanRequest(BaseModel):
    geographic_area:    str   = "Bangalore"
    demand_mode:        Literal["permanent", "temporary"] = Field(
        "permanent",
        description="permanent=Case A (expanding rollout), temporary=Case B (event/diurnal shift)"
    )
    time_periods:       list[TimePeriodDemand] = Field(
        default_factory=list,
        description="Per-period demand specs. If empty, uses built-in Bangalore profiles."
    )
    spectrum_bands:     list[str]          = Field(default_factory=lambda: ["n78", "n28"])
    deployment_budget:  float              = Field(2_000_000.0, description="USD")
    traffic_profile:    TrafficProfile     = Field(default_factory=TrafficProfile)
    latency_constraints: LatencyConstraints = Field(default_factory=LatencyConstraints)
    max_cells_per_du:   int  = 3
    max_dus_per_cu:     int  = 4
    sinr_min_db:        float = Field(10.0, description="Minimum SINR threshold (dB)")
    mip_time_limit_sec: int   = Field(120,  description="MIP solver wall-clock limit (s)")


class ApplyRequest(BaseModel):
    plan_id: str


# ── Planning logic ───────────────────────────────────────────────────────────

def generate_plan(req: PlanRequest) -> dict:
    traffic = {"eMBB": req.traffic_profile.eMBB,
               "URLLC": req.traffic_profile.URLLC,
               "mMTC": req.traffic_profile.mMTC}
    lat_constraints = {"e2e_ms": req.latency_constraints.e2e_ms,
                       "fronthaul_us": req.latency_constraints.fronthaul_us}

    # 1. Select cells — heuristic or MIP
    mip_result = None
    if req.use_mip:
        try:
            mip_result = select_cells_mip(
                demand_clusters=None,
                budget=req.deployment_budget,
                spectrum_bands=req.spectrum_bands,
                sinr_min_db=req.sinr_min_db,
                time_limit_sec=req.mip_time_limit_sec,
            )
            cells = mip_result["selected_cells"]
            log.info("MIP placement: %d sites selected, status=%s",
                     len(cells), mip_result["status"])
        except Exception as e:
            log.warning("MIP placement failed (%s); falling back to heuristic", e)
            mip_result = None

    if mip_result is None:
        cells = select_cells(req.expected_user_density, req.deployment_budget, req.spectrum_bands)

    cell_map = {c["cell_id"]: c for c in cells}

    # 2. Assign PCIs (collision + confusion free)
    pcis       = assign_pcis(cells)
    violations = validate_plan(cells, pcis)
    if violations:
        log.warning(f"PCI plan has {len(violations)} violation(s): {violations[:3]}")

    # 3. Group cells into DUs
    du_cells = assign_dus(cells, req.max_cells_per_du)

    # 4. Group DUs into CUs
    cu_dus = assign_cus(du_cells, cell_map, req.max_dus_per_cu)

    # 5. Compute centroids for latency estimates
    du_cents = {du_id: du_centroid(du_id, cids, cell_map) for du_id, cids in du_cells.items()}
    cu_cents = {}
    for cu_id, du_ids in cu_dus.items():
        lats = [du_cents[d][0] for d in du_ids]
        lons = [du_cents[d][1] for d in du_ids]
        cu_cents[cu_id] = (sum(lats) / len(lats), sum(lons) / len(lons))

    # 6. Build reverse lookup: cell → DU, DU → CU
    cell_to_du = {cid: du_id for du_id, cids in du_cells.items() for cid in cids}
    du_to_cu   = {du_id: cu_id for cu_id, du_ids in cu_dus.items() for du_id in du_ids}

    # 7. Timing synchronisation strategy
    timing_sync = timing_sync_strategy(lat_constraints, req.spectrum_bands)

    # 8. Slice allocation per cell
    cell_plans = []
    for c in cells:
        cid   = c["cell_id"]
        du_id = cell_to_du[cid]
        cu_id = du_to_cu[du_id]
        fh_us = fronthaul_latency_us(c, du_cents[du_id])
        sl    = allocate(traffic, c["max_ues"], lat_constraints)

        cell_plans.append({
            **c,
            "pci":                   pcis[cid],
            "du_id":                 du_id,
            "cu_id":                 cu_id,
            "fronthaul_latency_us":  fh_us,
            "slices":                sl["slices"],
            "slice_warnings":        sl["warnings"],
        })

    # 9. DU plans
    du_plans = []
    for du_id, cids in du_cells.items():
        cu_id   = du_to_cu[du_id]
        mh_ms   = midhaul_latency_ms(du_cents[du_id], cu_cents[cu_id])
        du_plans.append({
            "du_id":               du_id,
            "cu_id":               cu_id,
            "cell_ids":            cids,
            "centroid_lat":        round(du_cents[du_id][0], 6),
            "centroid_lon":        round(du_cents[du_id][1], 6),
            "midhaul_latency_ms":  mh_ms,
        })

    # 10. CU plans
    cu_plans = []
    for cu_id, du_ids in cu_dus.items():
        cu_plans.append({
            "cu_id":     cu_id,
            "du_ids":    du_ids,
            "centroid_lat": round(cu_cents[cu_id][0], 6),
            "centroid_lon": round(cu_cents[cu_id][1], 6),
        })

    estimated_cost     = estimate_cost(len(cells), len(du_cells), len(cu_dus))
    total_capacity_ues = sum(c["max_ues"] for c in cells)

    plan_id = str(uuid.uuid4())[:8]
    plan = {
        "plan_id":              plan_id,
        "timestamp":            datetime.now(timezone.utc).isoformat(),
        "geographic_area":      req.geographic_area,
        "timing_sync":          timing_sync,
        "pci_violations":       violations,
        "cells":                cell_plans,
        "dus":                  du_plans,
        "cus":                  cu_plans,
        "summary": {
            "n_cells":              len(cells),
            "n_dus":                len(du_plans),
            "n_cus":                len(cu_plans),
            "total_capacity_ues":   total_capacity_ues,
            "estimated_cost_usd":   estimated_cost,
            "budget_utilisation_pct": round(estimated_cost / req.deployment_budget * 100, 1),
            "placement_method":     "mip" if mip_result and mip_result.get("source") == "mip"
                                    else "heuristic",
        },
    }
    if mip_result:
        plan["mip_placement"] = {
            "status":         mip_result.get("status"),
            "install_cost":   mip_result.get("install_cost"),
            "op_cost":        mip_result.get("op_cost"),
            "total_cost":     mip_result.get("total_cost"),
            "build_schedule": mip_result.get("build_schedule", {}),
            "feasibility":    mip_result.get("feasibility", {}),
        }
    return plan


def plan_to_topology(plan: dict) -> dict:
    """Convert a network plan into the topology.json format used by the Controller."""
    cus, dus, cells_topo = {}, {}, {}

    for cu in plan["cus"]:
        cus[cu["cu_id"]] = {
            "host":    cu["cu_id"].lower(),
            "region":  plan["geographic_area"],
            "du_ids":  cu["du_ids"],
        }
    for du in plan["dus"]:
        dus[du["du_id"]] = {
            "cu_id":    du["cu_id"],
            "host":     du["du_id"].lower(),
            "cell_ids": du["cell_ids"],
        }
    for c in plan["cells"]:
        cells_topo[c["cell_id"]] = {
            "area":     c["area"],
            "pci":      c["pci"],
            "lat":      c["lat"],
            "lon":      c["lon"],
            "band":     c["band"],
            "freq_mhz": c["freq_mhz"],
            "max_ues":  c["max_ues"],
        }

    return {
        "version":      1,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "updated_by":   f"planning-api:{plan['plan_id']}",
        "cus":          cus,
        "dus":          dus,
        "cells":        cells_topo,
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/plan")
def create_plan(req: PlanRequest):
    plan = generate_plan(req)
    _plans[plan["plan_id"]] = plan
    log.info(f"Plan {plan['plan_id']}: {plan['summary']}")
    return plan


@app.get("/plan/{plan_id}")
def get_plan(plan_id: str):
    if plan_id not in _plans:
        raise HTTPException(404, f"Plan {plan_id} not found")
    return _plans[plan_id]


@app.post("/plan/apply")
def apply_plan(req: ApplyRequest):
    if req.plan_id not in _plans:
        raise HTTPException(404, f"Plan {req.plan_id} not found")

    plan     = _plans[req.plan_id]
    topology = plan_to_topology(plan)

    try:
        # Write topology to controller
        resp = httpx.post(
            f"{CONTROLLER_URL}/topology/replace",
            json=topology,
            timeout=10.0,
        )
        resp.raise_for_status()
        log.info(f"Plan {req.plan_id} applied to Controller.")
        return {"status": "applied", "plan_id": req.plan_id, "controller_response": resp.json()}
    except httpx.HTTPError as e:
        # Fall back: return the topology for manual application
        log.warning(f"Controller unreachable ({e}); returning topology for manual apply.")
        return {
            "status":   "controller_unreachable",
            "plan_id":  req.plan_id,
            "topology": topology,
            "hint":     f"POST this topology to {CONTROLLER_URL}/topology/replace",
        }


@app.get("/candidates")
def list_candidates():
    """Return all candidate cell sites (the Bangalore inventory)."""
    return CANDIDATE_CELLS


@app.get("/demand-clusters")
def list_demand_clusters():
    """Return the pre-defined Bangalore demand clusters used by the MIP placer."""
    from mip_placer import (
        BANGALORE_DEMAND_CLUSTERS, DEMAND_PERIODS_CASE_A, DEMAND_PERIODS_CASE_B
    )
    return {
        "clusters": [
            {"cluster_id": dc.cluster_id, "area": dc.area,
             "lat": dc.lat, "lon": dc.lon, "n_channels": dc.n_channels}
            for dc in BANGALORE_DEMAND_CLUSTERS
        ],
        "preset_periods": {
            "permanent_case_a": DEMAND_PERIODS_CASE_A,
            "temporary_case_b": DEMAND_PERIODS_CASE_B,
        },
    }


@app.post("/plan/multi-period")
def create_multi_period_plan(req: MultiPeriodPlanRequest):
    """
    Multi-period MIP-based network planning.

    Implements Almoghathawi et al. (2024) Case A (permanent) and Case B (temporary).

    If time_periods is empty, uses the built-in Bangalore profiles:
      permanent → phased rollout (core → outer → eastern areas)
      temporary → diurnal demand shift (residential → IT hubs → commute mix)

    Returns the optimal BS build schedule across all periods plus a
    standard single-period plan for the final (complete) topology.
    """
    from mip_placer import (
        solve_bs_placement_mip, PropagationParams,
        BANGALORE_DEMAND_CLUSTERS, DEMAND_PERIODS_CASE_A, DEMAND_PERIODS_CASE_B,
        candidate_sites_from_cells, DemandCluster,
    )

    dc_by_id = {dc.cluster_id: dc for dc in BANGALORE_DEMAND_CLUSTERS}

    # Build demand-by-period list
    if req.time_periods:
        demand_by_period = []
        for tp in sorted(req.time_periods, key=lambda x: x.period):
            period_clusters = [dc_by_id[cid] for cid in tp.cluster_ids
                               if cid in dc_by_id]
            demand_by_period.append(period_clusters)
    else:
        preset = DEMAND_PERIODS_CASE_A if req.demand_mode == "permanent" \
                 else DEMAND_PERIODS_CASE_B
        demand_by_period = [
            [dc_by_id[cid] for cid in cids if cid in dc_by_id]
            for cids in preset
        ]

    candidates = [c for c in CANDIDATE_CELLS
                  if c["band"] in req.spectrum_bands] or CANDIDATE_CELLS
    cs_list = candidate_sites_from_cells(
        candidates,
        install_cost_usd=req.deployment_budget * 0.6 / max(len(candidates), 1),
        op_cost_usd=1_000.0,
    )

    prop = PropagationParams(sinr_min_db=req.sinr_min_db)
    mip_result = solve_bs_placement_mip(
        demand_by_period=demand_by_period,
        candidate_sites=cs_list,
        prop=prop,
        mode=req.demand_mode,
        time_limit_sec=req.mip_time_limit_sec,
    )

    if mip_result["status"] != "Optimal":
        raise HTTPException(422, detail={
            "error": "MIP solver could not find an optimal solution",
            "status": mip_result["status"],
            "msg":    mip_result["solver_msg"],
            "feasibility": mip_result.get("feasibility", {}),
        })

    # Build final single-period plan using all selected sites across all periods
    selected_ids = set(mip_result["selected_sites"])
    final_cells  = [c for c in CANDIDATE_CELLS if c["cell_id"] in selected_ids]

    # Run PCI + slicing on the final cell set
    pcis           = assign_pcis(final_cells)
    violations     = validate_plan(final_cells, pcis)
    cell_map       = {c["cell_id"]: c for c in final_cells}
    du_cells       = assign_dus(final_cells, req.max_cells_per_du)
    cu_dus         = assign_cus(du_cells, cell_map, req.max_dus_per_cu)
    du_cents       = {du: du_centroid(du, cids, cell_map) for du, cids in du_cells.items()}
    cu_cents: dict[str, tuple[float, float]] = {}
    for cu_id, du_ids in cu_dus.items():
        lats = [du_cents[d][0] for d in du_ids]
        lons = [du_cents[d][1] for d in du_ids]
        cu_cents[cu_id] = (sum(lats) / len(lats), sum(lons) / len(lons))

    cell_to_du = {cid: du for du, cids in du_cells.items() for cid in cids}
    du_to_cu   = {du: cu for cu, dus in cu_dus.items() for du in dus}
    traffic    = {"eMBB": req.traffic_profile.eMBB,
                  "URLLC": req.traffic_profile.URLLC,
                  "mMTC": req.traffic_profile.mMTC}
    lat_constraints = {"e2e_ms":       req.latency_constraints.e2e_ms,
                       "fronthaul_us": req.latency_constraints.fronthaul_us}
    timing_sync = timing_sync_strategy(lat_constraints, req.spectrum_bands)

    cell_plans = []
    for c in final_cells:
        cid   = c["cell_id"]
        du_id = cell_to_du[cid]
        cu_id = du_to_cu[du_id]
        sl    = allocate(traffic, c["max_ues"], lat_constraints)
        cell_plans.append({
            **c,
            "pci": pcis[cid], "du_id": du_id, "cu_id": cu_id,
            "fronthaul_latency_us": fronthaul_latency_us(c, du_cents[du_id]),
            "slices": sl["slices"], "slice_warnings": sl["warnings"],
            "built_in_period": mip_result["build_schedule"].get(cid),
        })

    du_plans = [{"du_id": du, "cu_id": du_to_cu[du], "cell_ids": cids,
                 "centroid_lat": round(du_cents[du][0], 6),
                 "centroid_lon": round(du_cents[du][1], 6),
                 "midhaul_latency_ms": midhaul_latency_ms(
                     du_cents[du], cu_cents[du_to_cu[du]])}
                for du, cids in du_cells.items()]

    cu_plans = [{"cu_id": cu, "du_ids": dus,
                 "centroid_lat": round(cu_cents[cu][0], 6),
                 "centroid_lon": round(cu_cents[cu][1], 6)}
                for cu, dus in cu_dus.items()]

    plan_id = str(uuid.uuid4())[:8]
    plan = {
        "plan_id":       plan_id,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "geographic_area": req.geographic_area,
        "planning_method": "mip_multi_period",
        "demand_mode":   req.demand_mode,
        "n_periods":     mip_result["n_periods"],
        "timing_sync":   timing_sync,
        "pci_violations": violations,
        "cells":         cell_plans,
        "dus":           du_plans,
        "cus":           cu_plans,
        "mip_result": {
            "status":         mip_result["status"],
            "total_cost":     mip_result["total_cost"],
            "install_cost":   mip_result["install_cost"],
            "op_cost":        mip_result["op_cost"],
            "build_schedule": mip_result["build_schedule"],
            "assignments":    mip_result["assignments"],
            "feasibility":    mip_result["feasibility"],
        },
        "summary": {
            "n_cells":             len(final_cells),
            "n_dus":               len(du_plans),
            "n_cus":               len(cu_plans),
            "total_capacity_ues":  sum(c["max_ues"] for c in final_cells),
            "mip_total_cost_usd":  mip_result["total_cost"],
            "mip_install_cost_usd": mip_result["install_cost"],
            "mip_op_cost_usd":     mip_result["op_cost"],
            "budget_utilisation_pct": round(
                (mip_result["total_cost"] or 0) / req.deployment_budget * 100, 1),
            "placement_method":    "mip_multi_period",
        },
    }
    _plans[plan_id] = plan
    log.info("Multi-period plan %s: %d sites, mode=%s, cost=%.0f",
             plan_id, len(final_cells), req.demand_mode, mip_result["total_cost"] or 0)
    return plan


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("planner_api:app", host="0.0.0.0", port=8081, reload=False)
