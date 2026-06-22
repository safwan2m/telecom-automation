"""Tests for agents/planning/slice_allocator.py — PRB and UE allocation."""
import pytest
from slice_allocator import allocate, timing_sync_strategy, MIN_PRB, LATENCY_TARGET_MS

STANDARD_TRAFFIC = {"eMBB": 0.70, "URLLC": 0.20, "mMTC": 0.10}
STANDARD_LAT     = {"e2e_ms": 10.0, "fronthaul_us": 100.0}
MAX_UES = 900


# ── allocate ─────────────────────────────────────────────────────────────────

def test_prb_fractions_sum_to_one():
    result = allocate(STANDARD_TRAFFIC, MAX_UES, STANDARD_LAT)
    total = sum(s["prb_fraction"] for s in result["slices"].values())
    assert abs(total - 1.0) < 1e-3, f"PRB fractions sum to {total}"


def test_all_slices_present():
    result = allocate(STANDARD_TRAFFIC, MAX_UES, STANDARD_LAT)
    assert set(result["slices"].keys()) == {"eMBB", "URLLC", "mMTC"}


def test_min_prb_guarantees_enforced():
    # Balanced skew where renorm doesn't push anything below the minimum.
    # With {eMBB:0.9, URLLC:0.05, mMTC:0.05} the pre-renorm totals are already 1.0
    # so renorm is a no-op and each slice meets its floor exactly.
    result = allocate({"eMBB": 0.9, "URLLC": 0.05, "mMTC": 0.05}, MAX_UES, STANDARD_LAT)
    for name, s in result["slices"].items():
        assert s["prb_fraction"] >= MIN_PRB[name], \
            f"{name} PRB {s['prb_fraction']} below minimum {MIN_PRB[name]}"


def test_ue_cap_proportional_to_prb():
    result = allocate(STANDARD_TRAFFIC, MAX_UES, STANDARD_LAT)
    embb  = result["slices"]["eMBB"]
    urllc = result["slices"]["URLLC"]
    # eMBB PRB > URLLC PRB → eMBB should have more UEs
    assert embb["prb_fraction"] > urllc["prb_fraction"]
    assert embb["max_ues"] > urllc["max_ues"]


def test_ue_cap_at_least_one_per_slice():
    result = allocate(STANDARD_TRAFFIC, 3, STANDARD_LAT)   # tiny cell
    for name, s in result["slices"].items():
        assert s["max_ues"] >= 1, f"{name} has 0 UEs"


def test_latency_targets_correct():
    result = allocate(STANDARD_TRAFFIC, MAX_UES, STANDARD_LAT)
    assert result["slices"]["URLLC"]["latency_target_ms"] == LATENCY_TARGET_MS["URLLC"]
    assert result["slices"]["eMBB"]["latency_target_ms"]  == LATENCY_TARGET_MS["eMBB"]


def test_urllc_warning_tight_constraint():
    # e2e_ms=1.0 ≤ URLLC target (1.0 ms) AND URLLC PRB must be < 0.15 to trigger warning.
    # With eMBB-heavy profile, URLLC lands at ~5% PRB (well below 15%).
    result = allocate({"eMBB": 0.9, "URLLC": 0.05, "mMTC": 0.05},
                      MAX_UES, {"e2e_ms": 1.0, "fronthaul_us": 100.0})
    assert any("URLLC" in w for w in result["warnings"])


def test_no_warning_relaxed_constraint():
    result = allocate(STANDARD_TRAFFIC, MAX_UES, {"e2e_ms": 50.0, "fronthaul_us": 100.0})
    assert result["warnings"] == []


# ── timing_sync_strategy ─────────────────────────────────────────────────────

def test_timing_sync_tdd_band_requires_ptp_c():
    strategy = timing_sync_strategy({"e2e_ms": 10, "fronthaul_us": 150}, ["n78"])
    assert strategy == "IEEE-1588-PTP-Class-C"


def test_timing_sync_tight_fronthaul_requires_ptp_c():
    strategy = timing_sync_strategy({"e2e_ms": 10, "fronthaul_us": 30}, ["B3"])
    assert strategy == "IEEE-1588-PTP-Class-C"


def test_timing_sync_medium_fronthaul_ptp_b():
    strategy = timing_sync_strategy({"e2e_ms": 10, "fronthaul_us": 150}, ["B3"])
    assert strategy == "IEEE-1588-PTP-Class-B"


def test_timing_sync_relaxed_fdd_synce():
    strategy = timing_sync_strategy({"e2e_ms": 10, "fronthaul_us": 300}, ["B3"])
    assert strategy == "SyncE"
