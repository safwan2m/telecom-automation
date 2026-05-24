#!/usr/bin/env python3
"""
Core Simulator — pushes AMF, SMF, UPF metrics for the Bangalore 5G core to InfluxDB.

Simulates one AMF, one SMF, and one UPF instance serving all RAN cells.
"""

import os
import time
import random
import logging
from datetime import datetime
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

INFLUX_URL    = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN", "telecom-super-secret-auth-token-2026")
INFLUX_ORG    = os.environ.get("INFLUX_ORG", "telecom")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "telecom_metrics")
INTERVAL_SEC  = int(os.environ.get("INTERVAL_SEC", "10"))

MAX_UES_TOTAL = 4440   # sum of all cell max_ues across both simulators
IP_POOL_SIZE  = 65536  # /16 pool

HOURLY_LOAD = [
    0.08, 0.06, 0.05, 0.05, 0.06, 0.12,
    0.30, 0.65, 0.85, 0.80, 0.70, 0.65,
    0.65, 0.60, 0.62, 0.68, 0.78, 0.90,
    0.95, 1.00, 0.97, 0.88, 0.62, 0.30,
]


def load_factor() -> float:
    return HOURLY_LOAD[datetime.now().hour]


class CoreState:
    """Smoothly-evolving state for the 5GC instance."""

    def __init__(self):
        lf = load_factor()
        self.registered_ues  = int(MAX_UES_TOTAL * lf * 1.08)
        self.active_sessions = int(self.registered_ues * 0.90)

    def tick(self) -> dict:
        lf = load_factor()
        # Exponential smoothing toward target
        target_reg            = int(MAX_UES_TOTAL * lf * random.uniform(1.04, 1.12))
        self.registered_ues   = int(self.registered_ues * 0.88 + target_reg * 0.12)
        self.active_sessions  = int(self.registered_ues * random.uniform(0.84, 0.96))

        # UPF throughput: roughly sum-of-sessions × per-session rate
        dl_gbps = self.active_sessions * random.uniform(1.5e-5, 7e-5)
        ul_gbps = dl_gbps * random.uniform(0.08, 0.16)

        return dict(
            amf=dict(
                component          = "AMF",
                instance_id        = "AMF-BLR-01",
                registered_ues     = self.registered_ues,
                active_sessions    = self.active_sessions,
                nas_msg_per_sec    = int(self.registered_ues * random.uniform(0.4, 2.2)),
                paging_per_sec     = int(random.uniform(5, 80)),
                handover_per_sec   = int(random.uniform(2, 30)),
                cpu_pct            = round(20 + lf * 55 + random.gauss(0, 3), 1),
                memory_pct         = round(28 + lf * 42 + random.gauss(0, 2), 1),
                n2_latency_ms      = round(random.uniform(1, 9), 2),
            ),
            smf=dict(
                component          = "SMF",
                instance_id        = "SMF-BLR-01",
                active_pdu_sessions= self.active_sessions,
                session_setup_rate = int(random.uniform(4, 35)),
                session_release_rate= int(random.uniform(2, 25)),
                ip_pool_utilization_pct= round(self.active_sessions / IP_POOL_SIZE * 100, 2),
                cpu_pct            = round(15 + lf * 52 + random.gauss(0, 4), 1),
                memory_pct         = round(25 + lf * 38 + random.gauss(0, 2), 1),
                n4_latency_ms      = round(random.uniform(0.5, 6), 2),
            ),
            upf=dict(
                component          = "UPF",
                instance_id        = "UPF-BLR-01",
                dl_throughput_gbps = round(dl_gbps, 5),
                ul_throughput_gbps = round(ul_gbps, 5),
                active_tunnels     = self.active_sessions,
                packet_drop_rate   = round(random.uniform(0, 0.0015), 6),
                gtp_encap_errors   = random.randint(0, 5),
                cpu_pct            = round(25 + lf * 60 + random.gauss(0, 4), 1),
                memory_pct         = round(35 + lf * 35 + random.gauss(0, 2), 1),
            ),
        )


def connect_with_retry(url: str, token: str, org: str) -> InfluxDBClient:
    for attempt in range(1, 25):
        try:
            client = InfluxDBClient(url=url, token=token, org=org)
            client.ping()
            log.info("Connected to InfluxDB.")
            return client
        except Exception as exc:
            log.warning(f"Attempt {attempt}/24 — InfluxDB not ready: {exc}")
            time.sleep(6)
    raise RuntimeError("Could not connect to InfluxDB after 24 attempts.")


def main():
    log.info(f"Core Simulator starting → {INFLUX_URL}")
    client    = connect_with_retry(INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    state     = CoreState()

    while True:
        m      = state.tick()
        points = []

        for key in ("amf", "smf", "upf"):
            d = m[key]
            p = (Point("core_kpi")
                 .tag("component",   d["component"])
                 .tag("instance_id", d["instance_id"]))
            for field, val in d.items():
                if field not in ("component", "instance_id"):
                    p = p.field(field, val)
            points.append(p)

        try:
            write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
            log.info(
                f"AMF registered_ues={m['amf']['registered_ues']} | "
                f"SMF sessions={m['smf']['active_pdu_sessions']} | "
                f"UPF DL={m['upf']['dl_throughput_gbps']:.5f} Gbps"
            )
        except Exception as exc:
            log.error(f"Write failed: {exc}")

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
