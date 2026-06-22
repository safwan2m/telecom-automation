"""Tests for agents/controller/controller.py — REST API via FastAPI TestClient."""
import json
import copy
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import controller
from conftest import MINIMAL_TOPOLOGY, write_topology


@pytest.fixture(autouse=True)
def mock_influx(monkeypatch):
    """Replace InfluxDB with a mock that ping()s OK and returns empty query results."""
    mock = MagicMock()
    mock.ping.return_value = True
    mock.query_api.return_value.query.return_value = []
    mock.write_api.return_value.write.return_value = None
    monkeypatch.setattr(controller, "get_influx", lambda: mock)
    # Reset lazy cache so each test gets the fresh mock
    monkeypatch.setattr(controller, "_influx", None)
    return mock


@pytest.fixture
def topo_file(tmp_path, monkeypatch):
    """Write a fresh topology.json for each test and point TOPOLOGY_FILE at it."""
    f = write_topology(tmp_path / "topology.json")
    monkeypatch.setattr(controller, "TOPOLOGY_FILE", f)
    return f


@pytest.fixture
def client(topo_file):
    return TestClient(controller.app)


# ── /health ──────────────────────────────────────────────────────────────────

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["topology_exists"] is True


# ── /topology ─────────────────────────────────────────────────────────────────

def test_get_topology_returns_dict(client):
    r = client.get("/topology")
    assert r.status_code == 200
    body = r.json()
    assert "cells" in body
    assert "dus" in body
    assert "version" in body


def test_get_topology_version(client):
    r = client.get("/topology")
    assert r.json()["version"] == 1


# ── /cells ────────────────────────────────────────────────────────────────────

def test_get_cells_returns_list(client):
    r = client.get("/cells")
    assert r.status_code == 200
    cells = r.json()
    assert isinstance(cells, list)
    assert len(cells) == 3   # MINIMAL_TOPOLOGY has 3 cells


def test_get_cells_filter_by_du(client):
    r = client.get("/cells?du_id=DU-1")
    assert r.status_code == 200
    cells = r.json()
    assert len(cells) == 2
    assert all(c["du_id"] == "DU-1" for c in cells)


def test_get_cell_by_id(client):
    r = client.get("/cells/CELL_A")
    assert r.status_code == 200
    body = r.json()
    assert body["cell_id"] == "CELL_A"
    assert body["du_id"] == "DU-1"


def test_get_cell_not_found(client):
    r = client.get("/cells/DOES_NOT_EXIST")
    assert r.status_code == 404


# ── /network ──────────────────────────────────────────────────────────────────

def test_get_network_shape(client):
    r = client.get("/network")
    assert r.status_code == 200
    body = r.json()
    assert "cells" in body and "dus" in body and "cus" in body
    assert len(body["cells"]) == 3


def test_get_network_kpis_empty_without_influx(client):
    r = client.get("/network")
    # With mocked InfluxDB returning [], KPI fields should be empty dicts
    for cell_data in r.json()["cells"].values():
        assert cell_data["kpi"] == {}


# ── POST /move/cell ──────────────────────────────────────────────────────────

def test_move_cell_success(client, topo_file):
    r = client.post("/move/cell", json={"cell_id": "CELL_A", "to_du_id": "DU-2"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["cell_id"] == "CELL_A"
    assert body["from_du"] == "DU-1"
    assert body["to_du"] == "DU-2"

    # Verify topology file updated
    topo = json.loads(topo_file.read_text())
    assert "CELL_A" in topo["dus"]["DU-2"]["cell_ids"]
    assert "CELL_A" not in topo["dus"]["DU-1"]["cell_ids"]
    assert topo["version"] == 2


def test_move_cell_increments_version(client, topo_file):
    client.post("/move/cell", json={"cell_id": "CELL_A", "to_du_id": "DU-2"})
    client.post("/move/cell", json={"cell_id": "CELL_B", "to_du_id": "DU-2"})
    topo = json.loads(topo_file.read_text())
    assert topo["version"] == 3


def test_move_cell_same_du_is_noop(client, topo_file):
    r = client.post("/move/cell", json={"cell_id": "CELL_A", "to_du_id": "DU-1"})
    assert r.status_code == 200
    assert r.json()["status"] == "no-op"
    # Topology should not have changed
    topo = json.loads(topo_file.read_text())
    assert topo["version"] == 1


def test_move_cell_unknown_cell(client):
    r = client.post("/move/cell", json={"cell_id": "GHOST", "to_du_id": "DU-2"})
    assert r.status_code == 404


def test_move_cell_unknown_du(client):
    r = client.post("/move/cell", json={"cell_id": "CELL_A", "to_du_id": "DU-99"})
    assert r.status_code == 404


# ── POST /move/du ─────────────────────────────────────────────────────────────

def test_move_du_requires_second_cu(client, topo_file, monkeypatch):
    """Add a second CU then move DU-1 to it."""
    topo = json.loads(topo_file.read_text())
    topo["cus"]["CU-2"] = {"host": "cu-2", "region": "Test", "du_ids": []}
    topo_file.write_text(json.dumps(topo))

    r = client.post("/move/du", json={"du_id": "DU-1", "to_cu_id": "CU-2"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["from_cu"] == "CU-1"
    assert body["to_cu"] == "CU-2"


# ── /dus and /cus ─────────────────────────────────────────────────────────────

def test_get_dus(client):
    r = client.get("/dus")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_get_cus(client):
    r = client.get("/cus")
    assert r.status_code == 200
    assert len(r.json()) == 1
