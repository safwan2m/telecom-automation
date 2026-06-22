"""Tests for agents/planning/placement.py — pure functions, no external deps."""
import pytest
from placement import (
    haversine_km, coverage_radius_km, circle_overlap_fraction,
    cells_covering_area, select_cells, assign_dus, assign_cus,
    estimate_cost, fronthaul_latency_us, CANDIDATE_CELLS,
    COST_PER_CELL_USD, COST_PER_DU_USD, COST_PER_CU_USD,
)


# ── haversine_km ─────────────────────────────────────────────────────────────

def test_haversine_same_point():
    assert haversine_km(13.0, 77.5, 13.0, 77.5) == 0.0


def test_haversine_known_distance():
    # Bangalore (13.0827, 80.2707) → Chennai (13.0827, 80.2707) ~0 km; use a real pair:
    # Two Malleswaram cells ~1 km apart
    d = haversine_km(13.007, 77.576, 13.007, 77.567)
    assert 0.8 < d < 1.1


def test_haversine_symmetry():
    d1 = haversine_km(13.0, 77.5, 13.01, 77.51)
    d2 = haversine_km(13.01, 77.51, 13.0, 77.5)
    assert abs(d1 - d2) < 1e-9


# ── coverage_radius_km ───────────────────────────────────────────────────────

def test_coverage_radius_increases_with_power():
    r_low  = coverage_radius_km(tx_power_w=100, freq_mhz=1800)
    r_high = coverage_radius_km(tx_power_w=1000, freq_mhz=1800)
    assert r_high > r_low


def test_coverage_radius_decreases_with_frequency():
    r_low_f  = coverage_radius_km(tx_power_w=500, freq_mhz=700)
    r_high_f = coverage_radius_km(tx_power_w=500, freq_mhz=3500)
    assert r_low_f > r_high_f


def test_coverage_radius_positive():
    r = coverage_radius_km(tx_power_w=1000, freq_mhz=3500)
    assert r > 0


# ── circle_overlap_fraction ──────────────────────────────────────────────────

def test_overlap_non_overlapping():
    # Cell 10 km away, both radii < 3 km → no overlap
    assert circle_overlap_fraction(d=10.0, r_area=0.3, r_cell=1.0) == 0.0


def test_overlap_area_fully_inside_cell():
    # Area fully inside cell coverage → fraction = 1.0
    assert circle_overlap_fraction(d=0.0, r_area=0.3, r_cell=2.0) == 1.0


def test_overlap_cell_fully_inside_area():
    # Cell fully inside area → fraction = (r_cell/r_area)^2
    frac = circle_overlap_fraction(d=0.0, r_area=2.0, r_cell=1.0)
    assert abs(frac - (1.0 / 4.0)) < 0.01


def test_overlap_partial_in_range():
    frac = circle_overlap_fraction(d=0.5, r_area=0.3, r_cell=0.6)
    assert 0.0 < frac < 1.0


# ── cells_covering_area ──────────────────────────────────────────────────────

def _make_cell(cell_id, lat, lon, tx_power_w=1000, freq_mhz=3500):
    return {"cell_id": cell_id, "lat": lat, "lon": lon,
            "tx_power_w": tx_power_w, "freq_mhz": freq_mhz}


def test_cells_covering_area_returns_nearby():
    area = {"area_id": "TEST", "lat": 13.007, "lon": 77.576, "radius_km": 0.3}
    cells = [
        _make_cell("NEAR", 13.007, 77.576),   # co-located → definitely covers
        _make_cell("FAR",  13.100, 77.700),   # ~14 km away → cannot cover
    ]
    result = cells_covering_area(area, cells)
    ids = [c["cell_id"] for c in result]
    assert "NEAR" in ids
    assert "FAR" not in ids


def test_cells_covering_area_enriches_result():
    area = {"area_id": "TEST", "lat": 13.007, "lon": 77.576, "radius_km": 0.3}
    cells = [_make_cell("C1", 13.007, 77.576)]
    result = cells_covering_area(area, cells)
    assert result
    assert "coverage_radius_km" in result[0]
    assert "distance_to_area_km" in result[0]
    assert "area_coverage_fraction" in result[0]


