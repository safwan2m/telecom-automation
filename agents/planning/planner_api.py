#!/usr/bin/env python3
"""
Planning API — FastAPI service for network deployment planning.

Decides whether existing cells can satisfy demand (reorganize / suspend) or new
infrastructure is required (deploy / reactivate), then generates a complete
conflict-free plan.  All plan types return an identical unified schema.

POST /plan              → generate plan
POST /plan/apply        → push plan to Controller (live topology update)
GET  /plan/{id}         → retrieve a stored plan
GET  /plans             → list all persisted plans
GET  /cells/suspended   → list currently suspended cells
GET  /candidates        → list candidate cell inventory
"""

import copy
import json
import math
import os
import uuid
import httpx
import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from placement import (
    select_cells, assign_dus, assign_cus, du_centroid,
    estimate_cost, fronthaul_latency_us, midhaul_latency_ms,
    haversine_km, CANDIDATE_CELLS,
    MALLESWARAM_AREAS, cells_covering_area,
)
from pci_planner import assign_pcis, validate_plan
from slice_allocator import allocate, timing_sync_strategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://controller:8080")
INFLUX_URL     = os.environ.get("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN   = os.environ.get("INFLUX_TOKEN",  "telecom-super-secret-auth-token-2026")
INFLUX_ORG     = os.environ.get("INFLUX_ORG",    "telecom")
INFLUX_BUCKET  = os.environ.get("INFLUX_BUCKET", "telecom_metrics")

# Suspension thresholds
SUSPENSION_RATIO    = float(os.environ.get("SUSPENSION_RATIO", "2.0"))
MIN_ACTIVE_CELLS    = 1    # never suspend the last cell covering an area
MIN_CAPACITY_BUFFER = 1.1  # keep 10 % headroom above required_ues when deciding which cells to keep

# Diurnal load profile — matches core_simulator.py HOURLY_LOAD (index = hour 0–23)
HOURLY_LOAD = [
    0.08, 0.06, 0.05, 0.05, 0.06, 0.12,
    0.30, 0.65, 0.85, 0.80, 0.70, 0.65,
    0.65, 0.60, 0.62, 0.68, 0.78, 0.90,
    0.95, 1.00, 0.97, 0.88, 0.62, 0.30,
]

app = FastAPI(title="Telecom Planning API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Session cache — write-through to InfluxDB; prevents Flux round-trips for plans
# generated in the current process lifetime.
_plans: dict[str, dict] = {}

_influx_client: InfluxDBClient | None = None


def _get_influx() -> InfluxDBClient:
    global _influx_client
    if _influx_client is None:
        _influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    return _influx_client


def _write_plan(plan: dict) -> None:
    """Persist a plan to InfluxDB measurement 'plans'. Logs on failure; never raises."""
    try:
        p = (
            Point("plans")
            .tag("plan_id",     plan["plan_id"])
            .tag("plan_type",   plan["plan_type"])
            .field("plan_json",        json.dumps(plan))
            .field("geographic_area",  plan.get("geographic_area", ""))
            .field("planning_method",  plan.get("planning_method", ""))
        )
        _get_influx().write_api(write_options=SYNCHRONOUS).write(
            bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=[p]
        )
        log.info("Plan %s persisted to InfluxDB.", plan["plan_id"])
    except Exception as exc:
        log.warning("InfluxDB write failed for plan %s: %s", plan.get("plan_id"), exc)


def _read_plan(plan_id: str) -> dict | None:
    """Read a plan from InfluxDB by plan_id tag. Returns None on miss or error."""
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -90d)
  |> filter(fn: (r) => r._measurement == "plans")
  |> filter(fn: (r) => r.plan_id == "{plan_id}")
  |> filter(fn: (r) => r._field == "plan_json")
  |> last()
"""
    try:
        tables = _get_influx().query_api().query(flux, org=INFLUX_ORG)
        for table in tables:
            for rec in table.records:
                return json.loads(rec.get_value())
    except Exception as exc:
        log.warning("InfluxDB read failed for plan %s: %s", plan_id, exc)
    return None


def _store_plan(plan: dict) -> None:
    """Write to session cache and persist to InfluxDB."""
    _plans[plan["plan_id"]] = plan
    _write_plan(plan)


def _fetch_plan(plan_id: str) -> dict | None:
    """Return plan from session cache, falling back to InfluxDB."""
    if plan_id in _plans:
        return _plans[plan_id]
    plan = _read_plan(plan_id)
    if plan:
        _plans[plan_id] = plan   # warm the cache
    return plan


# ── Suspended cell registry ──────────────────────────────────────────────────

def _write_suspension_events(
    cells: list[dict],
    geographic_area: str,
    action: str,           # "suspended" | "reactivated"
    plan_id: str,
) -> None:
    """
    Write one InfluxDB point per cell to measurement 'suspended_cells'.
    Stores the full cell dict (stripped of internal/computed fields) plus
    action and plan_id so the latest event per cell can be queried to
    determine current suspension status.  Never raises.
    """
    try:
        wa = _get_influx().write_api(write_options=SYNCHRONOUS)
        points = []
        for c in cells:
            clean = {
                k: v for k, v in c.items()
                if not k.startswith("_")
                and k not in ("active", "coverage_radius_km", "distance_to_area_km",
                              "area_coverage_fraction", "slices", "slice_warnings",
                              "is_new", "du_id", "cu_id", "fronthaul_latency_us")
            }
            if "_existing_pci" in c:
                clean["pci"] = c["_existing_pci"]
            cell_record = {**clean, "action": action, "plan_id": plan_id,
                           "geographic_area": geographic_area}
            p = (
                Point("suspended_cells")
                .tag("cell_id",          c["cell_id"])
                .tag("geographic_area",  geographic_area)
                .field("cell_json",      json.dumps(cell_record))
            )
            points.append(p)
        wa.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
        log.info("Wrote %d '%s' events for area %s.", len(cells), action, geographic_area)
    except Exception as exc:
        log.warning("InfluxDB suspension event write failed: %s", exc)


def _get_suspended_cells(geographic_area: str) -> list[dict]:
    """
    Return the currently suspended cells for geographic_area.
    Queries InfluxDB: group by cell_id, take last event, keep action=='suspended'.
    Returns [] on InfluxDB error (treated as no suspended cells).
    """
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -36500d)
  |> filter(fn: (r) => r._measurement == "suspended_cells")
  |> filter(fn: (r) => r.geographic_area == "{geographic_area}")
  |> filter(fn: (r) => r._field == "cell_json")
  |> group(columns: ["cell_id"])
  |> last()
"""
    cells = []
    try:
        tables = _get_influx().query_api().query(flux, org=INFLUX_ORG)
        for table in tables:
            for rec in table.records:
                cell_data = json.loads(rec.get_value())
                if cell_data.get("action") == "suspended":
                    cells.append(cell_data)
    except Exception as exc:
        log.warning("InfluxDB get_suspended_cells failed for area %s: %s", geographic_area, exc)
    return cells


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


class ApplyRequest(BaseModel):
    plan_id: str


# ── Shared helpers ───────────────────────────────────────────────────────────

def _resolve_area(geographic_area: str) -> dict | None:
    """Resolve a free-text area name or area_id to a MALLESWARAM_AREAS entry.
    Tries exact area_id match first, then case-insensitive substring on name/area_id."""
    q = geographic_area.lower()
    return next(
        (a for a in MALLESWARAM_AREAS
         if q == a["area_id"].lower()
         or q in a["name"].lower()
         or a["name"].lower() in q
         or q in a["area_id"].lower()),
        None,
    )


def _area_km2(geographic_area: str, area_meta: dict | None = None) -> float:
    """Area in km².  Uses π·r² from MALLESWARAM_AREAS when known; bounding-box fallback."""
    if area_meta:
        return round(math.pi * area_meta["radius_km"] ** 2, 4)
    matches = [
        c for c in CANDIDATE_CELLS
        if geographic_area.lower() in c["area"].lower()
        or c["area"].lower() in geographic_area.lower()
    ] or CANDIDATE_CELLS
    lats = [c["lat"] for c in matches]
    lons = [c["lon"] for c in matches]
    lat_km = haversine_km(min(lats), min(lons), max(lats), min(lons))
    lon_km = haversine_km(min(lats), min(lons), min(lats), max(lons))
    return round(max(lat_km * lon_km, 0.01), 4)


def _area_matches(cell_area: str, target: str) -> bool:
    return target.lower() in cell_area.lower() or cell_area.lower() in target.lower()


def _sufficiency_check(
    req: PlanRequest,
    area_meta: dict | None = None,
) -> tuple[dict, list[dict], list[dict]]:
    """
    Query the live network, compute required UEs at peak hour, decide planning mode.

    Returns (analysis, active_covering, suspended_cells):
      active_covering:  cells from Controller that are active (active != false) and cover the area
      suspended_cells:  cells from InfluxDB suspended registry currently suspended for this area

    Five-way mode decision (evaluated top-to-bottom, first match wins):
      1. deployed_ues >= required_ues * SUSPENSION_RATIO and n_active > MIN_ACTIVE_CELLS
             → "suspend"
      2. deployed_ues >= required_ues
             → "reorganize"
      3. deployed_ues + suspended_ues >= required_ues
             → "reactivate"
      4. suspended_ues > 0
             → "reactivate_and_deploy"
      5. else
             → "deploy"
    """
    area_km2     = _area_km2(req.geographic_area, area_meta)
    peak_hour    = req.traffic_profile.peak_hour
    lf           = HOURLY_LOAD[peak_hour]
    required_ues = round(req.expected_user_density * area_km2 * lf)

    all_live: list[dict] = []
    try:
        resp = httpx.get(f"{CONTROLLER_URL}/network", timeout=5.0)
        resp.raise_for_status()
        for cell_id, cd in resp.json().get("cells", {}).items():
            all_live.append({
                "cell_id":        cell_id,
                "area":           cd.get("area", req.geographic_area),
                "lat":            cd["lat"],
                "lon":            cd["lon"],
                "band":           cd.get("band", "n78"),
                "freq_mhz":       cd.get("freq_mhz", 3500),
                "max_ues":        cd.get("max_ues", 900),
                "density_weight": cd.get("max_ues", 900) / 900.0,
                "generation":     cd.get("generation", "5G"),
                "vendor":         cd.get("vendor", "Nokia"),
                "hardware_model": cd.get("hardware_model", "AirScale MAA 64T64R"),
                "antenna_config": cd.get("antenna_config", "64T64R"),
                "tx_power_w":     cd.get("tx_power_w", 1000),
                "idle_power_w":   cd.get("idle_power_w", 250),
                "peak_dl_mbps":   cd.get("peak_dl_mbps", 3800),
                "_existing_pci":  cd.get("pci", 0),
                "active":         cd.get("active", True),
            })
    except Exception as exc:
        log.warning("Controller unreachable for sufficiency check: %s", exc)

    if area_meta:
        covering_cells = cells_covering_area(area_meta, all_live)
    else:
        covering_cells = [c for c in all_live if _area_matches(c["area"], req.geographic_area)]

    active_covering = [c for c in covering_cells if c.get("active", True)]
    deployed_ues    = sum(c.get("max_ues", 0) for c in active_covering)

    # Always fetch suspended cells — needed for mode decision and analysis
    suspended_cells = _get_suspended_cells(req.geographic_area)
    suspended_ues   = sum(c.get("max_ues", 0) for c in suspended_cells)

    if (deployed_ues >= required_ues * SUSPENSION_RATIO
            and len(active_covering) > MIN_ACTIVE_CELLS):
        mode = "suspend"
    elif deployed_ues >= required_ues:
        mode = "reorganize"
    elif deployed_ues + suspended_ues >= required_ues:
        mode = "reactivate"
    elif suspended_ues > 0:
        mode = "reactivate_and_deploy"
    else:
        mode = "deploy"

    analysis = {
        "area_km2":           area_km2,
        "required_ues":       required_ues,
        "active_capacity":    deployed_ues,
        "suspended_capacity": suspended_ues,
        "peak_hour":          peak_hour,
        "load_factor":        lf,
        "mode_chosen":        mode,
    }
    return analysis, active_covering, suspended_cells


def _run_pipeline(
    cells: list[dict],
    traffic: dict,
    lat_constraints: dict,
    spectrum_bands: list[str],
    max_cells_per_du: int,
    max_dus_per_cu: int,
    is_new: bool = True,
    keep_pcis: dict | None = None,
) -> dict:
    """
    Shared downstream pipeline: PCI → DU → CU → centroids → timing → slices.

    keep_pcis: if provided, uses these PCIs instead of re-running assignment
               (reorganize / reactivate mode — preserve existing cell PCIs).
    Returns dict with cell_plans, du_plans, cu_plans, timing_sync, violations, du_cells.
    """
    if keep_pcis is not None:
        pcis = keep_pcis
    else:
        pcis = assign_pcis(cells)
    pci_check = validate_plan(cells, pcis)
    if pci_check["collisions"]:
        log.warning("PCI plan has %d collision(s): %s", len(pci_check["collisions"]), pci_check["collisions"][:3])
    if pci_check["confusions"]:
        log.info("PCI plan has %d confusion(s) (best-effort, expected in dense networks)", len(pci_check["confusions"]))

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
        "cell_plans":    cell_plans,
        "du_plans":      du_plans,
        "cu_plans":      cu_plans,
        "timing_sync":   timing_sync,
        "violations":    pci_check["collisions"],
        "pci_confusions": pci_check["confusions"],
        "du_cells":      du_cells,
    }


