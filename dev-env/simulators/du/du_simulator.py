#!/usr/bin/env python3
"""
DU Simulator — one container per Distributed Unit.

Reads its cell assignments from /config/topology.json (polled every TOPO_POLL_SEC).
When topology changes (cells added/removed, CU parent changes), reconfigures live
without restarting. Writes cell_kpi, du_kpi, ue_mobility, ue_usage to InfluxDB.

KPI models use real hardware specs (peak_dl_mbps, tx_power_w, idle_power_w, band)
from the topology to produce vendor-realistic throughput and power numbers.
"""

import json
import os
import time
import random
import logging
from pathlib import Path
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DU_ID         = os.environ["DU_ID"]
INFLUX_URL    = os.environ.get("INFLUX_URL",    "http://influxdb:8086")
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN",  "telecom-super-secret-auth-token-2026")
INFLUX_ORG    = os.environ.get("INFLUX_ORG",    "telecom")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "telecom_metrics")
INTERVAL_SEC  = int(os.environ.get("INTERVAL_SEC",  "10"))
TOPO_POLL_SEC = int(os.environ.get("TOPO_POLL_SEC", "5"))
TOPOLOGY_FILE = Path(os.environ.get("TOPOLOGY_FILE", "/config/topology.json"))

# Bangalore diurnal load curve (fraction of max_ues active per hour)
HOURLY_LOAD = [
    0.08, 0.06, 0.05, 0.05, 0.06, 0.12,
    0.32, 0.68, 0.88, 0.82, 0.72, 0.66,
    0.64, 0.60, 0.62, 0.68, 0.78, 0.90,
    0.95, 1.00, 0.97, 0.88, 0.62, 0.28,
]

# SINR and RSRP baselines differ by band (at low load, clear-sky)
_SINR_BASE = {"n78": 22.0, "n41": 20.0, "n28": 29.0, "B3": 26.0, "B40": 23.0}
_RSRP_BASE = {"n78": -72,  "n41": -74,  "n28": -64,  "B3": -69,  "B40": -73}

SLICES = ["eMBB"] * 7 + ["URLLC"] * 2 + ["mMTC"] * 1


def load_factor() -> float:
    from datetime import datetime
    return HOURLY_LOAD[datetime.now().hour]


def read_topology() -> dict:
    with open(TOPOLOGY_FILE) as f:
        return json.load(f)


# ── Cell simulation ──────────────────────────────────────────────────────────

