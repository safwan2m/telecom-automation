"""
LangSmith tracing helpers for the Telecom Orchestrator.

Activate via dev-env/.env:
    LANGCHAIN_API_KEY=lsv2_pt_...
    LANGCHAIN_TRACING_V2=true
    LANGCHAIN_PROJECT=telecom_automation1

Each chat turn is a root 'chain' run.  Every tool call inside that turn is a
child 'tool' run.  The parent handle is passed explicitly through the call
stack so traces are correct even inside generator functions — @traceable on
generators does not propagate context across yield boundaries.

When tracing is inactive all helpers are no-ops with zero overhead.
"""

import os
import json
import logging

log = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 20 * 1024 * 1024  # LangSmith rejects fields > 25 MB

try:
    from langsmith.run_trees import RunTree as _RunTree
    _AVAILABLE = True
except ImportError:
    _RunTree = None  # type: ignore[assignment]
    _AVAILABLE = False


def _safe_outputs(outputs: dict) -> dict:
    """Truncate any value whose JSON encoding exceeds LangSmith's per-field limit."""
    try:
        result = {}
        for k, v in outputs.items():
            encoded = json.dumps(v, default=str).encode()
            if len(encoded) > _MAX_OUTPUT_BYTES:
                result[k] = f"[TRUNCATED {len(encoded) // 1024 // 1024} MB — exceeds 20 MB limit]"
            else:
                result[k] = v
        return result
    except Exception:
        return {"result": "[could not serialize outputs]"}


def _active() -> bool:
    api_key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY", "")
    # Check each var independently — "false" from one must not shadow "true" from the other
    tracing_on = (
        os.environ.get("LANGSMITH_TRACING",    "").lower() == "true"
        or os.environ.get("LANGCHAIN_TRACING_V2", "").lower() == "true"
    )
    return _AVAILABLE and bool(api_key) and tracing_on


def setup() -> bool:
    """Log LangSmith state at startup. Returns True when tracing is active."""
    if not _AVAILABLE:
        log.warning("langsmith not installed — pip install langsmith to enable tracing")
        return False
    active   = _active()
    project  = os.environ.get("LANGSMITH_PROJECT")  or os.environ.get("LANGCHAIN_PROJECT",  "telecom_tracing")
    endpoint = os.environ.get("LANGSMITH_ENDPOINT") or os.environ.get("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")
    if active:
        log.info("LangSmith tracing active  project=%s  endpoint=%s", project, endpoint)
    else:
        log.info("LangSmith tracing disabled (set LANGCHAIN_API_KEY + LANGCHAIN_TRACING_V2=true)")
    return active


def start_run(name: str, inputs: dict):
    """
    Open a root chain run in LangSmith.
    Returns a RunTree handle to pass down the call stack, or None when inactive.
    """
    if not _active():
        return None
    project = os.environ.get("LANGSMITH_PROJECT") or os.environ.get("LANGCHAIN_PROJECT", "telecom_tracing")
    try:
        run = _RunTree(name=name, run_type="chain", inputs=inputs, project_name=project)
        run.post()
        return run
    except Exception as e:
        log.warning("LangSmith start_run failed: %s", e)
        return None


def tool_span(parent_run, name: str, inputs: dict):
    """
    Open a child tool span under parent_run.
    Returns a span handle, or None when parent_run is None (tracing inactive).
    """
    if parent_run is None:
        return None
    try:
        span = parent_run.create_child(name=name, run_type="tool", inputs=inputs)
        span.post()
        return span
    except Exception as e:
        log.warning("LangSmith tool_span failed: %s", e)
        return None


def end_run(run, outputs: dict | None = None, error: str | None = None) -> None:
    """Close and upload a run or span. No-op when run is None."""
    if run is None:
        return
    try:
        if error:
            run.end(error=error)
        else:
            run.end(outputs=_safe_outputs(outputs or {}))
        run.patch()
    except Exception as e:
        log.warning("LangSmith end_run failed: %s", e)