# ── Planning logic ───────────────────────────────────────────────────────────

def generate_plan(req: PlanRequest) -> dict:
    area_meta = _resolve_area(req.geographic_area)
    traffic = {
        "eMBB":  req.traffic_profile.eMBB,
        "URLLC": req.traffic_profile.URLLC,
        "mMTC":  req.traffic_profile.mMTC,
    }
    lat_constraints = {
        "e2e_ms":       req.latency_constraints.e2e_ms,
        "fronthaul_us": req.latency_constraints.fronthaul_us,
    }

    sa, active_covering, suspended_cells = _sufficiency_check(req, area_meta)
    mode = sa["mode_chosen"]

    if mode == "suspend":
        return _suspend_plan(req, sa, active_covering, traffic, lat_constraints)
    if mode == "reorganize" and active_covering:
        return _reorganize_plan(req, sa, active_covering, traffic, lat_constraints)
    if mode == "reactivate":
        return _reactivate_plan(req, sa, active_covering, suspended_cells, traffic, lat_constraints)
    if mode == "reactivate_and_deploy":
        return _reactivate_and_deploy_plan(
            req, sa, active_covering, suspended_cells, traffic, lat_constraints, area_meta
        )
    return _deploy_plan(req, sa, traffic, lat_constraints, area_meta)


