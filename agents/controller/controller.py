#!/usr/bin/env python3
"""
Network Controller — single control plane for the telecom dev environment.

REST API to:
  - Load current topology and live KPIs from InfluxDB
  - Move a cell from one DU to another
  - Reassign a DU to a different CU
  - Query per-cell, per-DU, per-CU metrics

Topology changes are written atomically to /config/topology.json.
DU and CU simulators poll that file and reconfigure themselves live.
"""

import json
import os
import shutil
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

INFLUX_URL    = os.environ.get("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN",  "telecom-super-secret-auth-token-2026")
INFLUX_ORG    = os.environ.get("INFLUX_ORG",    "telecom")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "telecom_metrics")
TOPOLOGY_FILE = Path(os.environ.get("TOPOLOGY_FILE", "/config/topology.json"))

app = FastAPI(title="Telecom Network Controller", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_influx: Optional[InfluxDBClient] = None


def get_influx() -> InfluxDBClient:
    global _influx
    if _influx is None:
        _influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    return _influx


# ── Topology helpers ─────────────────────────────────────────────────────────

def read_topology() -> dict:
    with open(TOPOLOGY_FILE) as f:
        return json.load(f)


def write_topology(topo: dict, updated_by: str = "controller") -> None:
    topo["version"]      = topo.get("version", 0) + 1
    topo["last_updated"] = datetime.now(timezone.utc).isoformat()
    topo["updated_by"]   = updated_by

    # Atomic write: write to temp file then rename
    tmp = TOPOLOGY_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(topo, f, indent=2)
    shutil.move(str(tmp), str(TOPOLOGY_FILE))
    log.info(f"Topology written v{topo['version']} by {updated_by}")


def _record_event(event_type: str, details: dict) -> None:
    try:
        wa = get_influx().write_api(write_options=SYNCHRONOUS)
        p  = Point("topology_event").tag("event_type", event_type)
        for k, v in details.items():
            p = p.field(k, str(v))
        wa.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=[p])
    except Exception as e:
        log.warning(f"Could not record topology event: {e}")


# ── InfluxDB query helpers ───────────────────────────────────────────────────

def _query(flux: str) -> list[dict]:
    try:
        tables = get_influx().query_api().query(flux, org=INFLUX_ORG)
        rows   = []
        for table in tables:
            for rec in table.records:
                rows.append(dict(rec.values))
        return rows
    except Exception as e:
        log.warning(f"InfluxDB query error: {e}")
        return []


def latest_cell_kpis() -> dict[str, dict]:
    """Return {cell_id: {field: value, ...}} for the most recent data point per cell."""
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "cell_kpi")
  |> last()
  |> pivot(rowKey: ["_time","cell_id","area","band","pci","du_id","cu_id"],
           columnKey: ["_field"], valueColumn: "_value")
"""
    rows   = _query(flux)
    result = {}
    for r in rows:
        cid = r.get("cell_id")
        if cid:
            result[cid] = {k: v for k, v in r.items() if not k.startswith("_") and k != "result" and k != "table"}
    return result


def latest_du_kpis() -> dict[str, dict]:
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "du_kpi")
  |> last()
  |> pivot(rowKey: ["_time","du_id","cu_id"],
           columnKey: ["_field"], valueColumn: "_value")
"""
    rows   = _query(flux)
    result = {}
    for r in rows:
        did = r.get("du_id")
        if did:
            result[did] = {k: v for k, v in r.items() if not k.startswith("_") and k != "result" and k != "table"}
    return result


def latest_cu_kpis() -> dict[str, dict]:
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "cu_kpi")
  |> last()
  |> pivot(rowKey: ["_time","cu_id"],
           columnKey: ["_field"], valueColumn: "_value")
"""
    rows   = _query(flux)
    result = {}
    for r in rows:
        cid = r.get("cu_id")
        if cid:
            result[cid] = {k: v for k, v in r.items() if not k.startswith("_") and k != "result" and k != "table"}
    return result


def latest_core_kpis() -> dict[str, dict]:
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "core_kpi")
  |> last()
  |> pivot(rowKey: ["_time","component","instance_id"],
           columnKey: ["_field"], valueColumn: "_value")
"""
    rows   = _query(flux)
    result = {}
    for r in rows:
        comp = r.get("component")
        if comp:
            result[comp] = {k: v for k, v in r.items() if not k.startswith("_") and k != "result" and k != "table"}
    return result


