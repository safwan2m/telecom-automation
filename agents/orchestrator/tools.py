"""
Tool definitions for the LLM Orchestrator.
Each tool is a typed Python function + a JSON schema the Claude API can call.
"""

import httpx
import logging
from typing import Any

log = logging.getLogger(__name__)

CONTROLLER_URL = None   # set at startup
PLANNING_URL   = None
INFLUX_URL     = None
INFLUX_TOKEN   = None
INFLUX_ORG     = None
INFLUX_BUCKET  = None


def _ctrl(path: str, method="GET", body: dict | None = None) -> Any:
    url = f"{CONTROLLER_URL}{path}"
    try:
        if method == "GET":
            r = httpx.get(url, timeout=8.0)
        elif method == "DELETE":
            r = httpx.delete(url, timeout=8.0)
        else:
            r = httpx.post(url, json=body, timeout=8.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _plan(path: str, method="GET", body: dict | None = None) -> Any:
    url = f"{PLANNING_URL}{path}"
    try:
        if method == "GET":
            r = httpx.get(url, timeout=30.0)
        else:
            r = httpx.post(url, json=body, timeout=30.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _influx_query(flux: str) -> list[dict]:
    from influxdb_client import InfluxDBClient
    try:
        client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        tables = client.query_api().query(flux, org=INFLUX_ORG)
        rows   = []
        for table in tables:
            for rec in table.records:
                rows.append({k: v for k, v in rec.values.items() if not k.startswith("_") or k == "_time"})
        return rows
    except Exception as e:
        return [{"error": str(e)}]


# ── Tool implementations ─────────────────────────────────────────────────────

def optimize_congestion(top_n: int = 10) -> dict:
    """Fetch per-cell congestion scores from the controller and return ranked results."""
    data = _ctrl("/congestion")
    if "error" in data:
        return data
    cells = data.get("cells", [])[:top_n]
    summary = data.get("summary", {})
    # Attach neighbour headroom hint for the top critical cells
    hints = []
    for cell in cells[:5]:
        if cell["level"] in ("CRITICAL", "HIGH"):
            nbrs = _ctrl(f"/neighbors/{cell['cell_id']}?max_neighbors=3")
            cell["neighbors"] = nbrs.get("neighbors", [])
        hints.append(cell)
    return {
        "summary": summary,
        "top_congested_cells": hints + cells[5:],
        "guidance": (
            "For CRITICAL cells: call move_cell to shift to a lighter DU, or rely on "
            "neighbor_load_steer SON actions already written by the KPI agent. "
            "For HIGH cells: monitor — the KPI agent will pre-emptively steer if score "
            "exceeds 0.65. Use get_son_status to see recent autonomous actions taken."
        ),
    }


def query_network() -> dict:
    """Return the complete current network state: all cells, DUs, CUs with live KPIs."""
    return _ctrl("/network")


def query_cell(cell_id: str) -> dict:
    """Return details and 30-minute KPI time series for a specific cell."""
    return _ctrl(f"/cells/{cell_id}")


def list_cells(area: str = "", du_id: str = "", cu_id: str = "") -> list:
    """List all cells, optionally filtered by area, du_id, or cu_id."""
    params = "&".join(f"{k}={v}" for k, v in [("area", area), ("du_id", du_id), ("cu_id", cu_id)] if v)
    return _ctrl(f"/cells{'?' + params if params else ''}")


def move_cell(cell_id: str, to_du_id: str) -> dict:
    """Move a cell to a different DU. DU simulators reconfigure within ~5 seconds."""
    return _ctrl("/move/cell", "POST", {"cell_id": cell_id, "to_du_id": to_du_id})


def move_du(du_id: str, to_cu_id: str) -> dict:
    """Reassign a DU to a different CU. Both CU simulators reconfigure within ~5 seconds."""
    return _ctrl("/move/du", "POST", {"du_id": du_id, "to_cu_id": to_cu_id})


def plan_network(
    geographic_area: str = "Bangalore",
    expected_user_density: float = 500.0,
    embb_fraction: float = 0.7,
    urllc_fraction: float = 0.2,
    mmtc_fraction: float = 0.1,
    spectrum_bands: list[str] | None = None,
    deployment_budget: float = 2_000_000.0,
    e2e_latency_ms: float = 10.0,
    use_mip: bool = False,
    sinr_min_db: float = 10.0,
) -> dict:
    """
    Run the planning engine to generate a new network plan.
    Returns cell placement, PCI assignments, DU/CU grouping, and slice allocation.
    use_mip=True selects cells using MIP optimisation (Almoghathawi et al. 2024)
    instead of the default heuristic.  sinr_min_db sets the SINR quality constraint.
    """
    body = {
        "geographic_area":       geographic_area,
        "expected_user_density": expected_user_density,
        "traffic_profile":       {"eMBB": embb_fraction, "URLLC": urllc_fraction, "mMTC": mmtc_fraction},
        "spectrum_bands":        spectrum_bands or ["n78", "n28"],
        "deployment_budget":     deployment_budget,
        "latency_constraints":   {"e2e_ms": e2e_latency_ms, "fronthaul_us": 100.0},
        "use_mip":               use_mip,
        "sinr_min_db":           sinr_min_db,
    }
    return _plan("/plan", "POST", body)


def plan_network_multi_period(
    demand_mode: str = "permanent",
    spectrum_bands: list[str] | None = None,
    deployment_budget: float = 2_000_000.0,
    sinr_min_db: float = 10.0,
) -> dict:
    """
    Run multi-period MIP network planning.
    demand_mode='permanent': phased rollout (Case A — areas added each period).
    demand_mode='temporary': diurnal/event demand shift (Case B — demand clusters shift).
    Returns optimal build schedule across periods plus full network plan.
    """
    body = {
        "demand_mode":      demand_mode,
        "spectrum_bands":   spectrum_bands or ["n78", "n28"],
        "deployment_budget": deployment_budget,
        "sinr_min_db":      sinr_min_db,
    }
    return _plan("/plan/multi-period", "POST", body)


def apply_plan(plan_id: str) -> dict:
    """Apply a previously generated network plan to the live deployment via the Controller."""
    return _plan("/plan/apply", "POST", {"plan_id": plan_id})


def query_ue(ue_id: str = "", cell_id: str = "", last_minutes: int = 30) -> list:
    """
    Query per-UE usage and mobility events from InfluxDB.
    Filter by ue_id or cell_id; returns both ue_usage and ue_mobility records.
    """
    ue_filter   = f'|> filter(fn: (r) => r.ue_id == "{ue_id}")' if ue_id else ""
    cell_filter = f'|> filter(fn: (r) => r.cell_id == "{cell_id}")' if cell_id else ""
    flux_usage  = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{last_minutes}m)
  |> filter(fn: (r) => r._measurement == "ue_usage")
  {ue_filter}{cell_filter}
  |> filter(fn: (r) => r._field == "dl_bytes" or r._field == "ul_bytes"
                    or r._field == "latency_ms" or r._field == "jitter_ms"
                    or r._field == "packet_loss")
  |> last()
  |> limit(n: 50)
"""
    cell_mob_filter = (
        f'|> filter(fn: (r) => r.source_cell == "{cell_id}" or r.target_cell == "{cell_id}")'
        if cell_id else ""
    )
    flux_mob = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{last_minutes}m)
  |> filter(fn: (r) => r._measurement == "ue_mobility")
  {ue_filter}{cell_mob_filter}
  |> filter(fn: (r) => r._field == "ho_duration_ms" or r._field == "rsrp_source"
                    or r._field == "rsrp_target" or r._field == "velocity_kmh")
  |> last()
  |> limit(n: 20)
"""
    usage    = _influx_query(flux_usage)
    mobility = _influx_query(flux_mob)
    return {"usage_records": usage, "mobility_events": mobility}


def get_son_status(last_minutes: int = 60) -> dict:
    """
    Return a summary of recent SON (Self-Organizing Network) actions.
    Includes action counts by type, latest 10 actions, and alert severity breakdown.
    """
    flux_actions = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{last_minutes}m)
  |> filter(fn: (r) => r._measurement == "son_actions")
  |> filter(fn: (r) => r._field == "message")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 20)
