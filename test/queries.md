# Query Bank — 4G/5G NSA Network

Queries are grouped by objective and ordered from simple reads to complex
intent-based commands. The **5-day demo flow** at the bottom is the recommended
walkthrough sequence for presenting the system.

---

## Objective 2 — Network Query Assistant

### UE Queries

| # | Query | What it exercises |
|---|---|---|
| U1 | How many UEs are currently active in the network? | `query_network` → sum connected_ues across all cells |
| U2 | How many UEs are connected to MLS_RWS_01? | `query_cell` → connected_ues KPI |
| U3 | Which cell is serving the highest number of UEs right now? | `query_network` → rank cells by connected_ues |
| U4 | What is the average SINR across all active cells? | `query_network` → aggregate sinr_db |
| U5 | Which UE is consuming the most bandwidth right now? | `query_network` → rank by dl_throughput_mbps |
| U6 | What is the current UE load distribution across DU-MLS-1, DU-MLS-2, and DU-MLS-3? | `query_network` → per-DU UE totals |

> **Note:** Individual UE-level queries (top-10 UEs by throughput, video-streaming UEs,
> per-UE handover counts, RACH attempts) are not yet supported — the system models
> aggregate cell-level KPIs, not individual UE sessions. These queries are marked
> below as gaps to implement.

**Queries requiring future implementation:**

```
Show the top 10 UEs by throughput.
List all UEs currently streaming video traffic.
Which UEs have experienced more than 3 handovers today?
Show the UE with the highest number of RACH attempts.
```

---

### Cell Queries

| # | Query | What it exercises |
|---|---|---|
| C1 | How many cells are deployed in the network? | `query_network` → count cells |
| C2 | Show details of MLS_RWS_01. | `query_cell` → full config + 30-min KPI series |
| C3 | List all n78 cells. | `list_cells` → filter by band |
| C4 | Which cells currently have more than 80% PRB load? | `query_network` → filter prb_dl_pct > 80 |
| C5 | Which cell serves the highest number of UEs? | `query_network` → rank by connected_ues |
| C6 | Show all 5G NR cells under DU-MLS-2. | `list_cells` with du_id filter |
| C7 | What is the current throughput of MLS_SNK_01? | `query_cell` → dl_throughput_mbps |
| C8 | Which cells have SINR below 5 dB? | `query_network` → filter sinr_db < 5 |
| C9 | Show all B40 residential cells. | `list_cells` → LLM filters by band |

> **Note:** Neighbor cell queries ("show all neighboring cells of Cell_X") are not
> yet supported — there is no NRL (Neighbor Relation List) in the topology.

```
Show all neighboring cells of MLS_RWS_01.   [not yet implemented — NRL missing]
```

---

### Topology Queries

| # | Query | What it exercises |
|---|---|---|
| T1 | Show the network topology. | `query_network` → CU/DU/cell hierarchy |
| T2 | Which DU manages MLS_MGR_01? | `query_network` → reverse lookup cell→DU |
| T3 | Which cells belong to DU-MLS-3? | `list_cells` with du_id=DU-MLS-3 |
| T4 | Show all cells connected to CU-MLS. | `list_cells` with cu_id=CU-MLS |
| T5 | Which DU is least loaded right now? | `query_network` → rank DUs by avg PRB |
| T6 | How many DUs does CU-MLS manage? | `query_network` → CU config |
| T7 | Which sites carry both a 5G n78 and a 4G B40 cell? | `query_network` → LLM groups by lat/lon |

> **Note:** End-to-end path tracing ("explain the path from UE_1234 to the core")
> is not supported — there is no per-UE session tracking and the core is a single
> simulated component.

```
Explain the connection path from UE_1234 to the core network.  [not yet implemented]
```

---

## Objective 4 — Cell Deployment Agent

### Explicit Deployment Requests