def _reorganize_plan(
    req: PlanRequest,
    sa: dict,
    active_covering: list[dict],
    traffic: dict,
    lat_constraints: dict,
) -> dict:
    """Rebalance DU assignments + reallocate slices for existing active cells."""
    keep_pcis = {c["cell_id"]: c["_existing_pci"] for c in active_covering}
    pipeline  = _run_pipeline(
        cells=active_covering,
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
        "timing_sync":      pipeline["timing_sync"],
        "pci_violations":   pipeline["violations"],
        "pci_confusions":   pipeline["pci_confusions"],
        "cells":            pipeline["cell_plans"],
        "dus":              pipeline["du_plans"],
        "cus":              pipeline["cu_plans"],
        "summary": {
            "n_cells":                len(active_covering),
            "n_new_cells":            0,
            "n_dus":                  len(pipeline["du_plans"]),
            "n_cus":                  len(pipeline["cu_plans"]),
            "total_capacity_ues":     sum(c["max_ues"] for c in active_covering),
            "estimated_cost_usd":     0.0,
            "budget_utilisation_pct": 0.0,
            "placement_method":       "power_rebalance",
            "sufficiency_analysis":   sa,
        },
    }


def _deploy_plan(
    req: PlanRequest,
    sa: dict,
    traffic: dict,
    lat_constraints: dict,
    area_meta: dict | None = None,
) -> dict:
    """Select new cells via heuristic and run the full pipeline."""
    if area_meta:
        max_dist = area_meta["radius_km"] + 0.5
        candidate_pool = [
            c for c in CANDIDATE_CELLS
            if haversine_km(c["lat"], c["lon"], area_meta["lat"], area_meta["lon"]) <= max_dist
        ] or CANDIDATE_CELLS
        area_center = (area_meta["lat"], area_meta["lon"])
    else:
        candidate_pool = CANDIDATE_CELLS
        area_center    = None

    cells = select_cells(
        req.expected_user_density, req.deployment_budget, req.spectrum_bands,
        candidate_pool=candidate_pool, area_center=area_center,
    )

    pipeline = _run_pipeline(
        cells=cells,
        traffic=traffic,
        lat_constraints=lat_constraints,
        spectrum_bands=req.spectrum_bands,
        max_cells_per_du=req.max_cells_per_du,
        max_dus_per_cu=req.max_dus_per_cu,
        is_new=True,
    )

    estimated_cost = estimate_cost(
        len(cells), len(pipeline["du_plans"]), len(pipeline["cu_plans"])
    )

    plan_id = str(uuid.uuid4())[:8]
    return {
        "plan_id":          plan_id,
        "plan_type":        "deploy",
        "planning_method":  "heuristic",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "geographic_area":  req.geographic_area,
        "timing_sync":      pipeline["timing_sync"],
        "pci_violations":   pipeline["violations"],
        "pci_confusions":   pipeline["pci_confusions"],
        "cells":            pipeline["cell_plans"],
        "dus":              pipeline["du_plans"],
        "cus":              pipeline["cu_plans"],
        "summary": {
            "n_cells":                len(cells),
            "n_new_cells":            len(cells),
            "n_dus":                  len(pipeline["du_plans"]),
            "n_cus":                  len(pipeline["cu_plans"]),
            "total_capacity_ues":     sum(c["max_ues"] for c in cells),
            "estimated_cost_usd":     estimated_cost,
            "budget_utilisation_pct": round(estimated_cost / req.deployment_budget * 100, 1),
            "placement_method":       "heuristic",
            "sufficiency_analysis":   sa,
        },
    }


