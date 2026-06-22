"""
Tests for agents/planning/planner_api.py — REST API via FastAPI TestClient.

The Controller and InfluxDB are both unreachable (different Docker network),
so _sufficiency_check falls back to 0 live cells → "deploy" mode, which
exercises the full candidate-cell pipeline without any mocking.
"""
import pytest
import httpx
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

import planner_api


@pytest.fixture(autouse=True)
def mock_influx(monkeypatch):
    """Stub out InfluxDB: writes succeed silently; queries raise so fallbacks activate."""
    mock = MagicMock()
    mock.ping.return_value = True
    mock.write_api.return_value.write.return_value = None
    # Raise on query → _read_plan returns None, list_plans falls back to _plans cache
    mock.query_api.return_value.query.side_effect = Exception("mocked InfluxDB unavailable")
    monkeypatch.setattr(planner_api, "_influx_client", mock)
    monkeypatch.setattr(planner_api, "_get_influx", lambda: mock)
    return mock


@pytest.fixture(autouse=True)
def isolated_plan_cache(monkeypatch):
    """Give each test a clean in-memory plan cache."""
    monkeypatch.setattr(planner_api, "_plans", {})


@pytest.fixture
def client():
    return TestClient(planner_api.app)


VALID_REQUEST = {
    "geographic_area":       "Malleswaram",
    "expected_user_density": 2000.0,
    "traffic_profile":       {"eMBB": 0.70, "URLLC": 0.20, "mMTC": 0.10, "peak_hour": 19},
    "spectrum_bands":        ["n78"],
    "deployment_budget":     5_000_000.0,
    "latency_constraints":   {"e2e_ms": 10.0, "fronthaul_us": 100.0},
}


# ── /health ──────────────────────────────────────────────────────────────────

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── POST /plan — validation ───────────────────────────────────────────────────

def test_plan_missing_fields_returns_status(client):
    r = client.post("/plan", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "missing_fields"
    assert "missing" in body
    assert len(body["missing"]) > 0


def test_plan_missing_one_field(client):
    req = {k: v for k, v in VALID_REQUEST.items() if k != "deployment_budget"}
    r = client.post("/plan", json=req)
    body = r.json()
    assert body["status"] == "missing_fields"
    assert "deployment_budget" in body["missing"]


# ── POST /plan — happy path ───────────────────────────────────────────────────

def test_plan_returns_plan_id(client):
    r = client.post("/plan", json=VALID_REQUEST)
    assert r.status_code == 200
    assert "plan_id" in r.json()


def test_plan_has_no_pci_violations(client):
    r = client.post("/plan", json=VALID_REQUEST)
    assert r.status_code == 200
    violations = r.json().get("pci_violations", [])
    assert violations == [], f"Unexpected PCI collisions: {violations}"


def test_plan_contains_cells(client):
    r = client.post("/plan", json=VALID_REQUEST)
    assert r.status_code == 200
    cells = r.json().get("cells", [])
    assert len(cells) > 0


def test_plan_response_has_required_keys(client):
    r = client.post("/plan", json=VALID_REQUEST)
    body = r.json()
    for key in ("plan_id", "plan_type", "cells", "dus", "cus",
                "timing_sync", "pci_violations", "pci_confusions", "summary"):
        assert key in body, f"Missing key: {key}"


def test_plan_summary_has_cost(client):
    r = client.post("/plan", json=VALID_REQUEST)
    summary = r.json()["summary"]
    assert "estimated_cost_usd" in summary
    assert summary["estimated_cost_usd"] >= 0


def test_plan_pci_confusions_is_list(client):
    r = client.post("/plan", json=VALID_REQUEST)
    assert isinstance(r.json()["pci_confusions"], list)


# ── GET /plan/{plan_id} ───────────────────────────────────────────────────────

def test_get_plan_by_id(client):
    plan_id = client.post("/plan", json=VALID_REQUEST).json()["plan_id"]
    r = client.get(f"/plan/{plan_id}")
    assert r.status_code == 200
    assert r.json()["plan_id"] == plan_id


def test_get_plan_unknown_returns_404(client):
    r = client.get("/plan/nonexistent-id")
    assert r.status_code == 404


# ── GET /plans ────────────────────────────────────────────────────────────────

def test_list_plans_after_create(client):
    client.post("/plan", json=VALID_REQUEST)
    client.post("/plan", json=VALID_REQUEST)
    r = client.get("/plans")
    assert r.status_code == 200
    body = r.json()
    # InfluxDB is mocked → falls back to session cache
    assert body["count"] == 2


# ── GET /candidates ───────────────────────────────────────────────────────────

def test_candidates_returns_list(client):
    r = client.get("/candidates")
    assert r.status_code == 200
    candidates = r.json()
    assert isinstance(candidates, list)
    assert len(candidates) == 10   # 10 Malleswaram macro sites
