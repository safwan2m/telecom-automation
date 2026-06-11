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
}