def _suspend_plan(
    req: PlanRequest,
    sa: dict,
    active_covering: list[dict],
    traffic: dict,
    lat_constraints: dict,
) -> dict:
    """
    Identify minimum cells needed to meet required demand; mark the rest suspended.

    Strategy: sort active cells by max_ues descending; greedily keep cells until
    Σ max_ues >= required_ues * MIN_CAPACITY_BUFFER (10 % headroom). Suspend the rest.
    At least MIN_ACTIVE_CELLS (1) cell always stays active.

    Suspension events are written to InfluxDB so they can be retrieved later.
    Suspended cells are excluded from the topology written by plan_to_topology
    (they are listed in the plan for transparency but not deployed to the Controller).
    """
    required_ues = sa["required_ues"]
    keep_target  = required_ues * MIN_CAPACITY_BUFFER

    sorted_cells = sorted(active_covering, key=lambda c: c.get("max_ues", 0), reverse=True)

    kept       = []
    to_suspend = []
    running    = 0
    for c in sorted_cells:
        if running < keep_target or len(kept) < MIN_ACTIVE_CELLS:
            kept.append({**c, "active": True})
            running += c.get("max_ues", 0)
        else:
            to_suspend.append({**c, "active": False})

    plan_id = str(uuid.uuid4())[:8]

    if to_suspend:
        _write_suspension_events(to_suspend, req.geographic_area, "suspended", plan_id)

    keep_pcis = {c["cell_id"]: c["_existing_pci"] for c in kept}
    pipeline  = _run_pipeline(
        cells=kept,
        traffic=traffic,
        lat_constraints=lat_constraints,
        spectrum_bands=req.spectrum_bands,
        max_cells_per_du=req.max_cells_per_du,
        max_dus_per_cu=req.max_dus_per_cu,
        is_new=False,
        keep_pcis=keep_pcis,
    )

    # Suspended cells included in plan cells list for transparency (but not in topology)
    suspended_cell_entries = [
        {
            **{k: v for k, v in c.items() if not k.startswith("_")},
            "active":            False,
            "suspended_reason":  "demand_reduction",
        }
        for c in to_suspend
    ]

    return {
        "plan_id":          plan_id,
        "plan_type":        "suspend",
        "planning_method":  "suspension",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "geographic_area":  req.geographic_area,
        "timing_sync":      pipeline["timing_sync"],
        "pci_violations":   pipeline["violations"],
        "cells":            pipeline["cell_plans"] + suspended_cell_entries,
        "dus":              pipeline["du_plans"],
        "cus":              pipeline["cu_plans"],
        "suspended_cells":  [c["cell_id"] for c in to_suspend],
        "summary": {
            "n_cells":                len(active_covering),
            "n_cells_kept_active":    len(kept),
            "n_cells_suspended":      len(to_suspend),
            "n_new_cells":            0,
            "n_dus":                  len(pipeline["du_plans"]),
            "n_cus":                  len(pipeline["cu_plans"]),
            "total_capacity_ues":     sum(c.get("max_ues", 0) for c in kept),
            "estimated_cost_usd":     0.0,
            "budget_utilisation_pct": 0.0,
            "placement_method":       "suspension",
            "sufficiency_analysis":   sa,
        },
    }


