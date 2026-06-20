#!/usr/bin/env python3
"""
Planning API — FastAPI service for network deployment planning.

Decides whether existing cells can satisfy demand (reorganize) or new
infrastructure is required (deploy), then generates a complete conflict-free
plan. All plan types return an identical unified schema.

POST /plan              → generate plan (reorganize or deploy)
POST /plan/multi-period → multi-period MIP deployment plan
POST /plan/apply        → push plan to Controller (live topology update)
GET  /plan/{id}         → retrieve a stored plan
GET  /candidates        → list candidate cell inventory
GET  /demand-clusters   → list Malleswaram demand clusters
"""

import os
import uuid
import httpx
import logging
from datetime import datetime, timezone
from typing import Literal, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from placement import (
    select_cells, select_cells_mip, assign_dus, assign_cus, du_centroid,
    estimate_cost, fronthaul_latency_us, midhaul_latency_ms,
    haversine_km, CANDIDATE_CELLS,
)
from pci_planner import assign_pcis, validate_plan
from slice_allocator import allocate, timing_sync_strategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://controller:8080")

# Diurnal load profile — matches core_simulator.py HOURLY_LOAD (index = hour 0–23)
HOURLY_LOAD = [
    0.08, 0.06, 0.05, 0.05, 0.06, 0.12,
    0.30, 0.65, 0.85, 0.80, 0.70, 0.65,
    0.65, 0.60, 0.62, 0.68, 0.78, 0.90,
    0.95, 1.00, 0.97, 0.88, 0.62, 0.30,
]