| # | Query | What it exercises |
|---|---|---|
| D1 | Deploy a new n78 cell in Malleswaram. | `plan_network` with spectrum_bands=["n78"] |
| D2 | Add a new cell at latitude 13.008, longitude 77.569. | `plan_network` — LLM maps coordinates to nearest candidate site |
| D3 | Deploy a cell capable of serving 500 additional users. | `plan_network` with expected_user_density and budget adjusted |
| D4 | Plan a network for Malleswaram using only n78 and B3 spectrum. | `plan_network` with spectrum_bands=["n78","B3"] |
| D5 | Run an optimal MIP deployment plan for Malleswaram. | `plan_network` with use_mip=true |
| D6 | Plan a phased network rollout across multiple time periods. | `plan_network_multi_period` with demand_mode=permanent |

> **Note:** Queries referencing specific landmarks ("near Orion Mall", "northwest region")
> are supported conversationally — the LLM maps these to approximate coordinates and
> calls plan_network. The candidate site inventory is fixed to 10 Malleswaram tower
> locations; arbitrary lat/lon outside these candidates will select the closest one.
>
> "Deploy a 700 MHz cell" is intentionally unsupported — n28 is excluded from this
> deployment because 700 MHz coverage extends beyond Malleswaram into Peenya.

### Constraint-Based Deployment

| # | Query | What it exercises |
|---|---|---|
| D7 | Deploy a new cell that minimizes overlap with existing cells. | `plan_network` with use_mip=true, sinr_min_db constraint |
| D8 | Deploy a new cell and assign it to the least loaded DU. | `plan_network` → LLM notes DU load from `query_network` first |
| D9 | Add cells to reduce congestion in the most loaded area. | `query_network` → identify area, then `plan_network` |
| D10 | Plan a network with strict SINR above 10 dB across all sites. | `plan_network` with use_mip=true, sinr_min_db=10 |

### Follow-up Queries (after a plan is generated)

| # | Query | What it exercises |
|---|---|---|
| F1 | Why was that site selected for the new cell? | LLM explains density_weight and budget logic |
| F2 | What cells are affected by the new deployment? | LLM cross-references plan cells with current topology |
| F3 | Show the topology after deployment. | `apply_plan` then `query_network` |
| F4 | How much additional capacity does the new plan provide? | LLM computes total_capacity_ues delta from plan summary |
| F5 | What is the estimated cost of this deployment? | Plan summary → estimated_cost_usd |

---

## Objective 5 — Self-Organizing Network (SON) Agent

### Anomaly Detection

| # | Query | What it exercises |
|---|---|---|
| A1 | What anomalies currently exist in the network? | `get_alerts` → last 60 min, all severities |
| A2 | Identify overloaded cells. | `get_alerts` with alert_type=OVERLOAD or `query_network` PRB filter |
| A3 | Are there any cells with poor SINR performance? | `get_alerts` SINR_DEGRADATION + `query_network` |
| A4 | Show all CRITICAL alerts from the last hour. | `get_alerts` with severity=CRITICAL |
| A5 | Which cells are wasting power? | `get_alerts` with alert_type=POWER_WASTE |
| A6 | Show cells the SON agent flagged as underloaded. | `get_alerts` with alert_type=UNDERLOAD |

> **Note:** Per-cell handover failure rates and RACH failure counts are not tracked
> by the KPI agent. These queries are marked as gaps.

```
Which cells are experiencing abnormal handover failures?  [HO failure rate not tracked]
Show cells with excessive RACH failures.                  [RACH not implemented]
```

### Optimization Requests

| # | Query | What it exercises |
|---|---|---|
| O1 | Optimize the network for load balancing. | `get_alerts` → LLM calls `move_cell` for overloaded cells |
| O2 | Move the most overloaded cell to a lighter DU. | `query_network` + `move_cell` |
| O3 | Balance traffic across DU-MLS-1 and DU-MLS-2. | `query_network` → compare DU loads → `move_cell` |
| O4 | Reduce congestion on the most loaded cell. | `query_network` + `plan_network` or `move_cell` |