def _reactivate_plan(
    req: PlanRequest,
    sa: dict,
    active_covering: list[dict],
    suspended_cells: list[dict],
    traffic: dict,
    lat_constraints: dict,
) -> dict:
    """
    Reactivate the minimum subset of suspended cells to meet required demand.

    Sort suspended cells by max_ues descending; greedily wake them until
    deployed_ues + Σ reactivated max_ues >= required_ues.
    Writes reactivation events to InfluxDB.
    """
    required_ues = sa["required_ues"]
    deployed_ues = sa["active_capacity"]
    deficit      = required_ues - deployed_ues

    sorted_suspended = sorted(suspended_cells, key=lambda c: c.get("max_ues", 0), reverse=True)

    to_reactivate = []
    covered       = 0
    for c in sorted_suspended:
        if covered >= deficit:
            break
        to_reactivate.append(c)
        covered += c.get("max_ues", 0)

    plan_id = str(uuid.uuid4())[:8]

    if to_reactivate:
        _write_suspension_events(to_reactivate, req.geographic_area, "reactivated", plan_id)

    # Prepare reactivated cells: strip event-metadata keys, ensure density_weight present
    cleaned = []
    for c in to_reactivate:
        entry = {k: v for k, v in c.items() if k not in ("action", "plan_id", "geographic_area")}
        entry["active"] = True
        entry.setdefault("density_weight", entry.get("max_ues", 900) / 900.0)
        cleaned.append(entry)

    all_cells = active_covering + cleaned

    # Preserve PCIs: active cells use _existing_pci; reactivated cells use stored pci
    keep_pcis = {
        c["cell_id"]: c.get("_existing_pci", c.get("pci", 0))
        for c in all_cells
    }

    pipeline = _run_pipeline(
        cells=all_cells,
        traffic=traffic,
        lat_constraints=lat_constraints,
        spectrum_bands=req.spectrum_bands,
        max_cells_per_du=req.max_cells_per_du,
        max_dus_per_cu=req.max_dus_per_cu,
        is_new=False,
        keep_pcis=keep_pcis,
    )

    return {
        "plan_id":           plan_id,
        "plan_type":         "reactivate",
        "planning_method":   "reactivation",
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "geographic_area":   req.geographic_area,
        "timing_sync":       pipeline["timing_sync"],
        "pci_violations":    pipeline["violations"],
        "cells":             pipeline["cell_plans"],
        "dus":               pipeline["du_plans"],
        "cus":               pipeline["cu_plans"],
        "reactivated_cells": [c["cell_id"] for c in to_reactivate],
        "summary": {
            "n_cells":                len(all_cells),
            "n_new_cells":            0,
            "n_reactivated_cells":    len(to_reactivate),
            "n_dus":                  len(pipeline["du_plans"]),
            "n_cus":                  len(pipeline["cu_plans"]),
            "total_capacity_ues":     sum(c.get("max_ues", 0) for c in all_cells),
            "estimated_cost_usd":     0.0,
            "budget_utilisation_pct": 0.0,
            "placement_method":       "reactivation",
            "sufficiency_analysis":   sa,
        },
    }


