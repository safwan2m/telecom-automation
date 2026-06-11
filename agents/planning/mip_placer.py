"""
MIP-based base station placement.

Implements the model from:
  Almoghathawi et al. (2024), J. Eng. Research 13:561-567
  "Optimal location of base stations for cellular mobile network
   considering changes in users locations"

Two demand modes (Section 1 of paper):
  PERMANENT — Case A: each period adds new demand clusters (expanding rollout)
  TEMPORARY — Case B: demand clusters shift each period (events / diurnal load)

MIP formulation minimises:
  sum_j sum_t  (c_jt * z_jt  +  r_jt * y_jt)

Subject to (paper constraint numbers preserved):
  (2)  Each candidate site built at most once across all periods
  (3)  Each demand cluster served by at least one built BS
  (4)  Site active only if previously built
  (5)  Each demand cluster assigned to exactly one BS per period
  (6)  Assignment implies site is active
  (7)  BS channel capacity not exceeded
  (8)  SINR quality-of-service at each demand cluster (linearised)
"""

from __future__ import annotations
import math
import logging
from dataclasses import dataclass, field
from typing import Literal

try:
    import pulp
    HAS_PULP = True
except ImportError:
    HAS_PULP = False

log = logging.getLogger(__name__)


# ── COST-231 Walfisch-Ikegami NLOS propagation model (Section 3 of paper) ───

def cost231_wi_path_loss_db(
    d_m:          float,
    freq_mhz:     float,
    h_tx_m:       float = 25.0,
    h_rx_m:       float = 2.0,
    h_bld_m:      float = 10.0,
    b_sep_m:      float = 50.0,
    w_street_m:   float = 25.0,
    phi_deg:      float = 30.0,
    metropolitan: bool  = True,
) -> float:
    """
    COST-231 Walfisch-Ikegami NLOS path loss (dB).
    Valid for 800-2000 MHz; extended here to cover 700-3500 MHz bands.
    Parameters from Table 4 of Almoghathawi et al. (2024).
    """
    d_m  = max(d_m, 1.0)
    d_km = d_m / 1000.0
    f    = freq_mhz

    # (a) Free-space path loss
    L_fs = 32.4 + 20 * math.log10(d_km) + 20 * math.log10(f)

    # (b) Rooftop-to-street diffraction and scatter loss
    delta_h_mobile = max(h_bld_m - h_rx_m, 0.1)
    phi = abs(phi_deg) % 90
    if phi < 35:
        L_ori = -10.0 + 0.354 * phi
    elif phi < 55:
        L_ori = 2.5 + 0.075 * (phi - 35)
    else:
        L_ori = 4.0 - 0.114 * (phi - 55)

    L_rts = (-16.9
             - 10 * math.log10(w_street_m)
             + 10 * math.log10(f)
             + 20 * math.log10(delta_h_mobile)
             + L_ori)

    # (c) Multi-screen diffraction loss
    delta_h_bs = h_tx_m - h_bld_m
    if delta_h_bs > 0:
        L_bsh = -18 * math.log10(1 + delta_h_bs)
        k_a   = 54.0
        k_d   = 18.0
    else:
        L_bsh = 0.0
        k_a   = 54.0 - 0.8 * delta_h_bs if d_km >= 0.5 else 54.0 - 1.6 * delta_h_bs * d_km
        k_d   = 18.0 - 15.0 * delta_h_bs / max(h_bld_m, 1.0)

    k_f = (-4.0 + 1.5 * (f / 925.0 - 1) if metropolitan
           else -4.0 + 0.7 * (f / 925.0 - 1))

    L_msd = (L_bsh + k_a
             + k_d * math.log10(d_km)
             + k_f * math.log10(f)
             - 9.0 * math.log10(b_sep_m))

    # Total: L_fs + diffraction terms (if positive)
    return L_fs + max(0.0, L_rts + L_msd)


