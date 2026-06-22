"""
Root conftest — adds agent source directories to sys.path so test files can
import directly from agents without package scaffolding.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "agents" / "planning"))
sys.path.insert(0, str(ROOT / "agents" / "controller"))
sys.path.insert(0, str(ROOT / "agents" / "kpi_agent"))


# ── Shared topology fixture ──────────────────────────────────────────────────

MINIMAL_TOPOLOGY = {
    "version": 1,
    "last_updated": datetime.now(timezone.utc).isoformat(),
    "updated_by": "test",
    "meta": {},
    "cells": {
        "CELL_A": {
            "area": "Malleswaram", "lat": 13.007, "lon": 77.576,
            "generation": "5G", "band": "n78", "freq_mhz": 3500,
            "pci": 1, "vendor": "Nokia", "hardware_model": "AirScale MAA 64T64R",
            "antenna_config": "64T64R", "peak_dl_mbps": 3800,
            "tx_power_w": 1000, "idle_power_w": 250, "max_ues": 900,
        },
        "CELL_B": {
            "area": "Malleswaram", "lat": 13.008, "lon": 77.577,
            "generation": "5G", "band": "n78", "freq_mhz": 3500,
            "pci": 2, "vendor": "Ericsson", "hardware_model": "AIR 6449",
            "antenna_config": "64T64R", "peak_dl_mbps": 3600,
            "tx_power_w": 950, "idle_power_w": 240, "max_ues": 900,
        },
        "CELL_C": {
            "area": "Malleswaram", "lat": 13.009, "lon": 77.578,
            "generation": "4G", "band": "B3", "freq_mhz": 1800,
            "pci": 3, "vendor": "Samsung", "hardware_model": "TM500 64T64R",
            "antenna_config": "4T4R", "peak_dl_mbps": 150,
            "tx_power_w": 200, "idle_power_w": 50, "max_ues": 250,
        },
    },
    "dus": {
        "DU-1": {"cu_id": "CU-1", "host": "du-1", "cell_ids": ["CELL_A", "CELL_B"]},
        "DU-2": {"cu_id": "CU-1", "host": "du-2", "cell_ids": ["CELL_C"]},
    },
    "cus": {
        "CU-1": {"host": "cu-1", "region": "Malleswaram", "du_ids": ["DU-1", "DU-2"]},
    },
}


def write_topology(path: Path, topo: dict = None) -> Path:
    path.write_text(json.dumps(topo or MINIMAL_TOPOLOGY, indent=2))
    return path