def _reactivate_and_deploy_plan(
    req: PlanRequest,
    sa: dict,
    active_covering: list[dict],
    suspended_cells: list[dict],
    traffic: dict,
    lat_constraints: dict,
    area_meta: dict | None = None,
) -> dict:
    """
    Reactivate ALL suspended cells for the area, then deploy new cells for the
    remaining deficit.  PCI assignment is done fresh across all cells (active +
    reactivated + new) since the mixed pool makes preservation impractical.
    """
    plan_id = str(uuid.uuid4())[:8]

    if suspended_cells:
        _write_suspension_events(suspended_cells, req.geographic_area, "reactivated", plan_id)

    cleaned = []
    for c in suspended_cells:
        entry = {k: v for k, v in c.items() if k not in ("action", "plan_id", "geographic_area")}
        entry["active"] = True
        entry.setdefault("density_weight", entry.get("max_ues", 900) / 900.0)
        cleaned.append(entry)

    # Compute remaining deficit after full reactivation
    total_after_reactivation = sa["active_capacity"] + sa["suspended_capacity"]
    remaining_deficit        = sa["required_ues"] - total_after_reactivation

    # Proximity pre-filter for new candidates
    if area_meta:
        max_dist = area_meta["radius_km"] + 0.5
        candidate_pool = [
            c for c in CANDIDATE_CELLS
            if haversine_km(c["lat"], c["lon"], area_meta["lat"], area_meta["lon"]) <= max_dist
        ] or CANDIDATE_CELLS
        area_center = (area_meta["lat"], area_meta["lon"])
    else:
        candidate_pool = CANDIDATE_CELLS
        area_center    = None

    # Exclude candidates already in the active or reactivated set
    existing_ids   = {c["cell_id"] for c in active_covering + cleaned}
    candidate_pool = [c for c in candidate_pool if c["cell_id"] not in existing_ids]

    new_cells: list[dict] = []
    if remaining_deficit > 0 and candidate_pool:
        delta_budget = req.deployment_budget * (remaining_deficit / max(sa["required_ues"], 1))
        new_cells = select_cells(
            req.expected_user_density, delta_budget, req.spectrum_bands,
            candidate_pool=candidate_pool, area_center=area_center,
        )
    for c in new_cells:
        c["active"] = True

    all_cells = active_covering + cleaned + new_cells

    # Fresh PCI assignment for the whole mixed pool (keep_pcis=None)
    pipeline = _run_pipeline(
        cells=all_cells,
        traffic=traffic,
        lat_constraints=lat_constraints,
        spectrum_bands=req.spectrum_bands,
        max_cells_per_du=req.max_cells_per_du,
        max_dus_per_cu=req.max_dus_per_cu,
        is_new=False,
        keep_pcis=None,
    )

    # Mark newly deployed cells as is_new=True
    new_ids = {c["cell_id"] for c in new_cells}
    for cp in pipeline["cell_plans"]:
        if cp["cell_id"] in new_ids:
            cp["is_new"] = True

    estimated_cost = estimate_cost(
        len(new_cells), len(pipeline["du_plans"]), len(pipeline["cu_plans"])
    )

    return {
        "plan_id":           plan_id,
        "plan_type":         "reactivate_and_deploy",
        "planning_method":   "reactivation_and_heuristic",
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "geographic_area":   req.geographic_area,
        "timing_sync":       pipeline["timing_sync"],
        "pci_violations":    pipeline["violations"],
        "cells":             pipeline["cell_plans"],
        "dus":               pipeline["du_plans"],
        "cus":               pipeline["cu_plans"],
        "reactivated_cells": [c["cell_id"] for c in suspended_cells],
        "summary": {
            "n_cells":                len(all_cells),
            "n_new_cells":            len(new_cells),
            "n_reactivated_cells":    len(suspended_cells),
            "n_dus":                  len(pipeline["du_plans"]),
            "n_cus":                  len(pipeline["cu_plans"]),
            "total_capacity_ues":     sum(c.get("max_ues", 0) for c in all_cells),
            "estimated_cost_usd":     estimated_cost,
            "budget_utilisation_pct": round(estimated_cost / max(req.deployment_budget, 1) * 100, 1),
            "placement_method":       "reactivation_and_heuristic",
            "sufficiency_analysis":   sa,
        },
    }