def received_power_dbm(
    tx_power_dbm: float,
    path_loss_db: float,
    tx_gain_dbi:  float = 18.0,
    rx_gain_dbi:  float = 0.0,
) -> float:
    return tx_power_dbm + tx_gain_dbi + rx_gain_dbi - path_loss_db


# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class DemandCluster:
    """
    Demand node concept (Tutschku 1998, Mathar & Niessen 2000) —
    represents a cluster of uniformly distributed users in a geographic area.
    """
    cluster_id:  str
    lat:         float
    lon:         float
    n_channels:  int       # simultaneous channel demand (ρ_i in paper)
    area:        str = ""


@dataclass
class CandidateSite:
    """Candidate BS location with RF and cost parameters."""
    site_id:       str
    lat:           float
    lon:           float
    max_channels:  int     # BS capacity δ_j
    install_cost:  float   # c_jt one-time CAPEX (USD)
    op_cost:       float   # r_jt per-period OPEX (USD)
    tx_power_dbm:  float = 43.0   # 20 W effective per sector
    tx_gain_dbi:   float = 18.0   # 64T64R massive MIMO beamforming gain
    rx_gain_dbi:   float = 0.0
    freq_mhz:      float = 1800.0


@dataclass
class PropagationParams:
    """COST-231 WI model and link-budget parameters (Table 4-5 of paper)."""
    h_tx_m:          float = 25.0
    h_rx_m:          float = 2.0
    h_bld_m:         float = 10.0   # typical Bangalore low-rise building
    b_sep_m:         float = 50.0
    w_street_m:      float = 25.0
    phi_deg:         float = 30.0
    metropolitan:    bool  = True
    min_rx_power_dbm: float = -100.0   # γ_i: minimum power requirement (dBm)
    sinr_min_db:     float = 10.0      # minimum SINR threshold (dB)
    noise_power_dbm: float = -120.0    # thermal noise floor (dBm)


# ── Malleswaram pre-defined demand clusters ──────────────────────────────────
# 3 clusters aligned with DU service zones; n_channels ≈ Erlangs per cluster

BANGALORE_DEMAND_CLUSTERS: list[DemandCluster] = [
    DemandCluster("DC-MLS-N", 13.0070, 77.5700, 160, "Malleswaram North"),   # RWS, BEL, SNK, 18C
    DemandCluster("DC-MLS-C", 13.0015, 77.5650, 150, "Malleswaram Central"), # SPG, 3MN, 10C
    DemandCluster("DC-MLS-S", 12.9955, 77.5595, 150, "Malleswaram South"),   # MGR, CHD, 6CR
]

# Multi-period demand profiles for Case A (permanent — phased rollout across Malleswaram)
DEMAND_PERIODS_CASE_A: list[list[str]] = [
    ["DC-MLS-C"],                           # Period 1: central commercial core
    ["DC-MLS-N", "DC-MLS-S"],              # Period 2: expand to north and south
]

# Multi-period demand profiles for Case B (temporary — diurnal shift in Malleswaram)
DEMAND_PERIODS_CASE_B: list[list[str]] = [
    ["DC-MLS-N"],                           # Period 1: morning peak at railway station / transit hub
    ["DC-MLS-C", "DC-MLS-S"],              # Period 2: daytime commercial and residential load
    ["DC-MLS-N", "DC-MLS-C", "DC-MLS-S"], # Period 3: evening — full area at capacity
]