"""
    flux_alerts = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{last_minutes}m)
  |> filter(fn: (r) => r._measurement == "alerts")
  |> filter(fn: (r) => r._field == "message")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 50)
"""
    actions = _influx_query(flux_actions)
    alerts  = _influx_query(flux_alerts)

    action_types: dict[str, int] = {}
    for a in actions:
        t = a.get("action_type", "UNKNOWN")
        action_types[t] = action_types.get(t, 0) + 1

    alert_sev: dict[str, int] = {}
    for a in alerts:
        s = a.get("severity", "UNKNOWN")
        alert_sev[s] = alert_sev.get(s, 0) + 1

    return {
        "window_minutes":    last_minutes,
        "total_son_actions": len(actions),
        "action_type_counts": action_types,
        "total_alerts":      len(alerts),
        "alert_severity_counts": alert_sev,
        "recent_actions":    actions[:10],
    }


def add_cell(
    cell_id: str,
    du_id: str,
    area: str,
    lat: float,
    lon: float,
    generation: str = "5G",
    band: str = "n78",
    vendor: str = "Nokia",
    max_ues: int = 900,
    tx_power_w: int = 1000,
) -> dict:
    """
    Add a new cell to the live network topology.
    PCI is auto-assigned if not specified.
    The DU simulator picks up the new cell within TOPO_POLL_SEC (default 5 s).
    """
    hw_5g = {
        "Nokia":    {"hardware_model": "AirScale MAA 64T64R", "antenna_config": "64T64R", "peak_dl_mbps": 3800, "idle_power_w": 250, "freq_mhz": 3500},
        "Ericsson": {"hardware_model": "AIR 6449",            "antenna_config": "64T64R", "peak_dl_mbps": 3600, "idle_power_w": 240, "freq_mhz": 3500},
        "Samsung":  {"hardware_model": "TM500 64T64R",        "antenna_config": "64T64R", "peak_dl_mbps": 3400, "idle_power_w": 225, "freq_mhz": 3500},
        "ZTE":      {"hardware_model": "AAU 5614",            "antenna_config": "64T64R", "peak_dl_mbps": 3200, "idle_power_w": 250, "freq_mhz": 3500},
    }
    hw_4g = {
        "Nokia":    {"hardware_model": "Flexi Multiradio 10 AWHFA", "antenna_config": "4T4R", "peak_dl_mbps": 150, "idle_power_w": 80, "freq_mhz": 1800},
        "Ericsson": {"hardware_model": "Radio 4449",                 "antenna_config": "4T4R", "peak_dl_mbps": 150, "idle_power_w": 70, "freq_mhz": 1800},
        "Samsung":  {"hardware_model": "NR RU 4T4R",                 "antenna_config": "4T4R", "peak_dl_mbps": 150, "idle_power_w": 65, "freq_mhz": 1800},
        "ZTE":      {"hardware_model": "AARU 4T4R",                  "antenna_config": "4T4R", "peak_dl_mbps": 150, "idle_power_w": 75, "freq_mhz": 1800},
    }
    hw_defaults = hw_4g if generation == "4G" else hw_5g
    hw = hw_defaults.get(vendor, hw_defaults["Nokia"])
    body = {
        "cell_id": cell_id, "du_id": du_id, "area": area,
        "lat": lat, "lon": lon,
        "generation": generation, "band": band,
        "freq_mhz": hw["freq_mhz"], "pci": 0,
        "vendor": vendor, "hardware_model": hw["hardware_model"],
        "antenna_config": hw["antenna_config"],
        "peak_dl_mbps": hw["peak_dl_mbps"],
        "tx_power_w": tx_power_w, "idle_power_w": hw["idle_power_w"],
        "max_ues": max_ues,
    }
    return _ctrl("/cells/add", "POST", body)