class CellState:
    def __init__(self, cell_id: str, cfg: dict):
        self.cell_id = cell_id
        self.cfg     = cfg
        n = int(cfg["max_ues"] * load_factor() * random.uniform(0.8, 1.0))
        self.ue_pool: dict[str, str] = {
            f"UE-{cell_id}-{i:04d}": random.choice(SLICES) for i in range(n)
        }

    @property
    def ues(self) -> int:
        return len(self.ue_pool)

    def tick(self) -> dict:
        c         = self.cfg
        band      = c.get("band", "n78")
        peak_dl   = c.get("peak_dl_mbps", 3600)
        tx_pw     = c.get("tx_power_w", 950)
        idle_pw   = c.get("idle_power_w", int(tx_pw * 0.25))

        target = max(0, min(
            int(c["max_ues"] * load_factor() * random.uniform(0.88, 1.05)),
            c["max_ues"],
        ))
        delta = target - self.ues
        if delta > 0:
            for _ in range(delta):
                self.ue_pool[f"UE-{self.cell_id}-{random.randint(0, 9999):04d}"] = random.choice(SLICES)
        elif delta < 0:
            for uid in random.sample(list(self.ue_pool), min(-delta, self.ues)):
                del self.ue_pool[uid]

        load   = self.ues / max(c["max_ues"], 1)
        prb_dl = min(98.0, load * 100 * random.uniform(0.92, 1.08))
        prb_ul = min(95.0, load * 58  * random.uniform(0.88, 1.12))

        # Vendor-realistic throughput from peak spec
        dl_tput = round(prb_dl / 100 * peak_dl * random.uniform(0.82, 1.18), 2)
        ul_tput = round(prb_ul / 100 * peak_dl * 0.22 * random.uniform(0.80, 1.20), 2)

        # Band-specific SINR / RSRP (higher bands = shorter range = worse at cell edge)
        sinr_base = _SINR_BASE.get(band, 22.0)
        rsrp_base = _RSRP_BASE.get(band, -72)
        sinr_db   = round(sinr_base - load * 15 + random.gauss(0, 2.5), 1)
        rsrp_dbm  = round(rsrp_base - load * 22 + random.gauss(0, 3.0), 1)

        # Vendor-specific power: idle + load-proportional increase
        power_w = max(
            idle_pw * 0.90,
            round(idle_pw + load * (tx_pw - idle_pw) + random.gauss(0, tx_pw * 0.025), 1),
        )

        # Packet loss: near-zero at low load, rises with congestion
        pkt_loss_pct = round(max(0.0, (load - 0.75) * 2.5 + random.gauss(0, 0.05)), 3)

        return dict(
            connected_ues       = self.ues,
            dl_throughput_mbps  = dl_tput,
            ul_throughput_mbps  = ul_tput,
            rsrp_dbm            = rsrp_dbm,
            sinr_db             = sinr_db,
            power_w             = power_w,
            prb_dl_pct          = round(prb_dl, 1),
            prb_ul_pct          = round(prb_ul, 1),
            ho_success_rate     = round(random.uniform(0.962, 0.9995), 4),
            packet_loss_pct     = pkt_loss_pct,
        )

    def mobility_events(self, neighbours: list["CellState"]) -> list[dict]:
        if self.ues < 2 or not neighbours:
            return []
        n_ho   = max(0, int(self.ues * 0.015 * random.random()))
        events = []
        for _ in range(n_ho):
            if not self.ue_pool:
                break
            uid    = random.choice(list(self.ue_pool))
            target = random.choice(neighbours)
            events.append(dict(
                ue_id=uid, source_cell=self.cell_id, target_cell=target.cell_id,
                source_area=self.cfg["area"], target_area=target.cfg["area"],
                event_type="handover",
                rsrp_source=round(-70 + random.gauss(0, 8), 1),
                rsrp_target=round(-62 + random.gauss(0, 8), 1),
                ho_duration_ms=round(random.uniform(18, 65), 1),
                velocity_kmh=round(random.uniform(0, 90), 1),
            ))
            target.ue_pool[uid] = self.ue_pool.pop(uid)
        return events

    def usage_sample(self, n: int = 8) -> list[dict]:
        if not self.ue_pool:
            return []
        out = []
        for uid in random.sample(list(self.ue_pool), min(n, self.ues)):
            sl = self.ue_pool[uid]
            peak = self.cfg.get("peak_dl_mbps", 150)
            if sl == "URLLC":
                dl, ul = random.randint(1_000, 60_000), random.randint(500, 25_000)
                lat, jit = random.uniform(0.5, 4), random.uniform(0.1, 0.8)
            elif sl == "mMTC":
                dl, ul = random.randint(10, 2_000), random.randint(10, 800)
                lat, jit = random.uniform(10, 150), random.uniform(2, 30)
            else:  # eMBB — scale to cell's peak capability
                per_ue_peak = int(peak * 1e6 / max(self.ues, 1))
                dl = random.randint(max(50_000, per_ue_peak // 20), max(100_000, per_ue_peak // 4))
                ul = int(dl * random.uniform(0.08, 0.22))
                lat, jit = random.uniform(5, 35), random.uniform(0.5, 6)
            out.append(dict(ue_id=uid, cell_id=self.cell_id, area=self.cfg["area"],
                            slice_type=sl, dl_bytes=dl, ul_bytes=ul,
                            latency_ms=round(lat, 2), jitter_ms=round(jit, 2),
                            packet_loss=round(random.uniform(0, 0.003), 5)))
        return out


# ── Topology watch ───────────────────────────────────────────────────────────

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


def reconfigure(states: dict[str, CellState], topo: dict) -> tuple[dict[str, CellState], str]:
    du_cfg    = topo["dus"][DU_ID]
    new_cu_id = du_cfg["cu_id"]
    new_ids   = set(du_cfg["cell_ids"])
    old_ids   = set(states)

    for cid in old_ids - new_ids:
        log.info(f"[TOPO] Cell {cid} removed from {DU_ID}")
        del states[cid]

    for cid in new_ids - old_ids:
        log.info(f"[TOPO] Cell {cid} added to {DU_ID}")
        states[cid] = CellState(cid, topo["cells"][cid])

    return states, new_cu_id


# ── InfluxDB helpers ─────────────────────────────────────────────────────────

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


def build_points(states: dict[str, CellState], cu_id: str) -> list[Point]:
    points: list[Point] = []
    cell_list = list(states.values())

    for state in cell_list:
        m = state.tick()
        c = state.cfg
        points.append(
            Point("cell_kpi")
            .tag("cell_id",     state.cell_id)
            .tag("area",        c["area"])
            .tag("band",        c["band"])
            .tag("pci",         str(c["pci"]))
            .tag("du_id",       DU_ID)
            .tag("cu_id",       cu_id)
            .tag("vendor",      c.get("vendor", "unknown"))
            .tag("generation",  c.get("generation", "5G"))
            .field("connected_ues",       m["connected_ues"])
            .field("dl_throughput_mbps",  m["dl_throughput_mbps"])
            .field("ul_throughput_mbps",  m["ul_throughput_mbps"])
            .field("rsrp_dbm",            m["rsrp_dbm"])
            .field("sinr_db",             m["sinr_db"])
            .field("power_w",             m["power_w"])
            .field("prb_dl_pct",          m["prb_dl_pct"])
            .field("prb_ul_pct",          m["prb_ul_pct"])
            .field("ho_success_rate",     m["ho_success_rate"])
            .field("packet_loss_pct",     m["packet_loss_pct"])
        )
        neighbours = [s for s in cell_list if s.cell_id != state.cell_id]
        for ev in state.mobility_events(neighbours):
            points.append(
                Point("ue_mobility")
                .tag("ue_id",        ev["ue_id"])
                .tag("source_cell",  ev["source_cell"])
                .tag("target_cell",  ev["target_cell"])
                .tag("source_area",  ev["source_area"])
                .tag("target_area",  ev["target_area"])
                .tag("event_type",   ev["event_type"])
                .tag("du_id",        DU_ID)
                .field("rsrp_source",    ev["rsrp_source"])
                .field("rsrp_target",    ev["rsrp_target"])
                .field("ho_duration_ms", ev["ho_duration_ms"])
                .field("velocity_kmh",   ev["velocity_kmh"])
            )
        for u in state.usage_sample():
            points.append(
                Point("ue_usage")
                .tag("ue_id",       u["ue_id"])
                .tag("cell_id",     u["cell_id"])
                .tag("area",        u["area"])
                .tag("slice_type",  u["slice_type"])
                .tag("du_id",       DU_ID)
                .field("dl_bytes",    u["dl_bytes"])
                .field("ul_bytes",    u["ul_bytes"])
                .field("latency_ms",  u["latency_ms"])
                .field("jitter_ms",   u["jitter_ms"])
                .field("packet_loss", u["packet_loss"])
            )

    total = sum(s.ues for s in cell_list)
    load  = total / max(sum(s.cfg["max_ues"] for s in cell_list), 1) if cell_list else 0
    points.append(
        Point("du_kpi")
        .tag("du_id", DU_ID).tag("cu_id", cu_id)
        .field("active_ues",           total)
        .field("cell_count",           len(cell_list))
        .field("cpu_pct",              round(20 + load * 62 + random.gauss(0, 3), 1))
        .field("memory_pct",           round(30 + load * 45 + random.gauss(0, 2), 1))
        .field("fronthaul_latency_us", round(random.uniform(50, 200), 1))
        .field("processing_delay_ms",  round(random.uniform(0.1, 0.9), 3))
        .field("f1_msg_per_sec",       int(total * random.uniform(0.5, 2.0)))
    )

    return points


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info(f"DU Simulator {DU_ID} starting → {INFLUX_URL}")

    for _ in range(30):
        if TOPOLOGY_FILE.exists():
            break
        log.warning(f"Waiting for {TOPOLOGY_FILE} ...")
        time.sleep(3)

    client    = connect(INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    topo          = read_topology()
    states: dict[str, CellState] = {}
    states, cu_id = reconfigure(states, topo)
    topology_changed()  # prime mtime

    log.info(f"{DU_ID} → CU={cu_id}, cells={list(states)}")
    last_push = 0.0

    while True:
        now = time.monotonic()

        if topology_changed():
            topo          = read_topology()
            states, cu_id = reconfigure(states, topo)
            log.info(f"[TOPO UPDATE] {DU_ID} → CU={cu_id}, cells={list(states)}")

        if now - last_push >= INTERVAL_SEC:
            if states:
                pts = build_points(states, cu_id)
                try:
                    write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=pts)
                    log.info(f"Wrote {len(pts)} pts | cells={list(states)} | UEs={[s.ues for s in states.values()]}")
                except Exception as e:
                    log.error(f"Write error: {e}")
            last_push = now

        time.sleep(min(TOPO_POLL_SEC, INTERVAL_SEC))


if __name__ == "__main__":
    main()
