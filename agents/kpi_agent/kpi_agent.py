#!/usr/bin/env python3
"""
KPI Monitoring & Optimization Agent  —  AI edition.

Maintains a 6-timestep (60 s) sliding window of KPI readings per cell.
Once the window is full, an LSTM classifier decides the cell's state and
triggers the appropriate action.  While the window fills (first ~60 s),
falls back to simple threshold rules so the agent is never idle.

State classes (see model.py):
  0  NORMAL      — no action
  1  OVERLOAD    — try to move cell to a lighter DU
  2  UNDERLOAD   — flag as sleep candidate (INFO alert)
  3  SINR_LOW    — raise CRITICAL alert
  4  POWER_WASTE — raise WARNING alert
"""

import os
import time
import logging
from collections import deque, defaultdict

import httpx
import torch
import torch.nn.functional as F
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from model import KPIClassifier, SEQ_LEN, LABELS, normalise
from train import train_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
INFLUX_URL     = os.environ.get("INFLUX_URL",       "http://influxdb:8086")
INFLUX_TOKEN   = os.environ.get("INFLUX_TOKEN",     "telecom-super-secret-auth-token-2026")
INFLUX_ORG     = os.environ.get("INFLUX_ORG",       "telecom")
INFLUX_BUCKET  = os.environ.get("INFLUX_BUCKET",    "telecom_metrics")
CONTROLLER_URL = os.environ.get("CONTROLLER_URL",   "http://controller:8080")
POLL_SEC       = int(os.environ.get("POLL_INTERVAL_SEC", "10"))
MODEL_PATH     = os.environ.get("MODEL_PATH",       "kpi_model.pt")

# Fallback rule thresholds (used while the history buffer fills)
OVERLOAD_PRB   = float(os.environ.get("OVERLOAD_PRB_PCT",  "85"))
UNDERLOAD_PRB  = float(os.environ.get("UNDERLOAD_PRB_PCT", "20"))
SINR_MIN_DB    = float(os.environ.get("SINR_MIN_DB",       "5"))
POWER_WASTE_W  = float(os.environ.get("POWER_WASTE_W",     "500"))   # 5G cells idle at ~250W
POWER_WASTE_UE = int(os.environ.get("POWER_WASTE_MIN_UES", "15"))

# Minimum model confidence to act (below this, log but don't act)
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0.70"))

# Anti-thrashing: min seconds between SON actions on the same cell
COOLDOWN_SEC = int(os.environ.get("SON_COOLDOWN_SEC", "300"))
_cell_cooldown: dict[str, float] = {}   # cell_id → last action timestamp

# ── Model bootstrap ───────────────────────────────────────────────────────────

def load_or_train() -> KPIClassifier:
    model = KPIClassifier()
    if os.path.exists(MODEL_PATH):
        model.load_state_dict(torch.load(MODEL_PATH, map_location="cpu", weights_only=True))
        log.info("Loaded model weights from %s", MODEL_PATH)
    else:
        log.info("No saved weights found — training from scratch …")
        model = train_model(MODEL_PATH)
    model.eval()
    return model


# ── InfluxDB helpers ──────────────────────────────────────────────────────────

def connect_influx() -> InfluxDBClient:
    for attempt in range(1, 20):
        try:
            c = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
            c.ping()
            log.info("Connected to InfluxDB.")
            return c
        except Exception as e:
            log.warning("Attempt %d/20 — %s", attempt, e)
            time.sleep(6)
    raise RuntimeError("Cannot connect to InfluxDB.")


def query_latest_cell_kpis(client: InfluxDBClient) -> list[dict]:
    flux = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -3m)
  |> filter(fn: (r) => r._measurement == "cell_kpi")
  |> filter(fn: (r) => r._field == "prb_dl_pct" or r._field == "sinr_db"
                    or r._field == "connected_ues" or r._field == "power_w"
                    or r._field == "packet_loss_pct" or r._field == "dl_throughput_mbps"
                    or r._field == "cqi" or r._field == "bler_pct"
                    or r._field == "latency_ms")
  |> last()
  |> pivot(rowKey: ["cell_id","area","du_id","cu_id"],
           columnKey: ["_field"], valueColumn: "_value")
