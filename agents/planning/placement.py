"""
Cell placement, DU grouping, and CU grouping algorithms.

Two placement modes:
  Heuristic (default) — density-weighted greedy selection, fast.
  MIP                 — optimal cost-minimising selection via the formulation of
                        Almoghathawi et al. (2024), JER 13:561-567.
                        Requires pulp (pip install pulp).

Propagation models available:
  COST-231-Hata       — used by the DU simulator for coverage radius estimation.
  COST-231-Walfisch-Ikegami (WI) — NLOS urban model used by the MIP placer.
"""

import math
import logging

log = logging.getLogger(__name__)

# Malleswaram candidate macro sites (10 physical tower locations)
CANDIDATE_CELLS: list[dict] = [
    {"cell_id": "MLS_RWS_01", "area": "Malleswaram", "lat": 13.0080, "lon": 77.5760, "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": 1.5},
    {"cell_id": "MLS_18C_01", "area": "Malleswaram", "lat": 13.0030, "lon": 77.5670, "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": 1.4},
    {"cell_id": "MLS_SPG_01", "area": "Malleswaram", "lat": 12.9990, "lon": 77.5700, "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": 1.3},
    {"cell_id": "MLS_BEL_01", "area": "Malleswaram", "lat": 13.0110, "lon": 77.5630, "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": 1.1},
    {"cell_id": "MLS_SNK_01", "area": "Malleswaram", "lat": 13.0060, "lon": 77.5740, "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": 1.2},
    {"cell_id": "MLS_3MN_01", "area": "Malleswaram", "lat": 13.0010, "lon": 77.5600, "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": 1.2},
    {"cell_id": "MLS_MGR_01", "area": "Malleswaram", "lat": 12.9960, "lon": 77.5640, "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": 1.0},
    {"cell_id": "MLS_CHD_01", "area": "Malleswaram", "lat": 12.9930, "lon": 77.5560, "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": 0.9},
    {"cell_id": "MLS_10C_01", "area": "Malleswaram", "lat": 13.0040, "lon": 77.5710, "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": 1.3},
    {"cell_id": "MLS_6CR_01", "area": "Malleswaram", "lat": 12.9970, "lon": 77.5580, "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": 1.0},
]

COST_PER_CELL_USD   = 50_000
COST_PER_DU_USD     = 30_000
COST_PER_CU_USD     = 80_000
FRONTHAUL_RADIUS_KM = 5.0
MIDHAUL_RADIUS_KM   = 25.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R    = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a    = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def select_cells(user_density: float, budget: float, spectrum_bands: list[str]) -> list[dict]:
    """
    Select candidate cells to deploy.
    Prioritises cells whose band is in the licensed spectrum and high-density areas.
    Budget caps the number of cells (simplified cost model).
    """
    COST_PER_SITE = COST_PER_CELL_USD + COST_PER_DU_USD  # rough per-site cost
    max_cells     = min(int(budget * 0.6 / COST_PER_SITE), len(CANDIDATE_CELLS))
    max_cells     = max(max_cells, 1)

    def score(c: dict) -> float:
        band_bonus = 1.5 if c["band"] in spectrum_bands else 0.6
        return c["density_weight"] * band_bonus * (c["max_ues"] / 300.0) * (user_density / 500.0)

    ranked = sorted(CANDIDATE_CELLS, key=score, reverse=True)
    return ranked[:max_cells]


def assign_dus(cells: list[dict], max_cells_per_du: int = 3) -> dict[str, list[str]]:
    """
    Greedy geographic grouping of cells into DUs.
    A cell joins the nearest unfinished DU if within FRONTHAUL_RADIUS_KM.
    Returns {du_id: [cell_id, ...]}.
    """
    assigned: set[str] = set()
    dus: dict[str, list[str]] = {}
    idx = 1

    # Sort by density weight so high-priority cells become DU anchors first
    for anchor in sorted(cells, key=lambda c: c["density_weight"], reverse=True):
        if anchor["cell_id"] in assigned:
            continue

        du_id   = f"DU-BLR-{idx:02d}"
        members = [anchor["cell_id"]]
        assigned.add(anchor["cell_id"])

        for other in cells:
            if other["cell_id"] in assigned or len(members) >= max_cells_per_du:
                continue
            dist = haversine_km(anchor["lat"], anchor["lon"], other["lat"], other["lon"])
            if dist <= FRONTHAUL_RADIUS_KM:
                members.append(other["cell_id"])
                assigned.add(other["cell_id"])

        dus[du_id] = members
        idx += 1

    return dus


