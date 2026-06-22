"""
Cell placement, DU grouping, and CU grouping algorithms.

Placement: density-weighted greedy heuristic with proximity bonus for named areas.
Propagation: COST-231-Hata urban macro model for coverage radius estimation.
"""

import math
import logging

log = logging.getLogger(__name__)

# Malleswaram candidate macro sites (10 physical tower locations).
# Hardware fields are propagated through plan_to_topology() so DU simulators
# receive correct specs after a plan is applied.
_VENDOR_CYCLE = ["Nokia", "Ericsson", "Samsung", "ZTE", "Nokia", "Ericsson", "Samsung", "ZTE", "Nokia", "Ericsson"]
_HW_5G = {
    "Nokia":    {"hardware_model": "AirScale MAA 64T64R", "antenna_config": "64T64R", "tx_power_w": 1000, "idle_power_w": 250, "peak_dl_mbps": 3800},
    "Ericsson": {"hardware_model": "AIR 6449",            "antenna_config": "64T64R", "tx_power_w": 950,  "idle_power_w": 240, "peak_dl_mbps": 3600},
    "Samsung":  {"hardware_model": "TM500 64T64R",        "antenna_config": "64T64R", "tx_power_w": 900,  "idle_power_w": 225, "peak_dl_mbps": 3400},
    "ZTE":      {"hardware_model": "AAU 5614",             "antenna_config": "64T64R", "tx_power_w": 1000, "idle_power_w": 250, "peak_dl_mbps": 3200},
}

def _cand(i, cell_id, area, lat, lon, dw):
    vendor = _VENDOR_CYCLE[i]
    hw     = _HW_5G[vendor]
    return {
        "cell_id": cell_id, "area": area, "lat": lat, "lon": lon,
        "band": "n78", "freq_mhz": 3500, "max_ues": 900, "density_weight": dw,
        "generation": "5G", "vendor": vendor, **hw,
    }

CANDIDATE_CELLS: list[dict] = [
    _cand(0, "MLS_RWS_01", "Malleswaram Railway Station", 13.0080, 77.5760, 1.5),
    _cand(1, "MLS_18C_01", "Malleswaram 18th Cross",      13.0030, 77.5670, 1.4),
    _cand(2, "MLS_SPG_01", "Sampige Road South",          12.9990, 77.5700, 1.3),
    _cand(3, "MLS_BEL_01", "BEL Road",                    13.0110, 77.5630, 1.1),
    _cand(4, "MLS_SNK_01", "Shankar Mutt Road",           13.0060, 77.5740, 1.2),
    _cand(5, "MLS_3MN_01", "3rd Main Road",               13.0010, 77.5600, 1.2),
    _cand(6, "MLS_MGR_01", "Margosa Road",                12.9960, 77.5640, 1.0),
    _cand(7, "MLS_CHD_01", "Chowdaiah Road",              12.9930, 77.5560, 0.9),
    _cand(8, "MLS_10C_01", "10th Cross",                  13.0040, 77.5710, 1.3),
    _cand(9, "MLS_6CR_01", "6th Cross Road",              12.9970, 77.5580, 1.0),
]

COST_PER_CELL_USD   = 50_000
COST_PER_DU_USD     = 30_000
COST_PER_CU_USD     = 80_000
FRONTHAUL_RADIUS_KM = 5.0
MIDHAUL_RADIUS_KM   = 25.0

# Named geographic zones in Malleswaram — independent of any deployment.
# An area may have 0, 1, or several cells covering it.
MALLESWARAM_AREAS: list[dict] = [
    {"area_id": "MLS-RWS", "name": "Malleswaram Railway Station", "lat": 13.0127, "lon": 77.5707, "radius_km": 0.40},
    {"area_id": "MLS-KMT", "name": "Kadu Malleshwara Temple",     "lat": 13.0097, "lon": 77.5718, "radius_km": 0.30},
    {"area_id": "MLS-BEL", "name": "BEL Road",                    "lat": 13.0110, "lon": 77.5632, "radius_km": 0.35},
    {"area_id": "MLS-18C", "name": "Malleswaram 18th Cross",      "lat": 13.0080, "lon": 77.5663, "radius_km": 0.30},
    {"area_id": "MLS-SNK", "name": "Shankar Mutt Road",           "lat": 13.0062, "lon": 77.5742, "radius_km": 0.30},
    {"area_id": "MLS-MGR", "name": "Margosa Road Central",        "lat": 13.0055, "lon": 77.5692, "radius_km": 0.30},
    {"area_id": "MLS-10C", "name": "10th Cross",                  "lat": 13.0040, "lon": 77.5707, "radius_km": 0.30},
    {"area_id": "MLS-CIR", "name": "Malleswaram Circle",          "lat": 13.0022, "lon": 77.5718, "radius_km": 0.30},
    {"area_id": "MLS-SPG", "name": "Sampige Road South",          "lat": 13.0025, "lon": 77.5660, "radius_km": 0.30},
    {"area_id": "MLS-3MN", "name": "3rd Main Road",               "lat": 13.0012, "lon": 77.5598, "radius_km": 0.30},
    {"area_id": "MLS-6CR", "name": "6th Cross Road",              "lat": 12.9968, "lon": 77.5638, "radius_km": 0.30},
    {"area_id": "MLS-CHD", "name": "Chowdaiah Road",              "lat": 12.9932, "lon": 77.5562, "radius_km": 0.35},
]