"""
    try:
        rows = []
        for table in client.query_api().query(flux, org=INFLUX_ORG):
            for rec in table.records:
                v = rec.values
                rows.append({
                    "cell_id":             v.get("cell_id", ""),
                    "area":                v.get("area", ""),
                    "du_id":               v.get("du_id", ""),
                    "cu_id":               v.get("cu_id", ""),
                    "prb_dl_pct":          float(v.get("prb_dl_pct",         0) or 0),
                    "sinr_db":             float(v.get("sinr_db",            20) or 20),
                    "connected_ues":       float(v.get("connected_ues",       0) or 0),
                    "power_w":             float(v.get("power_w",             0) or 0),
                    "packet_loss_pct":     float(v.get("packet_loss_pct",     0) or 0),
                    "dl_throughput_mbps":  float(v.get("dl_throughput_mbps",  0) or 0),
                    "cqi":                 float(v.get("cqi",                10) or 10),
                    "bler_pct":            float(v.get("bler_pct",          1.0) or 1.0),
                    "latency_ms":          float(v.get("latency_ms",        15.0) or 15.0),
                })
        return rows
    except Exception as e:
        log.error("KPI query failed: %s", e)
        return []


def write_alert(write_api, severity: str, cell_id: str, du_id: str,
                alert_type: str, message: str, metric_value: float,
                threshold: float, confidence: float = -1.0):
    p = (Point("alerts")
         .tag("severity",   severity)
         .tag("cell_id",    cell_id)
         .tag("du_id",      du_id)
         .tag("alert_type", alert_type)
         .field("message",      message)
         .field("metric_value", metric_value)
         .field("threshold",    threshold)
         .field("ai_confidence", confidence))
    write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=[p])
    conf_str = f" (conf={confidence*100:.0f}%)" if confidence >= 0 else ""
    log.warning("[%s] %s | %s | %s%s", severity, alert_type, cell_id, message, conf_str)


# ── Controller ────────────────────────────────────────────────────────────────

def move_cell(cell_id: str, to_du_id: str) -> bool:
    try:
        r = httpx.post(f"{CONTROLLER_URL}/move/cell",
                       json={"cell_id": cell_id, "to_du_id": to_du_id}, timeout=5.0)
        if r.status_code == 200:
            log.info("[ACTION] Moved %s → %s", cell_id, to_du_id)
            return True
    except Exception as e:
        log.error("Move cell failed: %s", e)
    return False


# ── Congestion scoring ───────────────────────────────────────────────────────

def congestion_score(c: dict) -> float:
    """Multi-factor congestion score 0–1 (higher = worse).

    Weights: PRB 40% · SINR-inverse 20% · BLER 20% · latency 20%.
    Thresholds: SINR 25 dB = no contribution; BLER 20% = max; latency 150 ms = max.
    """
    prb     = min(c["prb_dl_pct"] / 100.0, 1.0)
    sinr    = max(0.0, 1.0 - c["sinr_db"] / 25.0)
    bler    = min(float(c.get("bler_pct", 1.0)) / 20.0, 1.0)
    latency = min(float(c.get("latency_ms", 15.0)) / 150.0, 1.0)
    return round(0.40 * prb + 0.20 * sinr + 0.20 * bler + 0.20 * latency, 3)


def _is_cooling_down(cell_id: str) -> bool:
    return time.time() - _cell_cooldown.get(cell_id, 0.0) < COOLDOWN_SEC


def _mark_action(cell_id: str) -> None:
    _cell_cooldown[cell_id] = time.time()


# ── Feature extraction ────────────────────────────────────────────────────────

def extract_features(c: dict) -> list[float]:
    return [
        c["prb_dl_pct"],
        c["sinr_db"],
        c["connected_ues"],
        c["power_w"],
        c["packet_loss_pct"],
        c["dl_throughput_mbps"],
        float(c.get("cqi", 10)),          # default 10 if not yet in InfluxDB
        float(c.get("bler_pct", 1.0)),
        float(c.get("latency_ms", 15.0)),
    ]


# ── Inference ─────────────────────────────────────────────────────────────────

def infer(model: KPIClassifier, buf: deque) -> tuple[int, float]:
    """Return (class_idx, confidence) for one cell's history buffer."""
    seq = list(buf)
    x = torch.tensor([normalise(step) for step in seq],
                     dtype=torch.float32).unsqueeze(0)   # (1, SEQ_LEN, N_FEATURES)
    with torch.no_grad():
        logits = model(x)
        probs  = F.softmax(logits, dim=1)[0]
    cls  = probs.argmax().item()
    conf = probs[cls].item()
    return cls, conf


# ── SON helper actions ────────────────────────────────────────────────────────

def _write_son_action(write_api, cell_id: str, du_id: str,
                      action_type: str, message: str, confidence: float) -> None:
    """Record a SON corrective action to InfluxDB for audit + dashboard visibility."""
    try:
        p = (Point("son_actions")
             .tag("cell_id",     cell_id)
             .tag("du_id",       du_id)
             .tag("action_type", action_type)
             .field("message",    message)
             .field("confidence", confidence))
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=[p])
        log.info("[SON] %s → %s: %s", action_type, cell_id, message[:80])
    except Exception as e:
        log.warning("SON action write failed: %s", e)


