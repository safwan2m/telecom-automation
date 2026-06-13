#!/usr/bin/env python3
"""
Dataset generator for the Malleswaram 4G/5G digital twin.

Generates a CSV (~50,400 rows: 30 cells × 24 h × 70 days) whose schema
merges the four reference Kaggle datasets:
  1. suraj520/cellular-network-performance-data
  2. srikumarnayak/5g-network-kpi-dataset
  3. praveenaparimi/telecom-network-dataset
  4. suraj520/cellular-network-analysis-dataset

Key additions vs. current DU-simulator output:
  rsrq_db, cqi, mcs, bler_pct, latency_ms (cell-level),
  interference_dbm, day_of_week, is_weekend, cell_state label

Usage:
    py dataset_generator.py                         # 70 days → generated_dataset.csv
    py dataset_generator.py --days 14 --out /tmp/test.csv
"""

import argparse
import csv
import random
from collections import Counter
from datetime import datetime, timedelta

# ── 30-cell Malleswaram inventory (mirrors topology.json) ─────────────────────

CELLS = [
    # ── Site RWS (Railway Station) — Nokia — high-traffic ─────────────────────
    {"cell_id":"MLS_RWS_01","area":"Malleswaram","lat":13.0080,"lon":77.5760,
     "generation":"5G","band":"n78","freq_mhz":3500,"pci":1,
     "vendor":"Nokia","hardware_model":"AirScale MAA 64T64R",
     "tx_power_w":1000,"idle_power_w":250,"max_ues":900,"peak_dl_mbps":3800},
    {"cell_id":"MLS_RWS_02","area":"Malleswaram","lat":13.0080,"lon":77.5760,
     "generation":"5G","band":"n41","freq_mhz":2500,"pci":101,
     "vendor":"Nokia","hardware_model":"AirScale MAA 64T64R",
     "tx_power_w":1000,"idle_power_w":250,"max_ues":700,"peak_dl_mbps":3000},
    {"cell_id":"MLS_RWS_03","area":"Malleswaram","lat":13.0080,"lon":77.5760,
     "generation":"4G","band":"B3","freq_mhz":1800,"pci":201,
     "vendor":"Nokia","hardware_model":"AWHFA",
     "tx_power_w":200,"idle_power_w":50,"max_ues":250,"peak_dl_mbps":150},
    # ── Site 18C (18th Cross) — Ericsson ──────────────────────────────────────
    {"cell_id":"MLS_18C_01","area":"Malleswaram","lat":13.0030,"lon":77.5670,
     "generation":"5G","band":"n78","freq_mhz":3500,"pci":4,
     "vendor":"Ericsson","hardware_model":"AIR 6449",
     "tx_power_w":950,"idle_power_w":240,"max_ues":750,"peak_dl_mbps":3600},
    {"cell_id":"MLS_18C_02","area":"Malleswaram","lat":13.0030,"lon":77.5670,
     "generation":"5G","band":"n41","freq_mhz":2500,"pci":104,
     "vendor":"Ericsson","hardware_model":"AIR 6449",
     "tx_power_w":950,"idle_power_w":240,"max_ues":700,"peak_dl_mbps":3000},
    {"cell_id":"MLS_18C_03","area":"Malleswaram","lat":13.0030,"lon":77.5670,
     "generation":"4G","band":"B3","freq_mhz":1800,"pci":204,
     "vendor":"Ericsson","hardware_model":"RBS 6402",
     "tx_power_w":200,"idle_power_w":50,"max_ues":250,"peak_dl_mbps":150},
    # ── Site BEL (BEL Circle) — Samsung ───────────────────────────────────────
    {"cell_id":"MLS_BEL_01","area":"Malleswaram","lat":13.0110,"lon":77.5630,
     "generation":"5G","band":"n78","freq_mhz":3500,"pci":7,
     "vendor":"Samsung","hardware_model":"TM500 64T64R",
     "tx_power_w":900,"idle_power_w":225,"max_ues":700,"peak_dl_mbps":3400},
    {"cell_id":"MLS_BEL_02","area":"Malleswaram","lat":13.0110,"lon":77.5630,
     "generation":"4G","band":"B40","freq_mhz":2300,"pci":107,
     "vendor":"Samsung","hardware_model":"RRU",
     "tx_power_w":200,"idle_power_w":50,"max_ues":300,"peak_dl_mbps":150},
    {"cell_id":"MLS_BEL_03","area":"Malleswaram","lat":13.0110,"lon":77.5630,
     "generation":"4G","band":"B3","freq_mhz":1800,"pci":207,
     "vendor":"Samsung","hardware_model":"RRU",
     "tx_power_w":200,"idle_power_w":50,"max_ues":250,"peak_dl_mbps":150},
    # ── Site SNK (Sankey Tank) — ZTE ──────────────────────────────────────────
    {"cell_id":"MLS_SNK_01","area":"Malleswaram","lat":13.0060,"lon":77.5740,
     "generation":"5G","band":"n78","freq_mhz":3500,"pci":10,
     "vendor":"ZTE","hardware_model":"AAU 5614",
     "tx_power_w":1000,"idle_power_w":250,"max_ues":680,"peak_dl_mbps":3200},
    {"cell_id":"MLS_SNK_02","area":"Malleswaram","lat":13.0060,"lon":77.5740,
     "generation":"5G","band":"n41","freq_mhz":2500,"pci":110,
     "vendor":"ZTE","hardware_model":"AAU 5614",
     "tx_power_w":1000,"idle_power_w":250,"max_ues":680,"peak_dl_mbps":3000},
    {"cell_id":"MLS_SNK_03","area":"Malleswaram","lat":13.0060,"lon":77.5740,
     "generation":"4G","band":"B3","freq_mhz":1800,"pci":210,
     "vendor":"ZTE","hardware_model":"RRU",
     "tx_power_w":200,"idle_power_w":50,"max_ues":250,"peak_dl_mbps":150},
    # ── Site SPG (Sampige Road) — Nokia — high-traffic ─────────────────────────
    {"cell_id":"MLS_SPG_01","area":"Malleswaram","lat":12.9990,"lon":77.5700,
     "generation":"5G","band":"n78","freq_mhz":3500,"pci":13,
     "vendor":"Nokia","hardware_model":"AirScale MAA 64T64R",
     "tx_power_w":1000,"idle_power_w":250,"max_ues":900,"peak_dl_mbps":3800},
    {"cell_id":"MLS_SPG_02","area":"Malleswaram","lat":12.9990,"lon":77.5700,
     "generation":"5G","band":"n41","freq_mhz":2500,"pci":113,
     "vendor":"Nokia","hardware_model":"AirScale MAA 64T64R",
     "tx_power_w":1000,"idle_power_w":250,"max_ues":700,"peak_dl_mbps":3000},
    {"cell_id":"MLS_SPG_03","area":"Malleswaram","lat":12.9990,"lon":77.5700,
     "generation":"4G","band":"B3","freq_mhz":1800,"pci":213,
     "vendor":"Nokia","hardware_model":"AWHFA",
     "tx_power_w":200,"idle_power_w":50,"max_ues":250,"peak_dl_mbps":150},
    # ── Site 3MN (3rd Main) — Ericsson ────────────────────────────────────────
    {"cell_id":"MLS_3MN_01","area":"Malleswaram","lat":13.0010,"lon":77.5600,
     "generation":"5G","band":"n78","freq_mhz":3500,"pci":16,
     "vendor":"Ericsson","hardware_model":"AIR 3221",
     "tx_power_w":950,"idle_power_w":240,"max_ues":750,"peak_dl_mbps":3600},
    {"cell_id":"MLS_3MN_02","area":"Malleswaram","lat":13.0010,"lon":77.5600,
     "generation":"4G","band":"B40","freq_mhz":2300,"pci":116,
     "vendor":"Ericsson","hardware_model":"RBS 6402",
     "tx_power_w":200,"idle_power_w":50,"max_ues":300,"peak_dl_mbps":150},
    {"cell_id":"MLS_3MN_03","area":"Malleswaram","lat":13.0010,"lon":77.5600,
     "generation":"4G","band":"B3","freq_mhz":1800,"pci":216,
     "vendor":"Ericsson","hardware_model":"RBS 6402",
     "tx_power_w":200,"idle_power_w":50,"max_ues":250,"peak_dl_mbps":150},
    # ── Site 10C (10th Cross) — Samsung — high-traffic ─────────────────────────
    {"cell_id":"MLS_10C_01","area":"Malleswaram","lat":13.0040,"lon":77.5710,
     "generation":"5G","band":"n78","freq_mhz":3500,"pci":19,
     "vendor":"Samsung","hardware_model":"TM500 64T64R",
     "tx_power_w":900,"idle_power_w":225,"max_ues":700,"peak_dl_mbps":3400},
    {"cell_id":"MLS_10C_02","area":"Malleswaram","lat":13.0040,"lon":77.5710,
     "generation":"5G","band":"n41","freq_mhz":2500,"pci":119,
     "vendor":"Samsung","hardware_model":"TM500 64T64R",
     "tx_power_w":900,"idle_power_w":225,"max_ues":700,"peak_dl_mbps":3000},
    {"cell_id":"MLS_10C_03","area":"Malleswaram","lat":13.0040,"lon":77.5710,
     "generation":"4G","band":"B3","freq_mhz":1800,"pci":219,
     "vendor":"Samsung","hardware_model":"RRU",
     "tx_power_w":200,"idle_power_w":50,"max_ues":250,"peak_dl_mbps":150},
    # ── Site MGR (Margosa Road) — ZTE ─────────────────────────────────────────
    {"cell_id":"MLS_MGR_01","area":"Malleswaram","lat":12.9960,"lon":77.5640,
     "generation":"5G","band":"n78","freq_mhz":3500,"pci":22,
     "vendor":"ZTE","hardware_model":"AAU 5614",
     "tx_power_w":1000,"idle_power_w":250,"max_ues":680,"peak_dl_mbps":3200},
    {"cell_id":"MLS_MGR_02","area":"Malleswaram","lat":12.9960,"lon":77.5640,
     "generation":"4G","band":"B40","freq_mhz":2300,"pci":122,
     "vendor":"ZTE","hardware_model":"RRU",
     "tx_power_w":200,"idle_power_w":50,"max_ues":300,"peak_dl_mbps":150},
    {"cell_id":"MLS_MGR_03","area":"Malleswaram","lat":12.9960,"lon":77.5640,
     "generation":"4G","band":"B3","freq_mhz":1800,"pci":222,
     "vendor":"ZTE","hardware_model":"RRU",
     "tx_power_w":200,"idle_power_w":50,"max_ues":250,"peak_dl_mbps":150},
    # ── Site CHD (Chord Road) — Nokia ─────────────────────────────────────────
    {"cell_id":"MLS_CHD_01","area":"Malleswaram","lat":12.9930,"lon":77.5560,
     "generation":"5G","band":"n78","freq_mhz":3500,"pci":25,
     "vendor":"Nokia","hardware_model":"AirScale MAA 64T64R",
     "tx_power_w":1000,"idle_power_w":250,"max_ues":900,"peak_dl_mbps":3800},
    {"cell_id":"MLS_CHD_02","area":"Malleswaram","lat":12.9930,"lon":77.5560,
     "generation":"4G","band":"B40","freq_mhz":2300,"pci":125,
     "vendor":"Nokia","hardware_model":"AWHFA",
     "tx_power_w":200,"idle_power_w":50,"max_ues":300,"peak_dl_mbps":150},
    {"cell_id":"MLS_CHD_03","area":"Malleswaram","lat":12.9930,"lon":77.5560,
     "generation":"4G","band":"B3","freq_mhz":1800,"pci":225,
     "vendor":"Nokia","hardware_model":"AWHFA",
     "tx_power_w":200,"idle_power_w":50,"max_ues":250,"peak_dl_mbps":150},
    # ── Site 6CR (6th Cross) — Ericsson ───────────────────────────────────────
    {"cell_id":"MLS_6CR_01","area":"Malleswaram","lat":12.9970,"lon":77.5580,
     "generation":"5G","band":"n78","freq_mhz":3500,"pci":28,
     "vendor":"Ericsson","hardware_model":"AIR 6449",
     "tx_power_w":950,"idle_power_w":240,"max_ues":750,"peak_dl_mbps":3600},
    {"cell_id":"MLS_6CR_02","area":"Malleswaram","lat":12.9970,"lon":77.5580,
     "generation":"4G","band":"B40","freq_mhz":2300,"pci":128,
     "vendor":"Ericsson","hardware_model":"RBS 6402",
     "tx_power_w":200,"idle_power_w":50,"max_ues":300,"peak_dl_mbps":150},
    {"cell_id":"MLS_6CR_03","area":"Malleswaram","lat":12.9970,"lon":77.5580,
     "generation":"4G","band":"B3","freq_mhz":1800,"pci":228,
     "vendor":"Ericsson","hardware_model":"RBS 6402",
     "tx_power_w":200,"idle_power_w":50,"max_ues":250,"peak_dl_mbps":150},
]