def candidate_sites_from_cells(
    cells: list[dict],
    install_cost_usd: float = 50_000.0,
    op_cost_usd:      float = 1_000.0,
) -> list[CandidateSite]:
    """Convert CANDIDATE_CELLS dicts to CandidateSite objects."""
    band_tx: dict[str, tuple[float, float]] = {
        # (tx_power_dbm, tx_gain_dbi)
        "n78": (43.0, 18.0),   # 3500 MHz 64T64R massive MIMO
        "n41": (43.0, 18.0),   # 2500 MHz 64T64R
        "n28": (43.0, 18.0),   # 700 MHz 64T64R (wider coverage)
        "B3":  (40.0, 15.0),   # 1800 MHz 4G
        "B40": (40.0, 15.0),   # 2300 MHz 4G
    }
    sites = []
    for c in cells:
        tx_dbm, tx_gain = band_tx.get(c.get("band", "n78"), (43.0, 18.0))
        sites.append(CandidateSite(
            site_id      = c["cell_id"],
            lat          = c["lat"],
            lon          = c["lon"],
            max_channels = c.get("max_ues", 300),
            install_cost = install_cost_usd,
            op_cost      = op_cost_usd,
            tx_power_dbm = tx_dbm,
            tx_gain_dbi  = tx_gain,
            freq_mhz     = float(c.get("freq_mhz", 1800)),
        ))
    return sites


# ── Link-budget and feasibility ──────────────────────────────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R    = 6_371_000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a    = (math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
            * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def compute_link_powers(
    demand_clusters: list[DemandCluster],
    candidate_sites: list[CandidateSite],
    prop:            PropagationParams,
) -> dict[tuple[str, str], float]:
    """Returns {(cluster_id, site_id): received_power_dbm} for all (i, j)."""
    powers: dict[tuple[str, str], float] = {}
    for dc in demand_clusters:
        for cs in candidate_sites:
            d_m = _haversine_m(dc.lat, dc.lon, cs.lat, cs.lon)
            pl  = cost231_wi_path_loss_db(
                d_m, cs.freq_mhz,
                prop.h_tx_m, prop.h_rx_m,
                prop.h_bld_m, prop.b_sep_m,
                prop.w_street_m, prop.phi_deg,
                prop.metropolitan,
            )
            powers[(dc.cluster_id, cs.site_id)] = received_power_dbm(
                cs.tx_power_dbm, pl, cs.tx_gain_dbi, cs.rx_gain_dbi
            )
    return powers


def build_feasible_sets(
    demand_clusters: list[DemandCluster],
    candidate_sites: list[CandidateSite],
    powers:          dict[tuple[str, str], float],
    min_rx_dbm:      float,
) -> dict[str, list[str]]:
    """
    S(i) from paper Table 1:
      {j ∈ S : received power at cluster i from site j ≥ γ_i}
    """
    site_ids = {cs.site_id for cs in candidate_sites}
    return {
        dc.cluster_id: [
            j for j in site_ids
            if powers.get((dc.cluster_id, j), -999.0) >= min_rx_dbm
        ]
        for dc in demand_clusters
    }


# ── MIP solver ────────────────────────────────────────────────────────────────