def plan_to_topology(plan: dict) -> dict:
    """Convert a network plan into topology.json format used by the Controller.

    Suspended cells (active=False) are excluded — they exist only in InfluxDB.
    The DU simulator never sees them; they are invisible to the live network
    until a reactivate plan re-adds them to a DU's cell_ids.

    Call _remap_infrastructure_ids() on the result before applying to a live
    deployment so that existing DU/CU hardware IDs are preserved.
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
        if not c.get("active", True):
            continue   # suspended cells are not written to topology
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


def _remap_infrastructure_ids(plan_topology: dict, plan: dict, existing_topo: dict) -> dict:
    """Merge a plan into the existing topology, preserving infrastructure IDs.

    A plan often covers only a subset of the live network (cells in the queried
    area).  This function:

      1. Starts from the existing topology (all cells/DUs/CUs intact).
      2. Removes the plan's cells from whichever DUs they currently sit in.
      3. Removes cells listed in plan["suspended_cells"] from the topology.
      4. Maps each plan DU to the geographically nearest existing DU.
      5. Adds the plan's active cells to their remapped DU's cell_ids list.
      6. Adds or updates cell configs for plan cells.
      7. Drops empty DUs and rebuilds each CU's du_ids list.

    If no existing DUs are found (fresh deploy) the plan topology is returned
    unchanged.
    """
    import copy

    existing_dus = existing_topo.get("dus", {})
    if not existing_dus:
        return plan_topology  # fresh deploy — no remapping needed

    merged      = copy.deepcopy(existing_topo)
    plan_cells  = plan_topology.get("cells", {})
    plan_dus    = plan_topology.get("dus", {})
    suspended   = set(plan.get("suspended_cells", []))

    def _centroid(cell_ids: list, cell_store: dict):
        pts = [(cell_store[c]["lat"], cell_store[c]["lon"]) for c in cell_ids if c in cell_store]
        if not pts:
            return None
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)

    # Centroids for existing DUs (from existing cell coords)
    ex_centroids = {
        du_id: _centroid(cfg.get("cell_ids", []), merged["cells"])
        for du_id, cfg in existing_dus.items()
    }
    ex_centroids = {k: v for k, v in ex_centroids.items() if v}
    existing_du_list = list(ex_centroids.keys())

    # Map each plan DU → nearest existing DU by centroid distance
    du_remap: dict[str, str] = {}
    for plan_du_id, plan_du_cfg in plan_dus.items():
        plan_cent = _centroid(plan_du_cfg.get("cell_ids", []), plan_cells)
        if plan_cent and existing_du_list:
            nearest = min(
                existing_du_list,
                key=lambda eid: haversine_km(plan_cent[0], plan_cent[1], *ex_centroids[eid]),
            )
            du_remap[plan_du_id] = nearest
        else:
            du_remap[plan_du_id] = plan_du_id

    # Remove plan cells (active + suspended) from their current DU positions
    all_plan_cell_ids = set(plan_cells.keys()) | suspended
    for du_cfg in merged["dus"].values():
        du_cfg["cell_ids"] = [c for c in du_cfg["cell_ids"] if c not in all_plan_cell_ids]

    # Remove suspended cells entirely from the cells dict
    for cell_id in suspended:
        merged["cells"].pop(cell_id, None)

    # Add / update active plan cell configs
    merged["cells"].update(plan_cells)

    # Place plan cells into their remapped DUs
    for plan_du_id, plan_du_cfg in plan_dus.items():
        target_du = du_remap.get(plan_du_id, plan_du_id)
        if target_du in merged["dus"]:
            existing_non_plan = [c for c in merged["dus"][target_du]["cell_ids"]
                                 if c not in set(plan_du_cfg["cell_ids"])]
            merged["dus"][target_du]["cell_ids"] = existing_non_plan + plan_du_cfg["cell_ids"]
        else:
            # Target DU doesn't exist yet — create it under the first available CU
            fallback_cu = next(iter(merged["cus"]), "CU-MLS")
            merged["dus"][target_du] = {
                "cu_id":    fallback_cu,
                "host":     target_du.lower(),
                "cell_ids": plan_du_cfg["cell_ids"],
            }

    # Drop newly-added DUs that ended up with no cells, but always keep DUs that
    # existed before the plan (their simulator is still running and looks itself up).
    existing_du_ids = set(existing_dus.keys())
    merged["dus"] = {
        k: v for k, v in merged["dus"].items()
        if v.get("cell_ids") or k in existing_du_ids
    }

    # Rebuild each CU's du_ids from the current DU→CU assignments
    for cu_id, cu_cfg in merged["cus"].items():
        cu_cfg["du_ids"] = [du_id for du_id, du_cfg in merged["dus"].items()
                            if du_cfg.get("cu_id") == cu_id]

    # Drop CUs that own no DUs (only if they weren't in existing topology)
    existing_cu_ids = set(existing_topo.get("cus", {}).keys())
    merged["cus"] = {
        k: v for k, v in merged["cus"].items()
        if v.get("du_ids") or k in existing_cu_ids
    }

    return merged


# ── Input validation ─────────────────────────────────────────────────────────

_PLAN_REQUIRED = [
    "geographic_area", "expected_user_density", "traffic_profile",
    "spectrum_bands", "deployment_budget", "latency_constraints",
]


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
    try:
        _get_influx().ping()
        influx_ok = True
    except Exception:
        influx_ok = False
    return {"status": "ok", "influxdb": influx_ok}


@app.get("/plans")
def list_plans():
    """List all plans stored in InfluxDB (last 90 days), most recent first."""
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -90d)
  |> filter(fn: (r) => r._measurement == "plans")
  |> filter(fn: (r) => r._field == "geographic_area")
  |> last()
  |> keep(columns: ["_time", "plan_id", "plan_type", "_value"])
"""
    try:
        tables = _get_influx().query_api().query(flux, org=INFLUX_ORG)
        rows = []
        seen: set[str] = set()
        for table in tables:
            for rec in table.records:
                pid = rec.values.get("plan_id")
                if pid and pid not in seen:
                    seen.add(pid)
                    rows.append({
                        "plan_id":         pid,
                        "plan_type":       rec.values.get("plan_type"),
                        "geographic_area": rec.get_value(),
                        "timestamp":       rec.get_time().isoformat() if rec.get_time() else None,
                    })
        rows.sort(key=lambda r: r["timestamp"] or "", reverse=True)
        return {"plans": rows, "count": len(rows)}
    except Exception as exc:
        log.warning("InfluxDB list_plans query failed: %s", exc)
        return {
            "plans": [
                {"plan_id": p["plan_id"], "plan_type": p["plan_type"],
                 "geographic_area": p.get("geographic_area"), "timestamp": p.get("timestamp")}
                for p in _plans.values()
            ],
            "count": len(_plans),
            "source": "session_cache",
        }


