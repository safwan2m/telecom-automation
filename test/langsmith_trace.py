"""
Standalone LangSmith tracing demo — 3 tool calls in a single traced run.

Loads credentials from dev-env/.env automatically.
Verifies LangSmith connectivity and Controller reachability before running.

Usage:
    py test/langsmith_trace.py
"""

import os
import sys
import json
import time
import httpx

# Load .env before any langsmith import so SDK picks up the vars
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "dev-env", ".env"))
except ImportError:
    print("python-dotenv not installed; relying on shell environment variables.")

CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://localhost:8080")
INFLUX_URL     = os.environ.get("INFLUX_URL",     "http://localhost:8086")
INFLUX_TOKEN   = os.environ.get("INFLUX_TOKEN",   "telecom-super-secret-auth-token-2026")
INFLUX_ORG     = os.environ.get("INFLUX_ORG",     "telecom")
INFLUX_BUCKET  = os.environ.get("INFLUX_BUCKET",  "telecom_metrics")

try:
    from langsmith.run_trees import RunTree
except ImportError:
    print("ERROR: langsmith not installed. Run: pip install langsmith")
    sys.exit(1)


# ── Preflight checks ──────────────────────────────────────────────────────────

def preflight():
    api_key  = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY", "")
    tracing  = (
        os.environ.get("LANGSMITH_TRACING",    "").lower() == "true"
        or os.environ.get("LANGCHAIN_TRACING_V2", "").lower() == "true"
    )
    project  = os.environ.get("LANGSMITH_PROJECT") or os.environ.get("LANGCHAIN_PROJECT", "telecom_tracing")
    endpoint = os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")

    print("=== LangSmith preflight ===")
    print(f"  API key  : {'set (' + api_key[:12] + '...)' if api_key else 'MISSING'}")
    print(f"  Tracing  : {tracing}")
    print(f"  Project  : {project}")
    print(f"  Endpoint : {endpoint}")

    if not api_key or not tracing:
        print("\nERROR: Set LANGCHAIN_API_KEY + LANGCHAIN_TRACING_V2=true in dev-env/.env")
        sys.exit(1)

    print("\n=== Controller preflight ===")
    try:
        r = httpx.get(f"{CONTROLLER_URL}/health", timeout=5.0)
        print(f"  Controller {CONTROLLER_URL} -> {r.status_code}")
    except Exception as e:
        print(f"  Controller unreachable: {e}")
        print("  Run: cd dev-env && docker compose up -d controller")
        sys.exit(1)

    return project


# ── Tool implementations ──────────────────────────────────────────────────────

def tool_query_network(parent: RunTree) -> dict:
    span = parent.create_child(
        name="query_network",
        run_type="tool",
        inputs={"description": "GET /network from Controller"},
    )
    span.post()
    try:
        r      = httpx.get(f"{CONTROLLER_URL}/network", timeout=8.0)
        result = r.json()
        cells  = result.get("cells", {})
        summary = {
            "total_cells": len(cells),
            "total_ues":   sum(c.get("kpi", {}).get("connected_ues", 0) for c in cells.values()),
            "max_prb_cell": max(cells, key=lambda k: cells[k].get("kpi", {}).get("prb_dl_pct", 0), default="?"),
        }
        span.end(outputs=summary)
        span.patch()
        return summary
    except Exception as e:
        span.end(error=str(e))
        span.patch()
        return {"error": str(e)}


def tool_get_alerts(parent: RunTree, last_minutes: int = 30) -> dict:
    span = parent.create_child(
        name="get_alerts",
        run_type="tool",
        inputs={"last_minutes": last_minutes},
    )
    span.post()
    try:
        from influxdb_client import InfluxDBClient
        client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        flux   = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{last_minutes}m)
  |> filter(fn: (r) => r._measurement == "alerts")
  |> filter(fn: (r) => r._field == "message")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 10)
"""
        tables  = client.query_api().query(flux, org=INFLUX_ORG)
        alerts  = [
            {k: v for k, v in rec.values.items() if not k.startswith("_") or k == "_time"}
            for table in tables for rec in table.records
        ]
        result  = {"count": len(alerts), "alerts": alerts[:5]}
        span.end(outputs=result)
        span.patch()
        return result
    except Exception as e:
        span.end(error=str(e))
        span.patch()
        return {"error": str(e)}


def tool_query_cell(parent: RunTree, cell_id: str) -> dict:
    span = parent.create_child(
        name="query_cell",
        run_type="tool",
        inputs={"cell_id": cell_id},
    )
    span.post()
    try:
        r      = httpx.get(f"{CONTROLLER_URL}/cells/{cell_id}", timeout=8.0)
        result = r.json()
        kpi    = result.get("kpi", {})
        summary = {
            "cell_id":    cell_id,
            "du_id":      result.get("du_id"),
            "connected_ues": kpi.get("connected_ues"),
            "prb_dl_pct":    kpi.get("prb_dl_pct"),
            "sinr_db":       kpi.get("sinr_db"),
            "power_w":       kpi.get("power_w"),
        }
        span.end(outputs=summary)
        span.patch()
        return summary
    except Exception as e:
        span.end(error=str(e))
        span.patch()
        return {"error": str(e)}


# ── Main demo ─────────────────────────────────────────────────────────────────

def main():
    project = preflight()
    print("\n=== Running 3-tool trace demo ===")

    root = RunTree(
        name="telecom-query-demo",
        run_type="chain",
        inputs={"query": "Which cell is serving the highest number of UEs right now?"},
        project_name=project,
    )
    root.post()

    print("\n[1/3] tool_query_network ...")
    net = tool_query_network(root)
    print(f"      total_cells={net.get('total_cells')}  total_ues={net.get('total_ues')}  "
          f"max_prb_cell={net.get('max_prb_cell')}")

    print("\n[2/3] tool_get_alerts (last 30 min) ...")
    alerts = tool_get_alerts(root, last_minutes=30)
    print(f"      alert_count={alerts.get('count')}")

    target_cell = net.get("max_prb_cell", "MLS_MGR_01")
    print(f"\n[3/3] tool_query_cell({target_cell}) ...")
    cell = tool_query_cell(root, cell_id=target_cell)
    print(f"      UEs={cell.get('connected_ues')}  PRB={cell.get('prb_dl_pct')}%  "
          f"SINR={cell.get('sinr_db')} dB")

    root.end(outputs={
        "answer": f"Highest-load cell is {target_cell} with {cell.get('connected_ues')} UEs",
        "tool_calls": 3,
    })
    root.patch()

    print(f"\nTrace uploaded to LangSmith project '{project}'.")
    print("Open https://smith.langchain.com to view the run.")


if __name__ == "__main__":
    main()
