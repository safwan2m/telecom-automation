#!/usr/bin/env python3
"""
CU Simulator — one container per Centralised Unit.

Reads its DU list from /config/topology.json (polled every TOPO_POLL_SEC).
Reports CU-CP and CU-UP plane metrics: RRC connections, PDCP throughput,
F1/N2/N3 interface latency, CPU/memory. Reconfigures live when topology changes.
"""

import json
import os
import time
import random
import logging
from datetime import datetime
from pathlib import Path
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CU_ID         = os.environ["CU_ID"]
INFLUX_URL    = os.environ.get("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN",  "telecom-super-secret-auth-token-2026")
INFLUX_ORG    = os.environ.get("INFLUX_ORG",    "telecom")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "telecom_metrics")
INTERVAL_SEC  = int(os.environ.get("INTERVAL_SEC",  "10"))
TOPO_POLL_SEC = int(os.environ.get("TOPO_POLL_SEC", "5"))
TOPOLOGY_FILE = Path(os.environ.get("TOPOLOGY_FILE", "/config/topology.json"))

HOURLY_LOAD = [
    0.08, 0.06, 0.05, 0.05, 0.06, 0.12,
    0.30, 0.65, 0.85, 0.80, 0.70, 0.65,
    0.65, 0.60, 0.62, 0.68, 0.78, 0.90,
    0.95, 1.00, 0.97, 0.88, 0.62, 0.30,
]


def load_factor() -> float:
    return HOURLY_LOAD[datetime.now().hour]


def read_topology() -> dict:
    with open(TOPOLOGY_FILE) as f:
        return json.load(f)


def get_cu_domain(topo: dict) -> tuple[list[str], int]:
    """Return (du_ids, total_max_ues) for this CU's current domain."""
    du_ids = topo["cus"][CU_ID]["du_ids"]
    total_max_ues = sum(
        topo["cells"][cid]["max_ues"]
        for du_id in du_ids
        for cid in topo["dus"][du_id]["cell_ids"]
    )
    return du_ids, total_max_ues


_topo_mtime: float = 0.0


def topology_changed() -> bool:
    global _topo_mtime
    try:
        mtime = TOPOLOGY_FILE.stat().st_mtime
        if mtime != _topo_mtime:
            _topo_mtime = mtime
            return True
    except Exception:
        pass
    return False


def connect(url, token, org) -> InfluxDBClient:
    for attempt in range(1, 25):
        try:
            c = InfluxDBClient(url=url, token=token, org=org)
            c.ping()
            log.info("Connected to InfluxDB.")
            return c
        except Exception as e:
            log.warning(f"Attempt {attempt}/24 — {e}")
            time.sleep(6)
    raise RuntimeError("Cannot connect to InfluxDB.")


def build_cu_point(du_ids: list[str], total_max_ues: int) -> Point:
    lf        = load_factor()
    du_count  = len(du_ids)
    est_ues   = int(total_max_ues * lf * random.uniform(0.88, 1.05))
    load      = est_ues / max(total_max_ues, 1)

    # CU-CP metrics
    rrc_connected     = est_ues
    rrc_idle          = int(est_ues * random.uniform(0.05, 0.20))
    rrc_setup_rate    = int(random.uniform(5, 40) * du_count)
    inter_du_ho_rate  = int(random.uniform(1, 10) * du_count)

    # CU-UP metrics
    pdcp_dl_gbps = est_ues * random.uniform(1.2e-5, 6e-5)
    pdcp_ul_gbps = pdcp_dl_gbps * random.uniform(0.08, 0.18)

    # Interface latencies
    f1_latency_ms  = round(random.uniform(0.3, 2.5), 2)   # CU ↔ DU
    n2_latency_ms  = round(random.uniform(1.0, 8.0), 2)   # CU ↔ AMF
    n3_latency_ms  = round(random.uniform(0.5, 4.0), 2)   # CU ↔ UPF
    e1_latency_ms  = round(random.uniform(0.1, 1.0), 2)   # CU-CP ↔ CU-UP

    return (
        Point("cu_kpi")
        .tag("cu_id", CU_ID)
        .field("du_count",          du_count)
        .field("rrc_connected",     rrc_connected)
        .field("rrc_idle",          rrc_idle)
        .field("rrc_setup_rate",    rrc_setup_rate)
        .field("inter_du_ho_rate",  inter_du_ho_rate)
        .field("pdcp_dl_gbps",      round(pdcp_dl_gbps, 6))
        .field("pdcp_ul_gbps",      round(pdcp_ul_gbps, 6))
        .field("f1_latency_ms",     f1_latency_ms)
        .field("n2_latency_ms",     n2_latency_ms)
        .field("n3_latency_ms",     n3_latency_ms)
        .field("e1_latency_ms",     e1_latency_ms)
        .field("cpu_pct",           round(15 + load * 55 + random.gauss(0, 4), 1))
        .field("memory_pct",        round(25 + load * 40 + random.gauss(0, 2), 1))
    )


def main():
    log.info(f"CU Simulator {CU_ID} starting → {INFLUX_URL}")

    for _ in range(30):
        if TOPOLOGY_FILE.exists():
            break
        log.warning(f"Waiting for {TOPOLOGY_FILE} ...")
        time.sleep(3)

    client    = connect(INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    topo                  = read_topology()
    du_ids, total_max_ues = get_cu_domain(topo)
    topology_changed()    # prime mtime

    log.info(f"{CU_ID} serving DUs: {du_ids} | total_max_ues={total_max_ues}")
    last_push = 0.0

    while True:
        now = time.monotonic()

        if topology_changed():
            topo                  = read_topology()
            du_ids, total_max_ues = get_cu_domain(topo)
            log.info(f"[TOPO UPDATE] {CU_ID} → DUs={du_ids}, max_ues={total_max_ues}")

        if now - last_push >= INTERVAL_SEC:
            pt = build_cu_point(du_ids, total_max_ues)
            try:
                write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=[pt])
                log.info(f"{CU_ID}: DUs={du_ids} | est_ues≈{int(total_max_ues * load_factor())}")
            except Exception as e:
                log.error(f"Write error: {e}")
            last_push = now

        time.sleep(min(TOPO_POLL_SEC, INTERVAL_SEC))


if __name__ == "__main__":
    main()
