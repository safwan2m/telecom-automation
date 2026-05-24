"""
Network slice allocation — assigns PRB budgets and UE capacity per slice per cell.

Slices:
  eMBB  (enhanced Mobile Broadband)  — bulk throughput, best-effort latency
  URLLC (Ultra-Reliable Low Latency) — small payloads, strict latency guarantee
  mMTC  (massive Machine-Type Comms) — low-rate, high-density IoT devices
"""

from __future__ import annotations
# no relative imports needed here

# Minimum guaranteed PRB fraction per slice (even with 0 traffic demand)
MIN_PRB = {"eMBB": 0.10, "URLLC": 0.05, "mMTC": 0.02}

# Typical latency targets (ms) — used to flag if fronthaul budget is tight
LATENCY_TARGET_MS = {"eMBB": 30.0, "URLLC": 1.0, "mMTC": 100.0}


def allocate(
    traffic_profile: dict[str, float],   # {"eMBB": 0.7, "URLLC": 0.2, "mMTC": 0.1}
    max_ues: int,
    latency_constraints: dict,
) -> dict:
    """
    Compute PRB fractions, UE capacity, and latency class per slice for one cell.

    Returns:
      {
        "slices": {
          "eMBB":  {"prb_fraction": 0.68, "max_ues": 210, "latency_target_ms": 30},
          "URLLC": {"prb_fraction": 0.20, "max_ues":  60, "latency_target_ms":  1},
          "mMTC":  {"prb_fraction": 0.10, "max_ues": 120, "latency_target_ms":100},
        },
        "warnings": [...]
      }
    """
    slices   = ["eMBB", "URLLC", "mMTC"]
    total    = sum(traffic_profile.get(s, 0.0) for s in slices)
    warnings = []

    # Normalise traffic profile fractions
    norm = {s: traffic_profile.get(s, 0.0) / max(total, 1e-9) for s in slices}

    # Enforce minimum PRB guarantees
    prb: dict[str, float] = {}
    remaining = 1.0
    for s in slices:
        prb[s] = max(norm[s], MIN_PRB[s])

    # Re-normalise so total = 1.0
    total_prb = sum(prb.values())
    prb = {s: round(v / total_prb, 4) for s, v in prb.items()}

    # UE capacity per slice (proportional to PRB allocation)
    ue_cap = {s: max(1, round(prb[s] * max_ues)) for s in slices}

    # Latency warnings
    e2e_ms = latency_constraints.get("e2e_ms", 10.0)
    if e2e_ms <= LATENCY_TARGET_MS["URLLC"] and prb["URLLC"] < 0.15:
        warnings.append(f"URLLC e2e target {e2e_ms} ms is tight; consider increasing URLLC PRB fraction above 15%")

    result_slices = {}
    for s in slices:
        result_slices[s] = {
            "prb_fraction":      prb[s],
            "max_ues":           ue_cap[s],
            "latency_target_ms": LATENCY_TARGET_MS[s],
        }

    return {"slices": result_slices, "warnings": warnings}


def timing_sync_strategy(latency_constraints: dict, spectrum_bands: list[str]) -> str:
    """
    Choose timing synchronisation strategy based on constraints and spectrum.
    TDD bands (n78, n41) require tighter sync than FDD (n28).
    """
    fronthaul_us = latency_constraints.get("fronthaul_us", 100)
    tdd_bands    = {"n78", "n41", "n257", "n258", "n260", "n261"}
    has_tdd      = any(b in tdd_bands for b in spectrum_bands)

    if fronthaul_us <= 50 or has_tdd:
        return "IEEE-1588-PTP-Class-C"   # ±100 ns accuracy, required for TDD
    elif fronthaul_us <= 200:
        return "IEEE-1588-PTP-Class-B"   # ±1 µs accuracy
    else:
        return "SyncE"                   # ±4.6 ppm, sufficient for FDD