MIN_AREA_COVERAGE_FRACTION = 0.20


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R    = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a    = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def coverage_radius_km(
    tx_power_w:       float,
    freq_mhz:         float,
    tx_gain_dbi:      float = 18.0,
    rx_threshold_dbm: float = -100.0,
    h_tx_m:           float = 25.0,
    h_rx_m:           float = 1.5,
) -> float:
    """
    Estimate cell coverage radius (km) using COST-231-Hata urban macro model.
    Solves for the distance d where path loss equals the available link budget.
    """
    tx_power_dbm  = 10 * math.log10(max(tx_power_w, 1e-9) * 1000)
    max_path_loss = tx_power_dbm + tx_gain_dbi - rx_threshold_dbm

    # a(h_rx) for urban large city, f > 300 MHz
    a_hrx = 3.2 * (math.log10(11.75 * h_rx_m)) ** 2 - 4.97
    C_m   = 3.0   # metropolitan correction

    A = 46.3 + 33.9 * math.log10(freq_mhz) - 13.82 * math.log10(h_tx_m) - a_hrx + C_m
    B = 44.9 - 6.55 * math.log10(h_tx_m)

    if B <= 0:
        return 0.1
    log_d = (max_path_loss - A) / B
    return round(max(10 ** log_d, 0.05), 3)


def circle_overlap_fraction(d: float, r_area: float, r_cell: float) -> float:
    """
    Fraction of the area circle (radius r_area) covered by the cell circle (radius r_cell).
    d = distance between their centres (km). Returns value in [0, 1].
    """
    if d >= r_cell + r_area:
        return 0.0
    if d + r_area <= r_cell:
        return 1.0                               # area fully inside cell coverage
    if d + r_cell <= r_area:
        return (r_cell * r_cell) / (r_area * r_area)   # cell fully inside area

    r1, r2 = r_area, r_cell
    cos_a1 = (d*d + r1*r1 - r2*r2) / (2 * d * r1)
    cos_a2 = (d*d + r2*r2 - r1*r1) / (2 * d * r2)
    a1 = math.acos(max(-1.0, min(1.0, cos_a1)))
    a2 = math.acos(max(-1.0, min(1.0, cos_a2)))

    s           = max(0.0, (-d+r1+r2) * (d+r1-r2) * (d-r1+r2) * (d+r1+r2))
    intersection = r1*r1*a1 + r2*r2*a2 - 0.5 * math.sqrt(s)
    return min(1.0, max(0.0, intersection / (math.pi * r1 * r1)))


def cells_covering_area(
    area:       dict,
    live_cells: list[dict],
    min_fraction: float = MIN_AREA_COVERAGE_FRACTION,
) -> list[dict]:
    """
    Return live cells that cover at least min_fraction (default 20 %) of the area circle.
    Each result entry is the original cell dict enriched with:
      coverage_radius_km, distance_to_area_km, area_coverage_fraction.
    """
    results = []
    for cell in live_cells:
        r_cell   = coverage_radius_km(
            tx_power_w  = cell.get("tx_power_w", 100),
            freq_mhz    = cell.get("freq_mhz", 1800),
        )
        dist     = haversine_km(cell["lat"], cell["lon"], area["lat"], area["lon"])
        fraction = circle_overlap_fraction(dist, area.get("radius_km", 0.3), r_cell)
        if fraction >= min_fraction:
            results.append({
                **cell,
                "coverage_radius_km":    round(r_cell, 3),
                "distance_to_area_km":   round(dist, 3),
                "area_coverage_fraction": round(fraction, 3),
            })
    return results


def select_cells(
    user_density:   float,
    budget:         float,
    spectrum_bands: list[str],
    candidate_pool: list[dict] | None = None,
    area_center:    tuple[float, float] | None = None,
) -> list[dict]:
    """
    Select candidate cells to deploy.
    candidate_pool: pre-filtered subset of CANDIDATE_CELLS (e.g. proximity-filtered);
                    defaults to all CANDIDATE_CELLS.
    area_center:    (lat, lon) of the target area; adds a proximity bonus to scoring
                    so nearer candidates rank higher when multiple sites are viable.
    Budget caps the number of cells selected.
    """
    pool          = candidate_pool if candidate_pool is not None else CANDIDATE_CELLS
    COST_PER_SITE = COST_PER_CELL_USD + COST_PER_DU_USD
    max_cells     = min(int(budget * 0.6 / COST_PER_SITE), len(pool))
    max_cells     = max(max_cells, 1)

    def score(c: dict) -> float:
        band_bonus    = 1.5 if c["band"] in spectrum_bands else 0.6
        density_score = c["density_weight"] * band_bonus * (c["max_ues"] / 300.0) * (user_density / 500.0)
        if area_center:
            dist = haversine_km(c["lat"], c["lon"], area_center[0], area_center[1])
            density_score *= (1.0 + 1.0 / (1.0 + dist))   # proximity bonus
        return density_score

    ranked = sorted(pool, key=score, reverse=True)
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