app = FastAPI(title="Telecom Planning API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_plans: dict[str, dict] = {}


# ── Request / Response models ────────────────────────────────────────────────

class TrafficProfile(BaseModel):
    eMBB:      float = 0.70
    URLLC:     float = 0.20
    mMTC:      float = 0.10
    peak_hour: int   = 19

class LatencyConstraints(BaseModel):
    e2e_ms:       float = 10.0
    fronthaul_us: float = 100.0

class ComputeResources(BaseModel):
    cpu_cores_per_site: int = 32
    ram_gb_per_site:    int = 64

class PlanRequest(BaseModel):
    # Required — None means the caller did not supply the field
    geographic_area:       Optional[str]                = None
    expected_user_density: Optional[float]              = None
    traffic_profile:       Optional[TrafficProfile]     = None
    spectrum_bands:        Optional[list[str]]          = None
    latency_constraints:   Optional[LatencyConstraints] = None
    deployment_budget:     Optional[float]              = None
    # Optional with defaults
    fiber_availability:    list[str]          = Field(default_factory=list)
    compute_resources:     ComputeResources   = Field(default_factory=ComputeResources)
    max_cells_per_du:      int                = 3
    max_dus_per_cu:        int                = 4
    use_mip:               bool               = False
    sinr_min_db:           float              = 10.0
    mip_time_limit_sec:    int                = 120


class TimePeriodDemand(BaseModel):
    period:      int
    cluster_ids: list[str] = Field(description="Active demand cluster IDs")
    description: str = ""


class MultiPeriodPlanRequest(BaseModel):
    # Required — None means the caller did not supply the field
    geographic_area:     Optional[str]                          = None
    demand_mode:         Optional[Literal["permanent", "temporary"]] = None
    expected_user_density: Optional[float]                      = None
    traffic_profile:     Optional[TrafficProfile]               = None
    spectrum_bands:      Optional[list[str]]                    = None
    latency_constraints: Optional[LatencyConstraints]           = None
    deployment_budget:   Optional[float]                        = None
    # Optional with defaults
    time_periods:        list[TimePeriodDemand] = Field(default_factory=list)
    max_cells_per_du:    int                    = 3
    max_dus_per_cu:      int                    = 4
    sinr_min_db:         float                  = 10.0
    mip_time_limit_sec:  int                    = 120


class ApplyRequest(BaseModel):
    plan_id: str


# ── Shared helpers ───────────────────────────────────────────────────────────

def _area_km2(geographic_area: str) -> float:
    """Bounding-box area in km² derived from matching CANDIDATE_CELLS lat/lon."""
    matches = [
        c for c in CANDIDATE_CELLS
        if geographic_area.lower() in c["area"].lower()
        or c["area"].lower() in geographic_area.lower()
    ] or CANDIDATE_CELLS
    lats = [c["lat"] for c in matches]
    lons = [c["lon"] for c in matches]
    lat_km = haversine_km(min(lats), min(lons), max(lats), min(lons))
    lon_km = haversine_km(min(lats), min(lons), min(lats), max(lons))
    return round(max(lat_km * lon_km, 0.01), 4)  # floor at 0.01 to avoid zero-area


def _area_matches(cell_area: str, target: str) -> bool:
    return target.lower() in cell_area.lower() or cell_area.lower() in target.lower()


def _sufficiency_check(req: PlanRequest) -> tuple[dict, list[dict]]:
    """
    Query the live network, compute required UEs at peak hour, decide mode.

    Returns (analysis_dict, live_cells_list).
    live_cells_list is in the format expected by _run_pipeline; contains
    _existing_pci for use in reorganize mode.
    """
    area_km2  = _area_km2(req.geographic_area)
    peak_hour = req.traffic_profile.peak_hour
    lf        = HOURLY_LOAD[peak_hour]
    required_ues = round(req.expected_user_density * area_km2 * lf)

    deployed_ues  = 0
    live_cells: list[dict] = []
    try:
        resp = httpx.get(f"{CONTROLLER_URL}/network", timeout=5.0)
        resp.raise_for_status()
        for cell_id, cd in resp.json().get("cells", {}).items():
            if _area_matches(cd.get("area", ""), req.geographic_area):
                deployed_ues += cd.get("max_ues", 0)
                live_cells.append({
                    "cell_id":        cell_id,
                    "area":           cd.get("area", req.geographic_area),
                    "lat":            cd["lat"],
                    "lon":            cd["lon"],
                    "band":           cd.get("band", "n78"),
                    "freq_mhz":       cd.get("freq_mhz", 3500),
                    "max_ues":        cd.get("max_ues", 900),
                    "generation":     cd.get("generation", "5G"),
                    "vendor":         cd.get("vendor", "Nokia"),
                    "hardware_model": cd.get("hardware_model", "AirScale MAA 64T64R"),
                    "antenna_config": cd.get("antenna_config", "64T64R"),
                    "tx_power_w":     cd.get("tx_power_w", 1000),
                    "idle_power_w":   cd.get("idle_power_w", 250),
                    "peak_dl_mbps":   cd.get("peak_dl_mbps", 3800),
                    "_existing_pci":  cd.get("pci", 0),
                })
    except Exception as exc:
        log.warning("Controller unreachable for sufficiency check: %s", exc)

    mode = "reorganize" if deployed_ues >= required_ues else "deploy"
    analysis = {
        "area_km2":         area_km2,
        "required_ues":     required_ues,
        "current_capacity": deployed_ues,
        "peak_hour":        peak_hour,
        "load_factor":      lf,
        "mode_chosen":      mode,
    }
    return analysis, live_cells


def _run_pipeline(
    cells: list[dict],
    traffic: dict,
    lat_constraints: dict,
    spectrum_bands: list[str],
    max_cells_per_du: int,
    max_dus_per_cu: int,
    is_new: bool = True,
    build_schedule: dict | None = None,
    keep_pcis: dict | None = None,
) -> dict:
    """
    Shared downstream pipeline: PCI → DU → CU → centroids → timing → slices.

    keep_pcis: if provided, uses these PCIs instead of re-running assignment
               (reorganize mode — preserve existing cell PCIs to minimise disruption).
    build_schedule: {cell_id: period} for multi-period plans.
    Returns dict with cell_plans, du_plans, cu_plans, timing_sync, violations, du_cells.
    """
    if keep_pcis is not None:
        pcis = keep_pcis
    else:
        pcis = assign_pcis(cells)
    violations = validate_plan(cells, pcis)
    if violations:
        log.warning("PCI plan has %d violation(s): %s", len(violations), violations[:3])

    cell_map  = {c["cell_id"]: c for c in cells}
    du_cells  = assign_dus(cells, max_cells_per_du)
    cu_dus    = assign_cus(du_cells, cell_map, max_dus_per_cu)

    du_cents: dict[str, tuple] = {
        du: du_centroid(du, cids, cell_map) for du, cids in du_cells.items()
    }
    cu_cents: dict[str, tuple] = {}
    for cu_id, du_ids in cu_dus.items():
        lats = [du_cents[d][0] for d in du_ids]
        lons = [du_cents[d][1] for d in du_ids]
        cu_cents[cu_id] = (sum(lats) / len(lats), sum(lons) / len(lons))

    cell_to_du = {cid: du for du, cids in du_cells.items() for cid in cids}
    du_to_cu   = {du: cu for cu, dus in cu_dus.items() for du in dus}
    timing_sync = timing_sync_strategy(lat_constraints, spectrum_bands)

    cell_plans = []
    for c in cells:
        cid   = c["cell_id"]
        du_id = cell_to_du[cid]
        cu_id = du_to_cu[du_id]
        sl    = allocate(traffic, c["max_ues"], lat_constraints)
        cell_plans.append({
            **{k: v for k, v in c.items() if not k.startswith("_")},
            "pci":                  pcis[cid],
            "du_id":                du_id,
            "cu_id":                cu_id,
            "fronthaul_latency_us": fronthaul_latency_us(c, du_cents[du_id]),
            "slices":               sl["slices"],
            "slice_warnings":       sl["warnings"],
            "is_new":               is_new,
            "built_in_period":      (build_schedule or {}).get(cid),
        })

    du_plans = [
        {
            "du_id":              du,
            "cu_id":              du_to_cu[du],
            "cell_ids":           cids,
            "centroid_lat":       round(du_cents[du][0], 6),
            "centroid_lon":       round(du_cents[du][1], 6),
            "midhaul_latency_ms": midhaul_latency_ms(du_cents[du], cu_cents[du_to_cu[du]]),
        }
        for du, cids in du_cells.items()
    ]

    cu_plans = [
        {
            "cu_id":        cu,
            "du_ids":       dus,
            "centroid_lat": round(cu_cents[cu][0], 6),
            "centroid_lon": round(cu_cents[cu][1], 6),
        }
        for cu, dus in cu_dus.items()
    ]

    return {
        "cell_plans":  cell_plans,
        "du_plans":    du_plans,
        "cu_plans":    cu_plans,
        "timing_sync": timing_sync,
        "violations":  violations,
        "du_cells":    du_cells,
    }


# ── Planning logic ───────────────────────────────────────────────────────────

def generate_plan(req: PlanRequest) -> dict:
    traffic = {
        "eMBB":  req.traffic_profile.eMBB,
        "URLLC": req.traffic_profile.URLLC,
        "mMTC":  req.traffic_profile.mMTC,
    }
    lat_constraints = {
        "e2e_ms":       req.latency_constraints.e2e_ms,
        "fronthaul_us": req.latency_constraints.fronthaul_us,
    }

    sa, live_cells = _sufficiency_check(req)

    if sa["mode_chosen"] == "reorganize" and live_cells:
        return _reorganize_plan(req, sa, live_cells, traffic, lat_constraints)
    return _deploy_plan(req, sa, traffic, lat_constraints)


def _reorganize_plan(
    req: PlanRequest,
    sa: dict,
    live_cells: list[dict],
    traffic: dict,
    lat_constraints: dict,
) -> dict:
    """Rebalance DU assignments + reallocate slices for existing deployed cells."""
    keep_pcis = {c["cell_id"]: c["_existing_pci"] for c in live_cells}
    pipeline  = _run_pipeline(
        cells=live_cells,
        traffic=traffic,
        lat_constraints=lat_constraints,
        spectrum_bands=req.spectrum_bands,
        max_cells_per_du=req.max_cells_per_du,
        max_dus_per_cu=req.max_dus_per_cu,
        is_new=False,
        keep_pcis=keep_pcis,
    )
    plan_id = str(uuid.uuid4())[:8]
    return {
        "plan_id":          plan_id,
        "plan_type":        "reorganize",
        "planning_method":  "power_rebalance",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "geographic_area":  req.geographic_area,
        "demand_mode":      "single",
        "n_periods":        1,
        "timing_sync":      pipeline["timing_sync"],
        "pci_violations":   pipeline["violations"],
        "cells":            pipeline["cell_plans"],
        "dus":              pipeline["du_plans"],
        "cus":              pipeline["cu_plans"],
        "build_schedule":   {},
        "period_assignments": {},
        "summary": {
            "n_cells":               len(live_cells),
            "n_new_cells":           0,
            "n_dus":                 len(pipeline["du_plans"]),
            "n_cus":                 len(pipeline["cu_plans"]),
            "total_capacity_ues":    sum(c["max_ues"] for c in live_cells),
            "estimated_cost_usd":    0.0,
            "budget_utilisation_pct": 0.0,
            "placement_method":      "power_rebalance",
            "sufficiency_analysis":  sa,
        },
    }


def _deploy_plan(
    req: PlanRequest,
    sa: dict,
    traffic: dict,
    lat_constraints: dict,
) -> dict:
    """Select new cells via heuristic or MIP and run the full pipeline."""
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
            log.info("MIP placement: %d sites, status=%s", len(cells), mip_result["status"])
        except Exception as exc:
            log.warning("MIP placement failed (%s); falling back to heuristic", exc)
            mip_result = None

    if mip_result is None:
        cells = select_cells(req.expected_user_density, req.deployment_budget, req.spectrum_bands)

    pipeline = _run_pipeline(
        cells=cells,
        traffic=traffic,
        lat_constraints=lat_constraints,
        spectrum_bands=req.spectrum_bands,
        max_cells_per_du=req.max_cells_per_du,
        max_dus_per_cu=req.max_dus_per_cu,
        is_new=True,
    )

    planning_method  = ("mip" if mip_result and mip_result.get("source") == "mip"
                        else "heuristic")
    estimated_cost   = estimate_cost(
        len(cells), len(pipeline["du_plans"]), len(pipeline["cu_plans"])
    )

    plan_id = str(uuid.uuid4())[:8]
    plan = {
        "plan_id":          plan_id,
        "plan_type":        "deploy",
        "planning_method":  planning_method,
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "geographic_area":  req.geographic_area,
        "demand_mode":      "single",
        "n_periods":        1,
        "timing_sync":      pipeline["timing_sync"],
        "pci_violations":   pipeline["violations"],
        "cells":            pipeline["cell_plans"],
        "dus":              pipeline["du_plans"],
        "cus":              pipeline["cu_plans"],
        "build_schedule":   {},
        "period_assignments": {},
        "summary": {
            "n_cells":               len(cells),
            "n_new_cells":           len(cells),
            "n_dus":                 len(pipeline["du_plans"]),
            "n_cus":                 len(pipeline["cu_plans"]),
            "total_capacity_ues":    sum(c["max_ues"] for c in cells),
            "estimated_cost_usd":    estimated_cost,
            "budget_utilisation_pct": round(estimated_cost / req.deployment_budget * 100, 1),
            "placement_method":      planning_method,
            "sufficiency_analysis":  sa,
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
    """Convert a network plan into topology.json format used by the Controller.

    Preserves all hardware fields (vendor, hardware_model, generation,
    antenna_config, tx_power_w, idle_power_w, peak_dl_mbps) so that DU
    simulators receive correct specs after a plan is applied.
    """
    cus, dus, cells_topo = {}, {}, {}

    for cu in plan["cus"]:
        cus[cu["cu_id"]] = {
            "host":   cu["cu_id"].lower(),
            "region": plan["geographic_area"],
            "du_ids": cu["du_ids"],
        }
    for du in plan["dus"]:
        dus[du["du_id"]] = {
            "cu_id":    du["cu_id"],
            "host":     du["du_id"].lower(),
            "cell_ids": du["cell_ids"],
        }
    for c in plan["cells"]:
        cells_topo[c["cell_id"]] = {
            "area":           c["area"],
            "pci":            c["pci"],
            "lat":            c["lat"],
            "lon":            c["lon"],
            "band":           c["band"],
            "freq_mhz":       c["freq_mhz"],
            "max_ues":        c["max_ues"],
            "generation":     c.get("generation",     "5G"),
            "vendor":         c.get("vendor",         "Nokia"),
            "hardware_model": c.get("hardware_model", "AirScale MAA 64T64R"),
            "antenna_config": c.get("antenna_config", "64T64R"),
            "tx_power_w":     c.get("tx_power_w",     1000),
            "idle_power_w":   c.get("idle_power_w",   250),
            "peak_dl_mbps":   c.get("peak_dl_mbps",   3800),
        }

    return {
        "version":      1,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "updated_by":   f"planning-api:{plan['plan_id']}",
        "cus":          cus,
        "dus":          dus,
        "cells":        cells_topo,
    }


# ── Input validation ─────────────────────────────────────────────────────────

_PLAN_REQUIRED = [
    "geographic_area", "expected_user_density", "traffic_profile",
    "spectrum_bands", "deployment_budget", "latency_constraints",
]
_MULTI_PERIOD_REQUIRED = _PLAN_REQUIRED + ["demand_mode"]


def _missing_fields_response(req, required: list[str]) -> dict | None:
    missing = [f for f in required if getattr(req, f, None) is None]
    if not missing:
        return None
    return {
        "status":  "missing_fields",
        "missing": missing,
        "message": f"The following required fields were not provided: {', '.join(missing)}",
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/plan")
def create_plan(req: PlanRequest):
    if (missing := _missing_fields_response(req, _PLAN_REQUIRED)):
        return missing
    plan = generate_plan(req)
    _plans[plan["plan_id"]] = plan
    log.info("Plan %s (%s): %s", plan["plan_id"], plan["plan_type"], plan["summary"])
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
        resp = httpx.post(f"{CONTROLLER_URL}/topology/replace", json=topology, timeout=10.0)
        resp.raise_for_status()
        log.info("Plan %s applied to Controller.", req.plan_id)
        return {"status": "applied", "plan_id": req.plan_id, "controller_response": resp.json()}
    except httpx.HTTPError as exc:
        log.warning("Controller unreachable (%s); returning topology for manual apply.", exc)
        return {
            "status":   "controller_unreachable",
            "plan_id":  req.plan_id,
            "topology": topology,
            "hint":     f"POST this topology to {CONTROLLER_URL}/topology/replace",
        }


@app.get("/candidates")
def list_candidates():
    return CANDIDATE_CELLS


@app.get("/demand-clusters")
def list_demand_clusters():
    from mip_placer import (
        BANGALORE_DEMAND_CLUSTERS, DEMAND_PERIODS_CASE_A, DEMAND_PERIODS_CASE_B,
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
    Multi-period MIP-based network planning (Almoghathawi et al. 2024).

    Returns HTTP 200 missing_fields response if any required field is absent.
    permanent (Case A): phased rollout — BSs built in early periods serve later demand.
    temporary (Case B): shifting demand — event/diurnal peaks across periods.

    Returns unified plan schema with build_schedule and period_assignments populated.
    """
    if (missing := _missing_fields_response(req, _MULTI_PERIOD_REQUIRED)):
        return missing

    from mip_placer import (
        solve_bs_placement_mip, PropagationParams,
        BANGALORE_DEMAND_CLUSTERS, DEMAND_PERIODS_CASE_A, DEMAND_PERIODS_CASE_B,
        candidate_sites_from_cells,
    )

    dc_by_id = {dc.cluster_id: dc for dc in BANGALORE_DEMAND_CLUSTERS}

    if req.time_periods:
        demand_by_period = [
            [dc_by_id[cid] for cid in tp.cluster_ids if cid in dc_by_id]
            for tp in sorted(req.time_periods, key=lambda x: x.period)
        ]
    else:
        preset = DEMAND_PERIODS_CASE_A if req.demand_mode == "permanent" \
                 else DEMAND_PERIODS_CASE_B
        demand_by_period = [
            [dc_by_id[cid] for cid in cids if cid in dc_by_id]
            for cids in preset
        ]

    candidates = [c for c in CANDIDATE_CELLS if c["band"] in req.spectrum_bands] or CANDIDATE_CELLS
    cs_list    = candidate_sites_from_cells(
        candidates,
        install_cost_usd=req.deployment_budget * 0.6 / max(len(candidates), 1),
        op_cost_usd=1_000.0,
    )

    mip_result = solve_bs_placement_mip(
        demand_by_period=demand_by_period,
        candidate_sites=cs_list,
        prop=PropagationParams(sinr_min_db=req.sinr_min_db),
        mode=req.demand_mode,
        time_limit_sec=req.mip_time_limit_sec,
    )

    if mip_result["status"] != "Optimal":
        raise HTTPException(422, detail={
            "error":      "MIP solver could not find an optimal solution",
            "status":     mip_result["status"],
            "msg":        mip_result["solver_msg"],
            "feasibility": mip_result.get("feasibility", {}),
        })

    selected_ids = set(mip_result["selected_sites"])
    final_cells  = [c for c in CANDIDATE_CELLS if c["cell_id"] in selected_ids]

    traffic = {
        "eMBB":  req.traffic_profile.eMBB,
        "URLLC": req.traffic_profile.URLLC,
        "mMTC":  req.traffic_profile.mMTC,
    }
    lat_constraints = {
        "e2e_ms":       req.latency_constraints.e2e_ms,
        "fronthaul_us": req.latency_constraints.fronthaul_us,
    }

    pipeline = _run_pipeline(
        cells=final_cells,
        traffic=traffic,
        lat_constraints=lat_constraints,
        spectrum_bands=req.spectrum_bands,
        max_cells_per_du=req.max_cells_per_du,
        max_dus_per_cu=req.max_dus_per_cu,
        is_new=True,
        build_schedule=mip_result["build_schedule"],
    )

    plan_id = str(uuid.uuid4())[:8]
    plan = {
        "plan_id":          plan_id,
        "plan_type":        "deploy",
        "planning_method":  "mip_multi_period",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "geographic_area":  req.geographic_area,
        "demand_mode":      req.demand_mode,
        "n_periods":        mip_result["n_periods"],
        "timing_sync":      pipeline["timing_sync"],
        "pci_violations":   pipeline["violations"],
        "cells":            pipeline["cell_plans"],
        "dus":              pipeline["du_plans"],
        "cus":              pipeline["cu_plans"],
        "build_schedule":   mip_result["build_schedule"],
        "period_assignments": mip_result["assignments"],
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
            "n_cells":               len(final_cells),
            "n_new_cells":           len(final_cells),
            "n_dus":                 len(pipeline["du_plans"]),
            "n_cus":                 len(pipeline["cu_plans"]),
            "total_capacity_ues":    sum(c["max_ues"] for c in final_cells),
            "estimated_cost_usd":    mip_result["total_cost"] or 0.0,
            "budget_utilisation_pct": round(
                (mip_result["total_cost"] or 0) / req.deployment_budget * 100, 1),
            "placement_method":      "mip_multi_period",
            "sufficiency_analysis": {
                "required_ues":     0,
                "current_capacity": 0,
                "mode_chosen":      "deploy",
            },
        },
    }
    _plans[plan_id] = plan
    log.info("Multi-period plan %s: %d sites, mode=%s, cost=%.0f",
             plan_id, len(final_cells), req.demand_mode, mip_result["total_cost"] or 0)
    return plan


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("planner_api:app", host="0.0.0.0", port=8081, reload=False)