# ── Temporal patterns ──────────────────────────────────────────────────────────

# Bangalore diurnal load curve (fraction of peak demand per hour, weekday)
HOURLY_LOAD = [
    0.08, 0.06, 0.05, 0.05, 0.06, 0.12,
    0.32, 0.68, 0.88, 0.82, 0.72, 0.66,
    0.64, 0.60, 0.62, 0.68, 0.78, 0.90,
    0.95, 1.00, 0.97, 0.88, 0.62, 0.28,
]
WEEKEND_FACTOR = 0.75

# ── RF baselines by band ───────────────────────────────────────────────────────

_SINR_BASE = {"n78": 22.0, "n41": 20.0, "B3": 26.0, "B40": 23.0}
_RSRP_BASE = {"n78": -72,  "n41": -74,  "B3": -69,  "B40": -73}

# ── Class label logic ──────────────────────────────────────────────────────────

# Realistic class proportions across the full dataset
_BASE_WEIGHTS    = [0.70, 0.15, 0.08, 0.05, 0.02]
_PEAK_WEIGHTS    = [0.50, 0.35, 0.02, 0.10, 0.03]   # busy hour, weekday
_OFFPEAK_WEIGHTS = [0.40, 0.02, 0.40, 0.03, 0.15]   # deep night
CLASS_LABELS     = ["NORMAL", "OVERLOAD", "UNDERLOAD", "SINR_LOW", "POWER_WASTE"]