### Intent-Based SON Commands

These are the most impressive demo queries — the user specifies the goal, the LLM
decides the actions:

| # | Query |
|---|---|
| I1 | Improve user experience in the Malleswaram railway station area. |
| I2 | Reduce call drops in the southern cells. |
| I3 | Ensure no cell exceeds 80% utilization. |
| I4 | Prepare the network for a large event expected near Sankey Tank. |
| I5 | Optimize the network for maximum throughput. |
| I6 | Improve coverage in weak signal areas. |
| I7 | Increase capacity near Malleswaram 18th Cross. |

### Explainability Queries

| # | Query |
|---|---|
| E1 | What actions did you take to optimize the network? |
| E2 | Why did you move that cell to a different DU? |
| E3 | Why do you recommend deploying a new cell? |
| E4 | What would happen if no action is taken on the overloaded cells? |
| E5 | How confident is the SON agent in its anomaly detection? |

---

## 5-Day Demo Flow

Recommended walkthrough for a presentation. Each step builds on the previous one.

### Day 1 — Baseline situational awareness

```
1. Show the network topology.
2. How many UEs are currently active in the network?
3. List all n78 cells.
4. Which cells currently have more than 80% PRB load?
5. Show the current network KPIs.
```

**Demonstrates:** Real-time topology awareness, live KPI queries, cell inventory.

---

### Day 2 — Anomaly detection and alerting

```
1. What anomalies currently exist in the network?
2. Which cells are overloaded?
3. Are there any cells with poor SINR performance?
4. Show all CRITICAL alerts from the last hour.
5. Which cell serves the highest number of UEs?
```

**Demonstrates:** SON agent alert pipeline, LSTM anomaly classification, alert
severity tagging.

---

### Day 3 — Root cause and planning

```
1. Which cell is most congested right now?
2. Why is [cell] overloaded?
3. Deploy a new cell to reduce congestion around [cell].
4. What is the estimated cost of this deployment?
5. What cells are affected by the new deployment?
```

**Demonstrates:** LLM-driven root cause reasoning, planning agent, PCI/DU/slice
auto-assignment, cost estimation.

---

### Day 4 — Autonomous optimization and topology change

```
1. Show the updated network topology.
2. Optimize the network for load balancing.
3. Move the most overloaded cell to a lighter DU.
4. Ensure no cell exceeds 80% utilization.
5. What actions did you take to optimize the network?
```

**Demonstrates:** Autonomous cell moves via SON agent and LLM, topology changes
propagating live to simulators, explainability.

---

### Day 5 — Intent-based commands and KPI comparison

```
1. Prepare the network for a large event expected near Sankey Tank.
2. Increase capacity near Malleswaram railway station.
3. What actions did you take?
4. Compare KPIs before and after optimization.
5. What would happen if no action is taken on the remaining overloaded cells?
```

**Demonstrates:** Intent-based NL commands, end-to-end autonomy, before/after
KPI reasoning, counterfactual explanation.

---

## Known Gaps (queries that will fail or give incomplete answers)

These are listed here so demo presenters can avoid them or frame them as future work:

| Query | Reason |
|---|---|
| Show the top 10 UEs by throughput | No per-UE session tracking — only cell-aggregate data |
| List UEs currently streaming video | Slice types simulated but not queryable per-UE via chat |
| Which UEs had more than 3 handovers today? | Handover events in InfluxDB but no tool to query them |
| Show the UE with the highest RACH attempts | RACH procedures not implemented |
| Show all neighboring cells of Cell_X | No Neighbor Relation List (NRL) in topology |
| Explain the connection path from UE_X to the core | No per-UE session or path tracking |
| Deploy a 700 MHz cell | n28 excluded by design — coverage extends to Peenya |
| Apply plan and show updated map | /topology/replace missing from controller — plans cannot be auto-applied yet |