def remove_cell(cell_id: str) -> dict:
    """Remove a cell from the live network topology."""
    return _ctrl(f"/cells/{cell_id}", "DELETE")


def get_alerts(severity: str = "", last_minutes: int = 60) -> list:
    """
    Retrieve recent KPI alerts from InfluxDB.
    severity: CRITICAL | WARNING | INFO  (empty = all)
    """
    sev_filter = f'|> filter(fn: (r) => r.severity == "{severity}")' if severity else ""
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{last_minutes}m)
  |> filter(fn: (r) => r._measurement == "alerts")
  {sev_filter}
  |> filter(fn: (r) => r._field == "message")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 50)
"""
    return _influx_query(flux)


# ── Tool schema (Claude tool_use format) ─────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "query_network",
        "description": "Get the complete current network state: all cells, DUs, and CUs with their latest KPIs (throughput, SINR, power, PRB utilisation, connected UEs). Use this first to understand what is deployed.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_cells",
        "description": "List cells, optionally filtered by geographic area, DU, or CU. Returns cell config + latest KPIs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "area":   {"type": "string", "description": "Filter by area name e.g. 'Whitefield'"},
                "du_id":  {"type": "string", "description": "Filter by DU e.g. 'DU-NORTH-1'"},
                "cu_id":  {"type": "string", "description": "Filter by CU e.g. 'CU-NORTH'"},
            },
            "required": [],
        },
    },
    {
        "name": "query_cell",
        "description": "Get detailed config and 30-minute KPI time series for a specific cell.",
        "input_schema": {
            "type": "object",
            "properties": {"cell_id": {"type": "string", "description": "e.g. 'BLR_KRM_01'"}},
            "required": ["cell_id"],
        },
    },
    {
        "name": "move_cell",
        "description": "Move a cell from its current DU to a different DU. The DU simulators reconfigure within ~5 seconds and the change is reflected in InfluxDB on the next push cycle.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cell_id":   {"type": "string", "description": "Cell to move e.g. 'BLR_KRM_01'"},
                "to_du_id":  {"type": "string", "description": "Destination DU e.g. 'DU-NORTH-2'"},
            },
            "required": ["cell_id", "to_du_id"],
        },
    },
    {
        "name": "move_du",
        "description": "Reassign a DU to a different CU (changes the midhaul parent). Both CU simulators reconfigure within ~5 seconds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "du_id":    {"type": "string", "description": "DU to reassign e.g. 'DU-NORTH-1'"},
                "to_cu_id": {"type": "string", "description": "Destination CU e.g. 'CU-SOUTH'"},
            },
            "required": ["du_id", "to_cu_id"],
        },
    },
    {
        "name": "plan_network",
        "description": "Run the planning engine to generate a new network plan. Returns cell placement, PCI assignments, DU/CU grouping, slice allocation, cost estimate, and timing sync strategy. The operator must call apply_plan separately to deploy it. Set use_mip=true for MIP-optimal placement with SINR quality constraints.",
        "input_schema": {
            "type": "object",
            "properties": {
                "geographic_area":        {"type": "string",  "default": "Bangalore"},
                "expected_user_density":  {"type": "number",  "description": "Users per km²", "default": 500},
                "embb_fraction":          {"type": "number",  "description": "eMBB traffic fraction 0-1", "default": 0.7},
                "urllc_fraction":         {"type": "number",  "description": "URLLC traffic fraction 0-1", "default": 0.2},
                "mmtc_fraction":          {"type": "number",  "description": "mMTC traffic fraction 0-1", "default": 0.1},
                "spectrum_bands":         {"type": "array", "items": {"type": "string"}, "description": "e.g. ['n78','n28']"},
                "deployment_budget":      {"type": "number",  "description": "USD", "default": 2000000},
                "e2e_latency_ms":         {"type": "number",  "description": "E2E latency target ms", "default": 10},
                "use_mip":                {"type": "boolean", "description": "Use MIP-optimal placement (slower but cost-optimal with SINR constraints)", "default": False},
                "sinr_min_db":            {"type": "number",  "description": "Minimum SINR constraint for MIP placement (dB)", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "plan_network_multi_period",
        "description": "Run multi-period MIP network planning. permanent mode (Case A) optimises a phased rollout — BSs built in early periods serve later demand. temporary mode (Case B) optimises for shifting demand (events, diurnal peaks). Returns optimal build schedule across periods plus a deployable network plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "demand_mode":       {"type": "string", "enum": ["permanent", "temporary"], "default": "permanent",
                                      "description": "permanent=phased rollout, temporary=diurnal/event shift"},
                "spectrum_bands":    {"type": "array", "items": {"type": "string"}, "description": "e.g. ['n78','n28']"},
                "deployment_budget": {"type": "number", "description": "USD", "default": 2000000},
                "sinr_min_db":       {"type": "number", "description": "Minimum SINR constraint (dB)", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "apply_plan",
        "description": "Apply a previously generated network plan to the live deployment. This replaces the current topology with the plan's topology.",
        "input_schema": {
            "type": "object",
            "properties": {"plan_id": {"type": "string", "description": "plan_id from plan_network response"}},
            "required": ["plan_id"],
        },
    },
    {
        "name": "get_alerts",
        "description": "Retrieve recent KPI alerts detected by the KPI agent (overloads, SINR degradation, power waste, underload).",
        "input_schema": {
            "type": "object",
            "properties": {
                "severity":      {"type": "string", "enum": ["CRITICAL", "WARNING", "INFO", ""], "default": ""},
                "last_minutes":  {"type": "integer", "default": 60},
            },
            "required": [],
        },
    },
    {
        "name": "query_ue",
        "description": "Query per-UE usage (DL/UL bytes, latency, jitter, packet loss) and mobility events (handovers, velocity, RSRP) from InfluxDB. Filter by ue_id or cell_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ue_id":        {"type": "string", "description": "Specific UE ID e.g. 'UE-MLS_RWS_01-0023'"},
                "cell_id":      {"type": "string", "description": "Filter all UEs on a cell e.g. 'MLS_RWS_01'"},
                "last_minutes": {"type": "integer", "default": 30},
            },
            "required": [],
        },
    },
    {
        "name": "get_son_status",
        "description": "Return a summary of recent SON (Self-Organizing Network) autonomous actions: load balancing moves, PCI re-optimisation requests, DTX recommendations, traffic steering. Shows action counts and the latest 10 actions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "last_minutes": {"type": "integer", "default": 60},
            },
            "required": [],
        },
    },
    {
        "name": "add_cell",
        "description": "Add a new cell to the live network topology. The DU simulator picks it up within 5 seconds. PCI is auto-assigned. Use this for conversational cell deployment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cell_id":    {"type": "string", "description": "Unique cell ID e.g. 'MLS_NEW_01'"},
                "du_id":      {"type": "string", "description": "Target DU e.g. 'DU-MLS-1'"},
                "area":       {"type": "string", "description": "Area name e.g. 'Malleswaram'"},
                "lat":        {"type": "number", "description": "Latitude"},
                "lon":        {"type": "number", "description": "Longitude"},
                "generation": {"type": "string", "enum": ["5G", "4G"], "default": "5G"},
                "band":       {"type": "string", "description": "Radio band e.g. 'n78'", "default": "n78"},
                "vendor":     {"type": "string", "enum": ["Nokia", "Ericsson", "Samsung", "ZTE"], "default": "Nokia"},
                "max_ues":    {"type": "integer", "default": 900},
                "tx_power_w": {"type": "integer", "default": 1000},
            },
            "required": ["cell_id", "du_id", "area", "lat", "lon"],
        },
    },
    {
        "name": "remove_cell",
        "description": "Remove a cell from the live network topology. The DU simulator deregisters it within 5 seconds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cell_id": {"type": "string", "description": "Cell to remove e.g. 'MLS_RWS_01'"},
            },
            "required": ["cell_id"],
        },
    },
    {
        "name": "optimize_congestion",
        "description": (
            "Get a live congestion report for all 30 cells ranked by a multi-factor score "
            "(PRB 40%, SINR-inverse 20%, BLER 20%, latency 20%). Returns severity levels "
            "(CRITICAL/HIGH/MODERATE/LOW), per-cell scores, and neighbor headroom hints for "
            "the top 5 cells. Use this before calling move_cell to understand which cells "
            "need intervention and which neighbors have spare capacity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "top_n": {
                    "type": "integer",
                    "description": "Number of top congested cells to return (default 10)",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
]

# Map tool name → function
TOOL_MAP = {
    "query_network":             lambda args: query_network(),
    "list_cells":                lambda args: list_cells(**args),
    "query_cell":                lambda args: query_cell(**args),
    "move_cell":                 lambda args: move_cell(**args),
    "move_du":                   lambda args: move_du(**args),
    "plan_network":              lambda args: plan_network(**args),
    "plan_network_multi_period": lambda args: plan_network_multi_period(**args),
    "apply_plan":                lambda args: apply_plan(**args),
    "get_alerts":                lambda args: get_alerts(**args),
    "query_ue":                  lambda args: query_ue(**args),
    "get_son_status":            lambda args: get_son_status(**args),
    "add_cell":                  lambda args: add_cell(**args),
    "remove_cell":               lambda args: remove_cell(**args),
    "optimize_congestion":       lambda args: optimize_congestion(**args),
}