def solve_bs_placement_mip(
    demand_by_period: list[list[DemandCluster]],
    candidate_sites:  list[CandidateSite],
    prop:             PropagationParams | None = None,
    mode:             Literal["permanent", "temporary"] = "permanent",
    time_limit_sec:   int = 120,
) -> dict:
    """
    Solve multi-period BS placement MIP (Almoghathawi et al., 2024).

    demand_by_period[t] is the list of active demand clusters at period t.
      mode="permanent": demand_by_period[t] = ONLY the NEW clusters in period t;
                        the MIP accumulates coverage across all periods (Case A).
      mode="temporary": demand_by_period[t] = ALL clusters active in period t;
                        different sets each period (Case B).

    Returns
    -------
    dict with keys:
      selected_sites   list[str]   site_ids of all built BSs
      build_schedule   dict        {site_id: period (1-indexed)}
      assignments      dict        {"(cluster_id, site_id, period)": 1}
      total_cost       float
      install_cost     float
      op_cost          float
      status           str         "Optimal" | "Infeasible" | ...
      solver_msg       str
      n_periods        int
      mode             str
      feasibility      dict        {cluster_id: n_feasible_sites}
    """
    if not HAS_PULP:
        raise ImportError("pulp is required for MIP-based placement — add it to requirements.txt")
    if prop is None:
        prop = PropagationParams()

    T  = len(demand_by_period)
    S  = {cs.site_id: cs for cs in candidate_sites}
    si = list(S.keys())

    # All unique demand clusters referenced across periods
    all_clusters: dict[str, DemandCluster] = {}
    for period_clusters in demand_by_period:
        for dc in period_clusters:
            all_clusters[dc.cluster_id] = dc

    powers   = compute_link_powers(list(all_clusters.values()), candidate_sites, prop)
    feasible = build_feasible_sets(list(all_clusters.values()), candidate_sites,
                                   powers, prop.min_rx_power_dbm)

    # Warn about infeasible clusters (no site can reach them)
    for cid, fj in feasible.items():
        if not fj:
            log.warning("Demand cluster %s has 0 feasible candidate sites — "
                        "coverage will not be achievable", cid)

    sinr_linear = 10 ** (prop.sinr_min_db / 10.0)
    noise_mw    = 10 ** (prop.noise_power_dbm / 10.0)
    powers_mw   = {k: 10 ** (v / 10.0) for k, v in powers.items()}

    # ── Decision variables ────────────────────────────────────────────────────
    prob = pulp.LpProblem("BS_Placement", pulp.LpMinimize)

    # x_ijt — demand cluster i assigned to site j at period t (binary)
    x: dict[tuple[str, str, int], pulp.LpVariable] = {}
    for t, pcs in enumerate(demand_by_period):
        for dc in pcs:
            for j in feasible.get(dc.cluster_id, []):
                x[(dc.cluster_id, j, t)] = pulp.LpVariable(
                    f"x_{dc.cluster_id}_{j}_t{t}", cat="Binary"
                )

    # y_jt — site j active (in use) at period t (binary)
    y = {(j, t): pulp.LpVariable(f"y_{j}_t{t}", cat="Binary")
         for j in si for t in range(T)}

    # z_jt — site j constructed at period t (binary)
    z = {(j, t): pulp.LpVariable(f"z_{j}_t{t}", cat="Binary")
         for j in si for t in range(T)}

    # ── Objective (eq. 1) ─────────────────────────────────────────────────────
    prob += (
        pulp.lpSum(S[j].install_cost * z[(j, t)] for j in si for t in range(T))
        + pulp.lpSum(S[j].op_cost    * y[(j, t)] for j in si for t in range(T)),
        "TotalCost",
    )

    # ── Constraint (2): each site built at most once ──────────────────────────
    for j in si:
        prob += (pulp.lpSum(z[(j, t)] for t in range(T)) <= 1,
                 f"build_once_{j}")

    # ── Constraint (3): every demand cluster served ───────────────────────────
    for t, pcs in enumerate(demand_by_period):
        for dc in pcs:
            fj = feasible.get(dc.cluster_id, [])
            if not fj:
                continue
            # "served by at least one BS installed at this period or earlier"
            prob += (
                pulp.lpSum(z[(j, l)] for j in fj for l in range(t + 1)) >= 1,
                f"coverage_{dc.cluster_id}_t{t}",
            )

    # ── Constraint (4): site active only if previously built ──────────────────
    for j in si:
        for t in range(T):
            prob += (
                pulp.lpSum(z[(j, l)] for l in range(t + 1)) >= y[(j, t)],
                f"activation_{j}_t{t}",
            )

    # ── Constraint (5): each cluster assigned to exactly one BS per period ────
    for t, pcs in enumerate(demand_by_period):
        for dc in pcs:
            fj = feasible.get(dc.cluster_id, [])
            xvars = [x[(dc.cluster_id, j, t)] for j in fj
                     if (dc.cluster_id, j, t) in x]
            if xvars:
                prob += (pulp.lpSum(xvars) == 1,
                         f"assign_{dc.cluster_id}_t{t}")

    # ── Constraint (6): assignment implies site active ────────────────────────
    for t, pcs in enumerate(demand_by_period):
        for j in si:
            dc_at_j = [dc for dc in pcs
                       if j in feasible.get(dc.cluster_id, [])
                       and (dc.cluster_id, j, t) in x]
            if dc_at_j:
                prob += (
                    pulp.lpSum(x[(dc.cluster_id, j, t)] for dc in dc_at_j)
                    <= len(dc_at_j) * y[(j, t)],
                    f"implies_active_{j}_t{t}",
                )

    # ── Constraint (7): BS channel capacity ──────────────────────────────────
    for t, pcs in enumerate(demand_by_period):
        for j in si:
            dc_at_j = [dc for dc in pcs
                       if j in feasible.get(dc.cluster_id, [])
                       and (dc.cluster_id, j, t) in x]
            if dc_at_j:
                prob += (
                    pulp.lpSum(dc.n_channels * x[(dc.cluster_id, j, t)]
                               for dc in dc_at_j)
                    <= S[j].max_channels,
                    f"capacity_{j}_t{t}",
                )

    # ── Constraint (8): SINR quality-of-service (linearised) ─────────────────
    # α(i,t)·(1 + SINR_lin) ≥ SINR_lin·P_noise + SINR_lin·β(i,t)
    # where α = power from assigned BS, β = total power from all active BSs in S(i)
    for t, pcs in enumerate(demand_by_period):
        for dc in pcs:
            fj = feasible.get(dc.cluster_id, [])
            xvars_j = [(j, x[(dc.cluster_id, j, t)])
                       for j in fj if (dc.cluster_id, j, t) in x]
            if not xvars_j:
                continue
            alpha_expr = pulp.lpSum(
                powers_mw.get((dc.cluster_id, j), 0.0) * xv
                for j, xv in xvars_j
            )
            beta_expr = pulp.lpSum(
                powers_mw.get((dc.cluster_id, j), 0.0) * y[(j, t)]
                for j in fj
            )
            prob += (
                alpha_expr * (1.0 + sinr_linear)
                >= sinr_linear * noise_mw + sinr_linear * beta_expr,
                f"sinr_{dc.cluster_id}_t{t}",
            )

    # ── Solve ─────────────────────────────────────────────────────────────────
    solver     = pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit_sec)
    prob.solve(solver)
    status_str = pulp.LpStatus[prob.status]
    log.info("MIP status: %s  obj=%.2f", status_str,
             pulp.value(prob.objective) or 0)

    if prob.status != 1:
        return {
            "selected_sites": [], "build_schedule": {}, "assignments": {},
            "total_cost": None, "install_cost": None, "op_cost": None,
            "status": status_str,
            "solver_msg": f"No optimal solution found ({status_str}). "
                          "Try relaxing SINR/capacity constraints or adding more candidate sites.",
            "n_periods": T, "mode": mode,
            "feasibility": {cid: len(fj) for cid, fj in feasible.items()},
        }

    # ── Extract solution ──────────────────────────────────────────────────────
    build_schedule: dict[str, int] = {}
    for j in si:
        for t in range(T):
            if round(pulp.value(z[(j, t)]) or 0) == 1:
                build_schedule[j] = t + 1   # 1-indexed

    assignments: dict[str, int] = {}
    for (i, j, t), var in x.items():
        if round(pulp.value(var) or 0) == 1:
            assignments[str((i, j, t + 1))] = 1  # 1-indexed period

    total_cost  = pulp.value(prob.objective) or 0.0
    i_cost      = sum(S[j].install_cost for j in build_schedule)
    o_cost      = total_cost - i_cost

    return {
        "selected_sites": list(build_schedule.keys()),
        "build_schedule": build_schedule,
        "assignments":    assignments,
        "total_cost":     round(total_cost, 2),
        "install_cost":   round(i_cost, 2),
        "op_cost":        round(o_cost, 2),
        "status":         status_str,
        "solver_msg":     "Optimal solution found",
        "n_periods":      T,
        "mode":           mode,
        "feasibility":    {cid: len(fj) for cid, fj in feasible.items()},
    }