def du_centroid(du_id: str, cell_ids: list[str], cell_map: dict[str, dict]) -> tuple[float, float]:
    lats = [cell_map[c]["lat"] for c in cell_ids]
    lons = [cell_map[c]["lon"] for c in cell_ids]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def assign_cus(dus: dict[str, list[str]], cell_map: dict[str, dict], max_dus_per_cu: int = 4) -> dict[str, list[str]]:
    """
    Greedy geographic grouping of DUs into CUs.
    Returns {cu_id: [du_id, ...]}.
    """
    centroids = {du_id: du_centroid(du_id, cell_ids, cell_map) for du_id, cell_ids in dus.items()}
    assigned: set[str] = set()
    cus: dict[str, list[str]] = {}
    idx = 1

    for anchor_du in sorted(dus.keys()):
        if anchor_du in assigned:
            continue
        cu_id   = f"CU-BLR-{idx:02d}"
        members = [anchor_du]
        assigned.add(anchor_du)

        alat, alon = centroids[anchor_du]
        for other_du in sorted(dus.keys()):
            if other_du in assigned or len(members) >= max_dus_per_cu:
                continue
            olat, olon = centroids[other_du]
            if haversine_km(alat, alon, olat, olon) <= MIDHAUL_RADIUS_KM:
                members.append(other_du)
                assigned.add(other_du)

        cus[cu_id] = members
        idx += 1

    return cus


def estimate_cost(n_cells: int, n_dus: int, n_cus: int) -> float:
    return n_cells * COST_PER_CELL_USD + n_dus * COST_PER_DU_USD + n_cus * COST_PER_CU_USD


# ── MIP-backed cell selection ────────────────────────────────────────────────

def select_cells_mip(
    demand_clusters: list[dict] | None = None,
    budget: float = 2_000_000.0,
    spectrum_bands: list[str] | None = None,
    sinr_min_db: float = 10.0,
    time_limit_sec: int = 120,
) -> dict:
    """
    Select candidate cells to deploy using the MIP formulation of
    Almoghathawi et al. (2024).  Returns a result dict from solve_bs_placement_mip.

    demand_clusters: list of DemandCluster objects, or None → use
                     BANGALORE_DEMAND_CLUSTERS (all 10 areas, single period).
    budget:          max installation budget (caps install_cost per site so
                     the solver stays within the envelope).
    spectrum_bands:  restrict candidate sites to these bands; None = all bands.
    sinr_min_db:     minimum SINR threshold enforced at each demand cluster.

    The returned dict includes selected_sites (list of cell_ids) and
    build_schedule so callers can filter CANDIDATE_CELLS accordingly.
    """
    from mip_placer import (
        solve_bs_placement_mip, PropagationParams,
        BANGALORE_DEMAND_CLUSTERS, candidate_sites_from_cells,
    )

    candidates = [
        c for c in CANDIDATE_CELLS
        if spectrum_bands is None or c["band"] in spectrum_bands
    ]
    if not candidates:
        candidates = CANDIDATE_CELLS

    max_sites = min(int(budget * 0.6 / COST_PER_CELL_USD), len(candidates))
    max_sites = max(max_sites, 1)
    install_cost_per_site = budget * 0.6 / max(max_sites, 1)

    cs_list = candidate_sites_from_cells(candidates,
                                         install_cost_usd=install_cost_per_site,
                                         op_cost_usd=1_000.0)

    if demand_clusters is None:
        demand_clusters = BANGALORE_DEMAND_CLUSTERS

    prop = PropagationParams(sinr_min_db=sinr_min_db)
    result = solve_bs_placement_mip(
        demand_by_period=[demand_clusters],
        candidate_sites=cs_list,
        prop=prop,
        mode="permanent",
        time_limit_sec=time_limit_sec,
    )

    if result["status"] != "Optimal":
        log.warning("MIP returned %s; falling back to heuristic.", result["status"])
        fallback = select_cells(500.0, budget, spectrum_bands or ["n78", "n28"])
        result["selected_cells"] = fallback
        result["source"] = "heuristic_fallback"
    else:
        selected_ids = set(result["selected_sites"])
        result["selected_cells"] = [c for c in CANDIDATE_CELLS
                                     if c["cell_id"] in selected_ids]
        result["source"] = "mip"

    return result


def fronthaul_latency_us(cell: dict, du_centroid_pos: tuple[float, float]) -> float:
    """Estimate fronthaul one-way latency in µs based on fiber distance (5 µs/km rule)."""
    dist = haversine_km(cell["lat"], cell["lon"], du_centroid_pos[0], du_centroid_pos[1])
    return round(dist * 5.0 + 10.0, 1)   # 10 µs processing overhead


def midhaul_latency_ms(du_pos: tuple[float, float], cu_pos: tuple[float, float]) -> float:
    """Estimate midhaul one-way latency in ms."""
    dist = haversine_km(du_pos[0], du_pos[1], cu_pos[0], cu_pos[1])
    return round(dist * 0.01 + 0.5, 2)   # 0.01 ms/km + 0.5 ms processing