@app.get("/cells/suspended")
def list_suspended_cells(area: Optional[str] = None):
    """List currently suspended cells across all areas, or filtered by ?area=."""
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -36500d)
  |> filter(fn: (r) => r._measurement == "suspended_cells")
  |> filter(fn: (r) => r._field == "cell_json")
  |> group(columns: ["cell_id"])
  |> last()
"""
    cells = []
    try:
        tables = _get_influx().query_api().query(flux, org=INFLUX_ORG)
        for table in tables:
            for rec in table.records:
                cell_data = json.loads(rec.get_value())
                if cell_data.get("action") == "suspended":
                    cells.append(cell_data)
    except Exception as exc:
        log.warning("InfluxDB list_suspended_cells failed: %s", exc)
        return {"suspended_cells": [], "count": 0}

    if area:
        q = area.lower()
        cells = [
            c for c in cells
            if q in c.get("geographic_area", "").lower()
            or q in c.get("area", "").lower()
        ]

    return {"suspended_cells": cells, "count": len(cells)}


@app.post("/plan")
def create_plan(req: PlanRequest):
    if (missing := _missing_fields_response(req, _PLAN_REQUIRED)):
        return missing
    plan = generate_plan(req)
    _store_plan(plan)
    log.info("Plan %s (%s): %s", plan["plan_id"], plan["plan_type"], plan["summary"])
    return plan


@app.get("/plan/{plan_id}")
def get_plan(plan_id: str):
    plan = _fetch_plan(plan_id)
    if plan is None:
        raise HTTPException(404, f"Plan {plan_id} not found")
    return plan


@app.post("/plan/apply")
def apply_plan(req: ApplyRequest):
    plan = _fetch_plan(req.plan_id)
    if plan is None:
        raise HTTPException(404, f"Plan {req.plan_id} not found")

    topology = plan_to_topology(plan)

    # Merge plan changes into the existing topology, preserving infrastructure IDs.
    # This maps abstract planner DU/CU IDs (e.g. DU-BLR-01) to hardware IDs
    # (e.g. DU-MLS-1) and keeps all non-plan cells intact so simulators keep working.
    try:
        ex = httpx.get(f"{CONTROLLER_URL}/topology", timeout=5.0)
        if ex.status_code == 200:
            topology = _remap_infrastructure_ids(topology, plan, ex.json())
    except Exception as exc:
        log.warning("ID remap skipped (topology fetch failed): %s", exc)

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


@app.get("/areas")
def list_areas():
    return MALLESWARAM_AREAS


@app.get("/areas/{area_id}/cells")
def area_cells(area_id: str):
    area = _resolve_area(area_id)
    if not area:
        raise HTTPException(404, f"Area {area_id!r} not found — use GET /areas to list valid names")

    live_cells: list[dict] = []
    try:
        resp = httpx.get(f"{CONTROLLER_URL}/network", timeout=5.0)
        resp.raise_for_status()
        for cell_id, cd in resp.json().get("cells", {}).items():
            live_cells.append({"cell_id": cell_id, **cd})
    except Exception as exc:
        log.warning("Controller unreachable for area coverage query: %s", exc)

    covering = cells_covering_area(area, live_cells)
    return {
        "area":           area,
        "n_covering":     len(covering),
        "covering_cells": covering,
    }


@app.get("/candidates")
def list_candidates():
    return CANDIDATE_CELLS


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("planner_api:app", host="0.0.0.0", port=8081, reload=False)