# ── Pydantic request models ──────────────────────────────────────────────────

class MoveCellRequest(BaseModel):
    cell_id:   str
    to_du_id:  str


class MoveDuRequest(BaseModel):
    du_id:     str
    to_cu_id:  str


class AddCellRequest(BaseModel):
    cell_id:        str
    du_id:          str
    area:           str
    lat:            float
    lon:            float
    generation:     str = "5G"
    band:           str = "n78"
    freq_mhz:       int = 3500
    pci:            int = 0
    vendor:         str = "Nokia"
    hardware_model: str = "AirScale MAA 64T64R"
    antenna_config: str = "64T64R"
    peak_dl_mbps:   int = 3800
    tx_power_w:     int = 1000
    idle_power_w:   int = 250
    max_ues:        int = 900


# ── API routes ───────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        get_influx().ping()
        influx_ok = True
    except Exception:
        influx_ok = False
    return {"status": "ok", "influxdb": influx_ok, "topology_exists": TOPOLOGY_FILE.exists()}


@app.get("/topology")
def get_topology():
    return read_topology()


@app.get("/network")
def get_network():
    """Full network state: topology + latest KPIs from InfluxDB in one response."""
    topo       = read_topology()
    cell_kpis  = latest_cell_kpis()
    du_kpis    = latest_du_kpis()
    cu_kpis    = latest_cu_kpis()
    core_kpis  = latest_core_kpis()

    cells = {}
    for cell_id, cfg in topo["cells"].items():
        # resolve current DU/CU from topology
        du_id = next((d for d, v in topo["dus"].items() if cell_id in v["cell_ids"]), None)
        cu_id = topo["dus"][du_id]["cu_id"] if du_id else None
        cells[cell_id] = {**cfg, "du_id": du_id, "cu_id": cu_id, "kpi": cell_kpis.get(cell_id, {})}

    dus = {}
    for du_id, cfg in topo["dus"].items():
        dus[du_id] = {**cfg, "kpi": du_kpis.get(du_id, {})}

    cus = {}
    for cu_id, cfg in topo["cus"].items():
        cus[cu_id] = {**cfg, "kpi": cu_kpis.get(cu_id, {})}

    return {"cells": cells, "dus": dus, "cus": cus, "core": core_kpis,
            "topology_version": topo["version"], "last_updated": topo["last_updated"]}


@app.get("/cells")
def get_cells(area: Optional[str] = None, du_id: Optional[str] = None, cu_id: Optional[str] = None):
    """List cells with latest KPIs. Optional filters: area, du_id, cu_id."""
    topo      = read_topology()
    cell_kpis = latest_cell_kpis()
    result    = []

    for cell_id, cfg in topo["cells"].items():
        curr_du = next((d for d, v in topo["dus"].items() if cell_id in v["cell_ids"]), None)
        curr_cu = topo["dus"][curr_du]["cu_id"] if curr_du else None

        if area   and cfg["area"]  != area:   continue
        if du_id  and curr_du      != du_id:  continue
        if cu_id  and curr_cu      != cu_id:  continue

        result.append({**cfg, "cell_id": cell_id, "du_id": curr_du, "cu_id": curr_cu,
                       "kpi": cell_kpis.get(cell_id, {})})

    return result