def _request_pci_reopt(cell_id: str, du_id: str) -> None:
    """Ask the Planning API to validate PCI assignments (non-blocking best-effort)."""
    try:
        r = httpx.post(
            f"{CONTROLLER_URL}/son/pci-reopt",
            json={"cell_id": cell_id, "du_id": du_id},
            timeout=3.0,
        )
        if r.status_code == 200:
            log.info("[SON] PCI re-opt request accepted for %s", cell_id)
    except Exception:
        pass   # best-effort; controller endpoint may not exist yet


# ── Main analysis cycle ───────────────────────────────────────────────────────

def analyse(model: KPIClassifier,
            cells: list[dict],
            buffers: dict,          # cell_id → deque of raw feature lists
            write_api,
            cycle: int) -> None:

    # Fast lookup maps for this cycle
    cell_kpi_map = {c["cell_id"]: c for c in cells}
    du_prb: dict[str, list[float]] = defaultdict(list)
    for c in cells:
        du_prb[c["du_id"]].append(c["prb_dl_pct"])
    du_avg = {du: sum(v) / len(v) for du, v in du_prb.items()}

    overload = underload = sinr_low = pwr_waste = normal = 0

    for c in cells:
        cell_id = c["cell_id"]
        feats   = extract_features(c)
        buffers[cell_id].append(feats)

        has_history = len(buffers[cell_id]) >= SEQ_LEN

        if has_history:
            cls, conf = infer(model, buffers[cell_id])
            label     = LABELS[cls]
            source    = "AI"
        else:
            # ── Fallback: rule-based until buffer fills ───────────────────────
            conf = -1.0
            source = "RULE"
            if c["prb_dl_pct"] > OVERLOAD_PRB:
                cls, label = 1, "OVERLOAD"
            elif c["prb_dl_pct"] < UNDERLOAD_PRB:
                cls, label = 2, "UNDERLOAD"
            elif c["sinr_db"] < SINR_MIN_DB:
                cls, label = 3, "SINR_LOW"
            elif c["power_w"] > POWER_WASTE_W and c["connected_ues"] < POWER_WASTE_UE:
                cls, label = 4, "POWER_WASTE"
            else:
                cls, label = 0, "NORMAL"

        # Only act when confident (or rule-based)
        act = (source == "RULE") or (conf >= MIN_CONFIDENCE)

        if cls == 0:
            normal += 1
            # Proactive: flag cells trending toward congestion before LSTM detects OVERLOAD
            score = congestion_score(c)
            if score > 0.65 and not _is_cooling_down(cell_id):
                _write_son_action(
                    write_api, cell_id, c["du_id"], "PRE_EMPTIVE_STEER",
                    f"[SON] PRE-EMPTIVE {cell_id} score={score:.2f} still NORMAL "
                    f"but trending — PRB {c['prb_dl_pct']:.1f}% "
                    f"BLER {c.get('bler_pct', 1):.1f}% "
                    f"latency {c.get('latency_ms', 15):.0f}ms",
                    conf,
                )
                _mark_action(cell_id)

        elif cls == 1:  # OVERLOAD
            overload += 1
            if act and not _is_cooling_down(cell_id):
                score = congestion_score(c)
                msg = (f"[{source}] OVERLOAD score={score:.2f} — "
                       f"PRB {c['prb_dl_pct']:.1f}% "
                       f"BLER {c.get('bler_pct', 1):.1f}% "
                       f"latency {c.get('latency_ms', 15):.0f}ms")
                write_alert(write_api, "WARNING", cell_id, c["du_id"],
                            "OVERLOAD", msg, c["prb_dl_pct"], OVERLOAD_PRB, conf)

                # 1st choice: steer to least-loaded NEIGHBOR (inexpensive, no topology change)
                neighbor_steered = False
                try:
                    resp = httpx.get(
                        f"{CONTROLLER_URL}/neighbors/{cell_id}", timeout=3.0
                    )
                    neighbors = resp.json().get("neighbors", [])
                    best_nbr, best_prb = None, 999.0
                    for n in neighbors:
                        nid = n.get("cell_id")
                        if nid and nid in cell_kpi_map:
                            nprb = cell_kpi_map[nid]["prb_dl_pct"]
                            if nprb < OVERLOAD_PRB - 25 and nprb < best_prb:
                                best_nbr, best_prb = nid, nprb
                    if best_nbr:
                        _write_son_action(
                            write_api, cell_id, c["du_id"], "NEIGHBOR_LOAD_STEER",
                            f"[SON] OVERLOAD on {cell_id} (PRB {c['prb_dl_pct']:.1f}%): "
                            f"steer excess load to neighbor {best_nbr} "
                            f"(PRB {best_prb:.1f}% — {OVERLOAD_PRB - best_prb:.0f}% headroom)",
                            conf,
                        )
                        neighbor_steered = True
                        _mark_action(cell_id)
                except Exception:
                    pass

                # 2nd choice: DU move — only when score is severe OR no neighbor has headroom
                if not neighbor_steered and score > 0.75:
                    candidate = None
                    for du_id, avg in sorted(du_avg.items(), key=lambda x: x[1]):
                        if du_id != c["du_id"] and avg < OVERLOAD_PRB - 20:
                            candidate = du_id
                            break
                    if candidate:
                        moved  = move_cell(cell_id, candidate)
                        action = f"moved to {candidate}" if moved else "auto-move failed"
                        write_alert(write_api, "INFO", cell_id, c["du_id"],
                                    "LOAD_BALANCE",
                                    f"[{source}] Load-balance: {action}",
                                    c["prb_dl_pct"], OVERLOAD_PRB, conf)
                        _write_son_action(
                            write_api, cell_id, c["du_id"], "LOAD_BALANCE",
                            f"[SON] Severe OVERLOAD score={score:.2f}: {action}",
                            conf,
                        )
                        _mark_action(cell_id)

        elif cls == 2:  # UNDERLOAD
            underload += 1
            if act:
                write_alert(write_api, "INFO", cell_id, c["du_id"],
                            "UNDERLOAD",
                            f"[{source}] Low utilisation — sleep candidate "
                            f"(PRB {c['prb_dl_pct']:.1f}%)",
                            c["prb_dl_pct"], UNDERLOAD_PRB, conf)
                # SON action: find the most-loaded DU and offer traffic steering
                candidate_du = max(du_avg, key=lambda d: du_avg[d])
                _write_son_action(
                    write_api, cell_id, c["du_id"], "TRAFFIC_STEER",
                    f"[SON] UNDERLOAD: recommend steering traffic from {cell_id} "
                    f"(PRB {c['prb_dl_pct']:.1f}%) toward {candidate_du} "
                    f"(PRB {du_avg[candidate_du]:.1f}%) to free compute resources",
                    conf,
                )

        elif cls == 3:  # SINR_LOW
            sinr_low += 1
            if act:
                write_alert(write_api, "CRITICAL", cell_id, c["du_id"],
                            "SINR_DEGRADATION",
                            f"[{source}] SINR {c['sinr_db']:.1f} dB — interference suspected",
                            c["sinr_db"], SINR_MIN_DB, conf)
                # SON action: request PCI re-optimisation via planning API to reduce co-channel interference
                _request_pci_reopt(cell_id, c["du_id"])
                _write_son_action(
                    write_api, cell_id, c["du_id"], "PCI_REOPT_REQUEST",
                    f"[SON] SINR_LOW {c['sinr_db']:.1f} dB: triggered PCI re-optimisation "
                    f"request for {cell_id} to reduce co-channel interference",
                    conf,
                )

        elif cls == 4:  # POWER_WASTE
            pwr_waste += 1
            if act:
                write_alert(write_api, "WARNING", cell_id, c["du_id"],
                            "POWER_WASTE",
                            f"[{source}] {c['power_w']:.0f}W with {int(c['connected_ues'])} UEs",
                            c["power_w"], POWER_WASTE_W, conf)
                # SON action: recommend DTX (Discontinuous Transmission) / sleep mode
                _write_son_action(
                    write_api, cell_id, c["du_id"], "DTX_RECOMMEND",
                    f"[SON] POWER_WASTE: {c['power_w']:.0f}W with only "
                    f"{int(c['connected_ues'])} UEs — recommend DTX/sleep mode "
                    f"(estimated saving: {c['power_w'] * 0.35:.0f}W)",
                    conf,
                )

        buf_fill = len(buffers[cell_id])
        if not has_history and buf_fill < SEQ_LEN:
            log.debug("%-16s history %d/%d (rule-based fallback)", cell_id, buf_fill, SEQ_LEN)

    log.info(
        "Cycle %d | cells=%d | normal=%d | overload=%d | underload=%d "
        "| sinr_low=%d | pwr_waste=%d",
        cycle, len(cells), normal, overload, underload, sinr_low, pwr_waste,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log.info("KPI Agent (AI) starting | poll=%ds | model=%s", POLL_SEC, MODEL_PATH)
    model   = load_or_train()
    client  = connect_influx()
    write_api = client.write_api(write_options=SYNCHRONOUS)

    # per-cell sliding window of raw feature lists  (not yet normalised)
    buffers: dict[str, deque] = defaultdict(lambda: deque(maxlen=SEQ_LEN))

    cycle = 0
    while True:
        cells = query_latest_cell_kpis(client)
        if cells:
            analyse(model, cells, buffers, write_api, cycle)
        else:
            log.info("No cell KPI data yet — waiting for simulators …")
        cycle += 1
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