def _pick_state(hour: int, dow: int, base_load: float) -> str:
    is_weekend = dow >= 5
    if not is_weekend and 8 <= hour <= 20 and base_load > 0.75:
        w = _PEAK_WEIGHTS
    elif base_load < 0.12:
        w = _OFFPEAK_WEIGHTS
    else:
        w = _BASE_WEIGHTS
    return random.choices(CLASS_LABELS, weights=w)[0]


# ── KPI simulation ─────────────────────────────────────────────────────────────

def _simulate(cell: dict, hour: int, dow: int, state: str) -> dict:
    band       = cell["band"]
    max_ues    = cell["max_ues"]
    peak_dl    = cell["peak_dl_mbps"]
    tx_pw      = cell["tx_power_w"]
    idle_pw    = cell["idle_power_w"]

    is_weekend = dow >= 5
    base_load  = HOURLY_LOAD[hour] * (WEEKEND_FACTOR if is_weekend else 1.0)
    sinr_base  = _SINR_BASE.get(band, 22.0)
    rsrp_base  = _RSRP_BASE.get(band, -72)

    if state == "NORMAL":
        load        = min(base_load * random.uniform(0.85, 1.0), 0.84)
        sinr_offset = random.gauss(0, 2.5)
    elif state == "OVERLOAD":
        load        = random.uniform(0.87, 1.0)
        sinr_offset = random.gauss(-8, 2.0)
    elif state == "UNDERLOAD":
        load        = random.uniform(0.01, 0.18)
        sinr_offset = random.gauss(3, 1.5)
    elif state == "SINR_LOW":
        load        = base_load * random.uniform(0.5, 1.0)
        sinr_offset = random.gauss(-18, 3.0)
    else:   # POWER_WASTE — 5G mMIMO idle but RF still on
        load        = random.uniform(0.01, 0.06)
        sinr_offset = random.gauss(4, 1.5)

    ues     = max(1, int(load * max_ues * random.uniform(0.90, 1.05)))
    prb_dl  = min(98.0, load * 100 * random.uniform(0.92, 1.08))
    prb_ul  = min(95.0, load * 58  * random.uniform(0.88, 1.12))
    dl_tput = round(prb_dl / 100 * peak_dl * random.uniform(0.82, 1.18), 2)
    ul_tput = round(prb_ul / 100 * peak_dl * 0.22 * random.uniform(0.80, 1.20), 2)

    sinr_db  = round(sinr_base + sinr_offset, 1)
    rsrp_dbm = round(rsrp_base - load * 22 + random.gauss(0, 3.0), 1)
    rsrq_raw = -10.0 + sinr_db * 0.3 + random.gauss(0, 1.5)
    rsrq_db  = round(max(-19.5, min(-3.0, rsrq_raw)), 1)

    # CQI 0–15, correlated with SINR
    cqi = max(0, min(15, int((sinr_db + 5) / 2.5 + random.gauss(0, 0.8))))

    # MCS 0–28, correlated with CQI
    mcs = max(0, min(28, int(cqi * 1.8 + random.gauss(0, 1.2))))

    # BLER rises with load and falls with CQI
    bler_pct = round(max(0.0, (load - 0.75) * 15.0 + (10 - cqi) * 0.5
                         + random.gauss(0, 0.5)), 2)

    power_w = round(max(idle_pw * 0.90,
                        idle_pw + load * (tx_pw - idle_pw)
                        + random.gauss(0, tx_pw * 0.025)), 1)
    if state == "POWER_WASTE":
        power_w = round(tx_pw * random.uniform(0.75, 0.90), 1)

    pkt_loss = round(max(0.0, (load - 0.75) * 2.5 + random.gauss(0, 0.05)), 3)

    lat_base   = 8.0 + load * 25.0 + max(0, 5 - sinr_db) * 2.0
    latency_ms = round(max(1.0, lat_base + random.gauss(0, 2.0)), 1)
    jitter_ms  = round(max(0.1, latency_ms * random.uniform(0.05, 0.15)
                           + random.gauss(0, 0.3)), 2)

    interf_dbm = round(-100.0 + load * 20.0
                       + (random.uniform(15, 30) if state == "SINR_LOW" else 0)
                       + random.gauss(0, 3.0), 1)

    ho_success = round(random.uniform(0.962, 0.9995)
                       - max(0, load - 0.80) * 0.05, 4)

    return {
        "rsrp_dbm":            rsrp_dbm,
        "rsrq_db":             rsrq_db,
        "sinr_db":             sinr_db,
        "cqi":                 cqi,
        "mcs":                 mcs,
        "bler_pct":            bler_pct,
        "prb_dl_pct":          round(prb_dl, 1),
        "prb_ul_pct":          round(prb_ul, 1),
        "dl_throughput_mbps":  dl_tput,
        "ul_throughput_mbps":  ul_tput,
        "connected_ues":       ues,
        "latency_ms":          latency_ms,
        "jitter_ms":           jitter_ms,
        "packet_loss_pct":     pkt_loss,
        "ho_success_rate":     ho_success,
        "interference_dbm":    interf_dbm,
        "power_w":             power_w,
    }