@app.get("/cells/{cell_id}")
def get_cell(cell_id: str):
    topo = read_topology()
    if cell_id not in topo["cells"]:
        raise HTTPException(404, f"Cell {cell_id} not found")
    cfg    = topo["cells"][cell_id]
    du_id  = next((d for d, v in topo["dus"].items() if cell_id in v["cell_ids"]), None)
    cu_id  = topo["dus"][du_id]["cu_id"] if du_id else None

    # Last 30 minutes of KPI series
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -30m)
  |> filter(fn: (r) => r._measurement == "cell_kpi" and r.cell_id == "{cell_id}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
    series = []
    for r in _query(flux):
        series.append({k: v for k, v in r.items() if not k.startswith("_") or k == "_time"})

    return {**cfg, "cell_id": cell_id, "du_id": du_id, "cu_id": cu_id, "series": series}


@app.get("/dus")
def get_dus():
    topo    = read_topology()
    du_kpis = latest_du_kpis()
    return [
        {**cfg, "du_id": du_id, "kpi": du_kpis.get(du_id, {})}
        for du_id, cfg in topo["dus"].items()
    ]


@app.get("/cus")
def get_cus():
    topo    = read_topology()
    cu_kpis = latest_cu_kpis()
    return [
        {**cfg, "cu_id": cu_id, "kpi": cu_kpis.get(cu_id, {})}
        for cu_id, cfg in topo["cus"].items()
    ]


@app.post("/move/cell")
def move_cell(req: MoveCellRequest):
    """Move a cell (by cell_id) to a different DU. DU simulators reconfigure within TOPO_POLL_SEC."""
    topo = read_topology()

    if req.cell_id not in topo["cells"]:
        raise HTTPException(404, f"Cell {req.cell_id} not found")
    if req.to_du_id not in topo["dus"]:
        raise HTTPException(404, f"DU {req.to_du_id} not found")

    # Find current DU
    from_du_id = next((d for d, v in topo["dus"].items() if req.cell_id in v["cell_ids"]), None)
    if from_du_id == req.to_du_id:
        return {"status": "no-op", "message": f"Cell {req.cell_id} already on {req.to_du_id}"}

    # Mutate topology
    if from_du_id:
        topo["dus"][from_du_id]["cell_ids"].remove(req.cell_id)
    topo["dus"][req.to_du_id]["cell_ids"].append(req.cell_id)

    # Keep CU's du_ids consistent if the target DU belongs to a different CU
    from_cu = topo["dus"][from_du_id]["cu_id"] if from_du_id else None
    to_cu   = topo["dus"][req.to_du_id]["cu_id"]

    write_topology(topo, updated_by=f"move_cell:{req.cell_id}")
    _record_event("cell_move", {"cell_id": req.cell_id, "from_du": from_du_id or "none",
                                "to_du": req.to_du_id, "from_cu": from_cu or "none", "to_cu": to_cu})

    return {
        "status": "ok",
        "cell_id":   req.cell_id,
        "from_du":   from_du_id,
        "to_du":     req.to_du_id,
        "cu_change": from_cu != to_cu,
        "new_cu":    to_cu,
        "topology_version": topo["version"],
    }


@app.post("/move/du")
def move_du(req: MoveDuRequest):
    """Reassign a DU to a different CU. Both CU simulators reconfigure within TOPO_POLL_SEC."""
    topo = read_topology()

    if req.du_id not in topo["dus"]:
        raise HTTPException(404, f"DU {req.du_id} not found")
    if req.to_cu_id not in topo["cus"]:
        raise HTTPException(404, f"CU {req.to_cu_id} not found")

    from_cu_id = topo["dus"][req.du_id]["cu_id"]
    if from_cu_id == req.to_cu_id:
        return {"status": "no-op", "message": f"DU {req.du_id} already under {req.to_cu_id}"}

    # Mutate topology
    topo["cus"][from_cu_id]["du_ids"].remove(req.du_id)
    topo["cus"][req.to_cu_id]["du_ids"].append(req.du_id)
    topo["dus"][req.du_id]["cu_id"] = req.to_cu_id

    write_topology(topo, updated_by=f"move_du:{req.du_id}")
    _record_event("du_move", {"du_id": req.du_id, "from_cu": from_cu_id, "to_cu": req.to_cu_id})

    return {
        "status": "ok",
        "du_id":   req.du_id,
        "from_cu": from_cu_id,
        "to_cu":   req.to_cu_id,
        "topology_version": topo["version"],
    }


@app.post("/topology/replace")
def replace_topology(new_topo: dict):
    """
    Replace the entire topology with the provided JSON.
    Called by the Planning API's /plan/apply endpoint.
    Validates presence of required top-level keys before writing.
    """
    required = {"cus", "dus", "cells"}
    missing  = required - set(new_topo.keys())
    if missing:
        raise HTTPException(422, f"Topology missing required keys: {missing}")

    # Preserve meta block from the current topology if not provided
    try:
        current = read_topology()
        if "meta" not in new_topo:
            new_topo["meta"] = current.get("meta", {})
    except Exception:
        pass

    write_topology(new_topo, updated_by="planning-api:apply")
    _record_event("topology_replace", {
        "n_cells": len(new_topo.get("cells", {})),
        "n_dus":   len(new_topo.get("dus", {})),
        "n_cus":   len(new_topo.get("cus", {})),
    })
    return {
        "status":           "ok",
        "cells_deployed":   len(new_topo.get("cells", {})),
        "dus_deployed":     len(new_topo.get("dus", {})),
        "cus_deployed":     len(new_topo.get("cus", {})),
        "topology_version": new_topo.get("version", 1),
    }


@app.get("/neighbors/{cell_id}")
def get_neighbors(cell_id: str, max_neighbors: int = 6):
    """
    Return the N geographically closest cells to cell_id (excludes self).
    Uses Haversine distance. Used by SON for ANR optimization.
    """
    import math

    topo = read_topology()
    if cell_id not in topo["cells"]:
        raise HTTPException(404, f"Cell {cell_id} not found")

    def haversine(lat1, lon1, lat2, lon2) -> float:
        R    = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a    = (math.sin(dlat / 2) ** 2
                + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
                * math.sin(dlon / 2) ** 2)
        return R * 2 * math.asin(math.sqrt(a))

    src  = topo["cells"][cell_id]
    dists = []
    for cid, cfg in topo["cells"].items():
        if cid == cell_id:
            continue
        d = haversine(src["lat"], src["lon"], cfg["lat"], cfg["lon"])
        dists.append({"cell_id": cid, "distance_km": round(d, 3), **cfg})

    dists.sort(key=lambda x: x["distance_km"])
    return {"cell_id": cell_id, "neighbors": dists[:max_neighbors]}


@app.post("/cells/add")
def add_cell(req: AddCellRequest):
    """
    Add a new cell to the topology and assign it to the specified DU.
    Auto-assigns a PCI if pci=0 (picks the next available PCI not in use).
    """
    topo = read_topology()

    if req.cell_id in topo["cells"]:
        raise HTTPException(409, f"Cell {req.cell_id} already exists")
    if req.du_id not in topo["dus"]:
        raise HTTPException(404, f"DU {req.du_id} not found")

    # Auto-assign PCI if not provided
    pci = req.pci
    if pci == 0:
        used_pcis = {c["pci"] for c in topo["cells"].values()}
        pci = next(p for p in range(1, 1024) if p not in used_pcis)

    topo["cells"][req.cell_id] = {
        "area":           req.area,
        "lat":            req.lat,
        "lon":            req.lon,
        "generation":     req.generation,
        "band":           req.band,
        "freq_mhz":       req.freq_mhz,
        "pci":            pci,
        "vendor":         req.vendor,
        "hardware_model": req.hardware_model,
        "antenna_config": req.antenna_config,
        "peak_dl_mbps":   req.peak_dl_mbps,
        "tx_power_w":     req.tx_power_w,
        "idle_power_w":   req.idle_power_w,
        "max_ues":        req.max_ues,
    }
    topo["dus"][req.du_id]["cell_ids"].append(req.cell_id)
    write_topology(topo, updated_by=f"add_cell:{req.cell_id}")
    _record_event("cell_add", {"cell_id": req.cell_id, "du_id": req.du_id,
                               "band": req.band, "pci": pci})
    return {
        "status":  "ok",
        "cell_id": req.cell_id,
        "pci":     pci,
        "du_id":   req.du_id,
        "topology_version": topo["version"],
    }


@app.delete("/cells/{cell_id}")
def remove_cell(cell_id: str):
    """Remove a cell from topology. The cell's DU reconfigures within TOPO_POLL_SEC."""
    topo = read_topology()
    if cell_id not in topo["cells"]:
        raise HTTPException(404, f"Cell {cell_id} not found")

    du_id = next((d for d, v in topo["dus"].items() if cell_id in v["cell_ids"]), None)
    if du_id:
        topo["dus"][du_id]["cell_ids"].remove(cell_id)
    del topo["cells"][cell_id]

    write_topology(topo, updated_by=f"remove_cell:{cell_id}")
    _record_event("cell_remove", {"cell_id": cell_id, "du_id": du_id or "none"})
    return {"status": "ok", "cell_id": cell_id, "removed_from_du": du_id,
            "topology_version": topo["version"]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("controller:app", host="0.0.0.0", port=8080, reload=False)