# ── select_cells ─────────────────────────────────────────────────────────────

def test_select_cells_budget_cap():
    # Tiny budget → only 1 cell selected
    selected = select_cells(
        user_density=1000, budget=100_000,
        spectrum_bands=["n78"], candidate_pool=CANDIDATE_CELLS,
    )
    assert len(selected) >= 1
    cost_per_site = COST_PER_CELL_USD + COST_PER_DU_USD
    max_affordable = int(100_000 * 0.6 / cost_per_site)
    assert len(selected) <= max(max_affordable, 1)


def test_select_cells_prefers_high_density():
    selected = select_cells(
        user_density=2000, budget=5_000_000,
        spectrum_bands=["n78"], candidate_pool=CANDIDATE_CELLS,
    )
    assert selected
    # Top-ranked cell should have the highest density_weight in the candidate pool
    top_dw = selected[0]["density_weight"]
    assert top_dw == max(c["density_weight"] for c in CANDIDATE_CELLS)


def test_select_cells_band_bonus():
    # Requesting n78 — all candidates already are n78, so all should score high
    selected = select_cells(
        user_density=1000, budget=10_000_000, spectrum_bands=["n78"]
    )
    assert len(selected) == len(CANDIDATE_CELLS)


# ── assign_dus ───────────────────────────────────────────────────────────────

def test_assign_dus_all_cells_covered():
    selected = CANDIDATE_CELLS[:6]
    dus = assign_dus(selected, max_cells_per_du=3)
    assigned = [cid for cids in dus.values() for cid in cids]
    assert set(assigned) == {c["cell_id"] for c in selected}


def test_assign_dus_respects_max_cells():
    max_per = 2
    dus = assign_dus(CANDIDATE_CELLS, max_cells_per_du=max_per)
    for du_id, cell_ids in dus.items():
        assert len(cell_ids) <= max_per, f"{du_id} has {len(cell_ids)} cells"


def test_assign_dus_no_duplicate_cells():
    dus = assign_dus(CANDIDATE_CELLS, max_cells_per_du=3)
    all_cells = [cid for cids in dus.values() for cid in cids]
    assert len(all_cells) == len(set(all_cells))


# ── assign_cus ───────────────────────────────────────────────────────────────

def test_assign_cus_all_dus_covered():
    cell_map = {c["cell_id"]: c for c in CANDIDATE_CELLS}
    dus = assign_dus(CANDIDATE_CELLS, max_cells_per_du=3)
    cus = assign_cus(dus, cell_map, max_dus_per_cu=4)
    assigned_dus = [did for dids in cus.values() for did in dids]
    assert set(assigned_dus) == set(dus.keys())


# ── estimate_cost ─────────────────────────────────────────────────────────────

def test_estimate_cost_arithmetic():
    cost = estimate_cost(n_cells=5, n_dus=2, n_cus=1)
    expected = 5 * COST_PER_CELL_USD + 2 * COST_PER_DU_USD + 1 * COST_PER_CU_USD
    assert cost == expected


def test_estimate_cost_zero():
    assert estimate_cost(0, 0, 0) == 0.0


# ── fronthaul_latency_us ─────────────────────────────────────────────────────

def test_fronthaul_latency_co_located():
    cell = {"lat": 13.007, "lon": 77.576}
    du_pos = (13.007, 77.576)
    lat = fronthaul_latency_us(cell, du_pos)
    # Zero distance → only processing overhead (10 µs)
    assert abs(lat - 10.0) < 0.5


def test_fronthaul_latency_increases_with_distance():
    cell = {"lat": 13.007, "lon": 77.576}
    near_du = (13.007, 77.577)
    far_du  = (13.007, 77.590)
    assert fronthaul_latency_us(cell, near_du) < fronthaul_latency_us(cell, far_du)
