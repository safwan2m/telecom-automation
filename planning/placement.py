"""
Cell placement, DU grouping, and CU grouping algorithms.
Uses haversine distance for geographic proximity decisions.
"""

import math
from typing import Any

# Bangalore candidate cell sites with density weights
CANDIDATE_CELLS: list[dict] = [
    {"cell_id": "BLR_KRM_01", "area": "Koramangala",     "lat": 12.9352, "lon": 77.6245, "band": "n78", "freq_mhz": 3500, "max_ues": 300, "density_weight": 1.2},
    {"cell_id": "BLR_KRM_02", "area": "Koramangala",     "lat": 12.9380, "lon": 77.6280, "band": "n78", "freq_mhz": 3500, "max_ues": 250, "density_weight": 1.0},
    {"cell_id": "BLR_IND_01", "area": "Indiranagar",     "lat": 12.9719, "lon": 77.6412, "band": "n78", "freq_mhz": 3500, "max_ues": 280, "density_weight": 1.1},
    {"cell_id": "BLR_IND_02", "area": "Indiranagar",     "lat": 12.9750, "lon": 77.6440, "band": "n41", "freq_mhz": 2500, "max_ues": 200, "density_weight": 0.9},
    {"cell_id": "BLR_HBL_01", "area": "Hebbal",          "lat": 13.0358, "lon": 77.5970, "band": "n78", "freq_mhz": 3500, "max_ues": 260, "density_weight": 0.8},
    {"cell_id": "BLR_MGR_01", "area": "MG Road",         "lat": 12.9757, "lon": 77.6011, "band": "n78", "freq_mhz": 3500, "max_ues": 250, "density_weight": 1.5},
    {"cell_id": "BLR_YPR_01", "area": "Yeshwanthpur",    "lat": 13.0210, "lon": 77.5550, "band": "n78", "freq_mhz": 3500, "max_ues": 300, "density_weight": 0.9},
    {"cell_id": "BLR_WFD_01", "area": "Whitefield",      "lat": 12.9698, "lon": 77.7500, "band": "n78", "freq_mhz": 3500, "max_ues": 350, "density_weight": 1.3},
    {"cell_id": "BLR_WFD_02", "area": "Whitefield",      "lat": 12.9720, "lon": 77.7550, "band": "n78", "freq_mhz": 3500, "max_ues": 300, "density_weight": 1.1},
    {"cell_id": "BLR_ELC_01", "area": "Electronic City", "lat": 12.8399, "lon": 77.6770, "band": "n78", "freq_mhz": 3500, "max_ues": 400, "density_weight": 1.4},
    {"cell_id": "BLR_ELC_02", "area": "Electronic City", "lat": 12.8420, "lon": 77.6800, "band": "n41", "freq_mhz": 2500, "max_ues": 350, "density_weight": 1.2},
    {"cell_id": "BLR_HSR_01", "area": "HSR Layout",      "lat": 12.9116, "lon": 77.6474, "band": "n78", "freq_mhz": 3500, "max_ues": 280, "density_weight": 1.0},
    {"cell_id": "BLR_JYN_01", "area": "Jayanagar",       "lat": 12.9258, "lon": 77.5938, "band": "n28", "freq_mhz":  700, "max_ues": 400, "density_weight": 0.8},
    {"cell_id": "BLR_BNS_01", "area": "Banashankari",    "lat": 12.9255, "lon": 77.5490, "band": "n78", "freq_mhz": 3500, "max_ues": 270, "density_weight": 0.7},
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


def fronthaul_latency_us(cell: dict, du_centroid_pos: tuple[float, float]) -> float:
    """Estimate fronthaul one-way latency in µs based on fiber distance (5 µs/km rule)."""
    dist = haversine_km(cell["lat"], cell["lon"], du_centroid_pos[0], du_centroid_pos[1])
    return round(dist * 5.0 + 10.0, 1)   # 10 µs processing overhead


def midhaul_latency_ms(du_pos: tuple[float, float], cu_pos: tuple[float, float]) -> float:
    """Estimate midhaul one-way latency in ms."""
    dist = haversine_km(du_pos[0], du_pos[1], cu_pos[0], cu_pos[1])
    return round(dist * 0.01 + 0.5, 2)   # 0.01 ms/km + 0.5 ms processing