# ── Generator ──────────────────────────────────────────────────────────────────

COLUMNS = [
    "timestamp", "cell_id", "area", "lat", "lon",
    "technology", "band", "freq_mhz", "pci",
    "vendor", "hardware_model",
    "day_of_week", "hour_of_day", "is_weekend",
    "rsrp_dbm", "rsrq_db", "sinr_db", "cqi", "mcs", "bler_pct",
    "prb_dl_pct", "prb_ul_pct", "dl_throughput_mbps", "ul_throughput_mbps",
    "connected_ues", "latency_ms", "jitter_ms",
    "packet_loss_pct", "ho_success_rate", "interference_dbm", "power_w",
    "cell_state",
]


def generate(days: int = 70, seed: int = 42) -> list[dict]:
    random.seed(seed)
    start = datetime(2025, 11, 1)
    rows  = []
    for d in range(days):
        day = start + timedelta(days=d)
        dow = day.weekday()
        for h in range(24):
            ts         = day.replace(hour=h, minute=0, second=0)
            is_weekend = dow >= 5
            base_load  = HOURLY_LOAD[h] * (WEEKEND_FACTOR if is_weekend else 1.0)
            for cell in CELLS:
                state = _pick_state(h, dow, base_load)
                kpis  = _simulate(cell, h, dow, state)
                rows.append({
                    "timestamp":       ts.isoformat(),
                    "cell_id":         cell["cell_id"],
                    "area":            cell["area"],
                    "lat":             cell["lat"],
                    "lon":             cell["lon"],
                    "technology":      cell["generation"],
                    "band":            cell["band"],
                    "freq_mhz":        cell["freq_mhz"],
                    "pci":             cell["pci"],
                    "vendor":          cell["vendor"],
                    "hardware_model":  cell["hardware_model"],
                    "day_of_week":     dow,
                    "hour_of_day":     h,
                    "is_weekend":      int(is_weekend),
                    **kpis,
                    "cell_state":      state,
                })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate Malleswaram 4G/5G network dataset")
    ap.add_argument("--days", type=int, default=70,
                    help="Days to simulate (default: 70 → 50,400 rows)")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed")
    ap.add_argument("--out",  default="generated_dataset.csv", help="Output CSV path")
    args = ap.parse_args()

    n_rows = args.days * len(CELLS) * 24
    print(f"Generating {n_rows:,} rows ({args.days} days × {len(CELLS)} cells × 24 h)…")
    rows = generate(days=args.days, seed=args.seed)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    counts = Counter(r["cell_state"] for r in rows)
    total  = len(rows)
    print(f"\nWrote {total:,} rows → {args.out}")
    print("\nClass distribution:")
    for lbl in CLASS_LABELS:
        n = counts[lbl]
        print(f"  {lbl:<15} {n:>7,}  ({100 * n / total:.1f}%)")
    print("\nColumns:", ", ".join(COLUMNS))


if __name__ == "__main__":
    main()
