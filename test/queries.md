# Query Bank — Malleswaram 4G/5G NSA Network

Queries are grouped by objective and ordered from simple reads to complex
intent-based commands. The **5-day demo flow** and **end-to-end test flows**
are at the bottom.

Tools available: `query_network`, `list_cells`, `query_cell`, `move_cell`,
`move_du`, `plan_network`, `plan_network_multi_period`, `apply_plan`,
`get_alerts`, `query_ue`, `get_son_status`, `add_cell`, `remove_cell`,
`optimize_congestion`.

---

## Objective 2 — Network Query Assistant

### UE Queries

| # | Query | Tool(s) exercised |
|---|---|---|
| U1 | How many UEs are currently active in the network? | `query_network` → sum connected_ues across all cells |
| U2 | How many UEs are connected to MLS_RWS_01? | `query_cell` → connected_ues KPI |
| U3 | Which cell is serving the highest number of UEs right now? | `query_network` → rank cells by connected_ues |
| U4 | What is the average SINR across all active cells? | `query_network` → aggregate sinr_db |
| U5 | What is the current UE load distribution across DU-MLS-1, DU-MLS-2, and DU-MLS-3? | `query_network` → per-DU UE totals |
| U6 | Show UE usage data for cell MLS_MGR_01 in the last 30 minutes. | `query_ue` with cell_id=MLS_MGR_01 |
| U7 | What is the average latency for UEs on MLS_BEL_01? | `query_ue` with cell_id=MLS_BEL_01 → avg latency_ms |
| U8 | Show handover events for UEs on MLS_SNK_01. | `query_ue` with cell_id=MLS_SNK_01 → mobility_events |
| U9 | What is the packet loss on MLS_3MN_01? | `query_ue` with cell_id=MLS_3MN_01 → packet_loss field |
| U10 | Show UE mobility data for the last hour on MLS_CHD_01. | `query_ue` with cell_id=MLS_CHD_01, last_minutes=60 |

> **Note:** Individual per-UE throughput ranking ("top 10 UEs by throughput")
> requires post-processing of `query_ue` records. RACH failure counts are not
> tracked. Video-streaming UE identification is not filterable per-UE via chat.

---

### Cell Queries

| # | Query | Tool(s) exercised |
|---|---|---|
| C1 | How many cells are deployed in the network? | `query_network` → count cells |
| C2 | Show details of MLS_RWS_01. | `query_cell` → full config + 30-min KPI series |
| C3 | List all n78 cells. | `list_cells` → LLM filters by band |
| C4 | Which cells currently have more than 80% PRB load? | `query_network` → filter prb_dl_pct > 80 |
| C5 | Which cell serves the highest number of UEs? | `query_network` → rank by connected_ues |
| C6 | Show all 5G NR cells under DU-MLS-2. | `list_cells` with du_id=DU-MLS-2 |
| C7 | What is the current throughput of MLS_SNK_01? | `query_cell` → dl_throughput_mbps |
| C8 | Which cells have SINR below 5 dB? | `query_network` → filter sinr_db < 5 |
| C9 | Show all B40 residential cells. | `list_cells` → LLM filters by band |
| C10 | What is the congestion score for the top 5 most loaded cells? | `optimize_congestion` with top_n=5 |
| C11 | Which cells are at CRITICAL congestion level? | `optimize_congestion` → filter by level=CRITICAL |
| C12 | Show neighbor headroom for the most congested cell. | `optimize_congestion` → neighbors field on top cell |

---

### Topology Queries

| # | Query | Tool(s) exercised |
|---|---|---|
| T1 | Show the network topology. | `query_network` → CU/DU/cell hierarchy |
| T2 | Which DU manages MLS_MGR_01? | `query_network` → reverse lookup cell→DU |
| T3 | Which cells belong to DU-MLS-3? | `list_cells` with du_id=DU-MLS-3 |
| T4 | Show all cells connected to CU-MLS. | `list_cells` with cu_id=CU-MLS |
| T5 | Which DU is least loaded right now? | `query_network` → rank DUs by avg PRB |
| T6 | How many DUs does CU-MLS manage? | `query_network` → CU config |
| T7 | Which sites carry both a 5G n78 and a 4G B40 cell? | `query_network` → LLM groups by lat/lon |

---

## Objective 4 — Cell Deployment Agent

### Explicit Deployment Requests

| # | Query | Tool(s) exercised |
|---|---|---|
| D1 | Deploy a new n78 cell in Malleswaram. | `plan_network` with spectrum_bands=["n78"] |
| D2 | Add a new 5G cell at latitude 13.008, longitude 77.569 under DU-MLS-1. | `add_cell` with lat/lon/du_id |
| D3 | Deploy a cell capable of serving 500 additional users. | `plan_network` with expected_user_density adjusted |
| D4 | Plan a network for Malleswaram using only n78 and B3 spectrum. | `plan_network` with spectrum_bands=["n78","B3"] |
| D5 | Run an optimal MIP deployment plan for Malleswaram. | `plan_network` with use_mip=true |
| D6 | Plan a phased network rollout across multiple time periods. | `plan_network_multi_period` with demand_mode=permanent |
| D7 | Plan a network for a temporary large event demand shift near Sankey Tank. | `plan_network_multi_period` with demand_mode=temporary |
| D8 | Add a 4G B3 anchor cell at MLS_NEW_01 near Malleswaram station. | `add_cell` with generation=4G, band=B3 |
| D9 | Remove MLS_NEW_01 from the network. | `remove_cell` with cell_id=MLS_NEW_01 |

> **Note:** "Deploy a 700 MHz cell" is intentionally unsupported — n28 is excluded
> because 700 MHz coverage radius (~8.4 km) extends beyond Malleswaram to Peenya.

### Constraint-Based Deployment

| # | Query | Tool(s) exercised |
|---|---|---|
| D10 | Deploy a new cell and assign it to the least loaded DU. | `query_network` → identify lightest DU, then `add_cell` |
| D11 | Add cells to reduce congestion in the most loaded area. | `optimize_congestion` → `plan_network` or `add_cell` |
| D12 | Plan a network with strict SINR above 10 dB across all sites. | `plan_network` with use_mip=true, sinr_min_db=10 |
| D13 | Generate a plan and immediately apply it to the live network. | `plan_network` → `apply_plan` with returned plan_id |

### Follow-up Queries (after a plan is generated)

| # | Query | Tool(s) exercised |
|---|---|---|
| F1 | Why was that site selected for the new cell? | LLM explains density_weight and budget logic from plan |
| F2 | What cells are affected by the new deployment? | LLM cross-references plan cells with current topology |
| F3 | Apply the plan and show the updated topology. | `apply_plan` then `query_network` |
| F4 | How much additional capacity does the new plan provide? | LLM computes total_capacity_ues delta from plan summary |
| F5 | What is the estimated cost of this deployment? | Plan summary → estimated_cost_usd |

---

## Objective 5 — Self-Organizing Network (SON) Agent

### Anomaly Detection

| # | Query | Tool(s) exercised |
|---|---|---|
| A1 | What anomalies currently exist in the network? | `get_alerts` → last 60 min, all severities |
| A2 | Identify overloaded cells. | `get_alerts` with alert_type=OVERLOAD or `query_network` PRB filter |
| A3 | Are there any cells with poor SINR performance? | `get_alerts` SINR_LOW + `query_network` |
| A4 | Show all CRITICAL alerts from the last hour. | `get_alerts` with severity=CRITICAL |
| A5 | Which cells are wasting power? | `get_alerts` with alert_type=POWER_WASTE |
| A6 | Show cells the SON agent flagged as underloaded. | `get_alerts` with alert_type=UNDERLOAD |

### Congestion Analysis

| # | Query | Tool(s) exercised |
|---|---|---|
| CON1 | Show the full congestion report ranked by score. | `optimize_congestion` with top_n=30 |
| CON2 | Which cells need immediate intervention? | `optimize_congestion` → filter level=CRITICAL or HIGH |
| CON3 | Which neighbors of MLS_MGR_01 have spare capacity? | `optimize_congestion` → neighbors field |
| CON4 | What is the network-wide congestion summary? | `optimize_congestion` → summary field |

### SON Action History

| # | Query | Tool(s) exercised |
|---|---|---|
| SON1 | What autonomous actions did the SON agent take in the last hour? | `get_son_status` with last_minutes=60 |
| SON2 | How many load-balancing moves has the SON agent made today? | `get_son_status` → action_type_counts |
| SON3 | Show the most recent SON actions. | `get_son_status` → recent_actions |
| SON4 | How many alerts has the KPI agent raised in the last 30 minutes? | `get_son_status` with last_minutes=30 → total_alerts |
| SON5 | What is the breakdown of alert severities? | `get_son_status` → alert_severity_counts |

### Optimization Requests

| # | Query | Tool(s) exercised |
|---|---|---|
| O1 | Optimize the network for load balancing. | `optimize_congestion` → LLM calls `move_cell` for CRITICAL cells |
| O2 | Move the most overloaded cell to a lighter DU. | `query_network` + `move_cell` |
| O3 | Balance traffic across DU-MLS-1 and DU-MLS-2. | `query_network` → compare DU loads → `move_cell` |
| O4 | Reduce congestion on the most loaded cell. | `optimize_congestion` → `move_cell` or `add_cell` |

### Intent-Based SON Commands

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

## End-to-End Test Flows

These flows test the full pipeline from query → tool call → live network change → verification.
Run them in sequence; each step verifies the previous one had effect.

### E2E-1 — Congestion Detection and Resolution

```
1. Show the full congestion report.
   [expects: optimize_congestion returns ranked cells with scores]

2. Which cells are at CRITICAL or HIGH congestion level?
   [expects: list of cell_ids with level=CRITICAL/HIGH]

3. Move [most congested cell] to the lightest DU.
   [expects: move_cell succeeds, returns updated topology]

4. Wait 10 seconds, then query the network again.
   [expects: cell now appears under new DU in query_network]

5. What autonomous actions did the SON agent take in the last 10 minutes?
   [expects: get_son_status shows recent load-balance action]
```

### E2E-2 — Live Cell Add and Remove

```
1. Show the current network topology.
   [expects: 30 cells across 3 DUs]

2. Add a new 5G n78 cell MLS_TEST_01 at lat=13.003, lon=77.575 under DU-MLS-2.
   [expects: add_cell succeeds; cell appears in topology within 5 s]

3. List all cells under DU-MLS-2.
   [expects: MLS_TEST_01 is now in the list]

4. Query MLS_TEST_01 details.
   [expects: cell config with auto-assigned PCI, no KPI data yet]

5. Remove MLS_TEST_01 from the network.
   [expects: remove_cell succeeds]

6. List all cells under DU-MLS-2 again.
   [expects: MLS_TEST_01 no longer appears]
```

### E2E-3 — Planning, Apply, Verify

```
1. Generate a network plan for Malleswaram with n78 and B3 spectrum.
   [expects: plan_network returns plan_id, cell list, PCI assignments]

2. What cells were selected in the plan and what is the estimated cost?
   [expects: LLM reads plan summary]

3. Apply the plan to the live network.
   [expects: apply_plan with plan_id succeeds; topology replaced]

4. Show the updated network topology.
   [expects: query_network reflects the new plan's cells and DU groupings]

5. What is the total connected UE capacity of the new deployment?
   [expects: LLM sums max_ues from plan cells]
```

### E2E-4 — Alert Pipeline to Autonomous Action

```
1. What anomalies exist in the network right now?
   [expects: get_alerts returns any OVERLOAD/SINR_LOW/POWER_WASTE alerts]

2. Show recent SON actions in the last 30 minutes.
   [expects: get_son_status shows action history]

3. Show UE latency data on the most overloaded cell.
   [expects: query_ue shows latency_ms values for that cell]

4. Move the overloaded cell to the least loaded DU.
   [expects: move_cell succeeds]

5. After 30 seconds, check if new alerts have appeared.
   [expects: get_alerts — overload alert should stop recurring after move]
```

### E2E-5 — Multi-Period Planning for Event Load

```
1. Plan a network for a temporary event demand shift.
   [expects: plan_network_multi_period with demand_mode=temporary returns period schedule]

2. What is the build schedule across periods?
   [expects: LLM summarises which BSs are built in each period]

3. What is the total budget consumed?
   [expects: LLM reads estimated_cost_usd from plan]

4. Apply the plan.
   [expects: apply_plan succeeds with the multi-period plan_id]

5. Show the updated topology.
   [expects: query_network reflects new cells from period-1 build]
```

---

## Failure Scenarios

These queries are designed to trigger error paths and verify the system fails
gracefully with informative messages.

### Invalid Cell and DU References

| # | Query | Expected behaviour |
|---|---|---|
| ERR1 | Move MLS_FAKE_99 to DU-MLS-2. | `move_cell` returns error — cell_id does not exist in topology |
| ERR2 | Move MLS_RWS_01 to DU-FAKE-99. | `move_cell` returns error — target DU does not exist |
| ERR3 | Move DU-MLS-1 to CU-FAKE. | `move_du` returns error — target CU does not exist |
| ERR4 | Show details of MLS_FAKE_99. | `query_cell` returns 404 / empty — cell does not exist |
| ERR5 | Remove MLS_FAKE_99 from the network. | `remove_cell` returns error — cell not found |

### Duplicate and Constraint Violations

| # | Query | Expected behaviour |
|---|---|---|
| ERR6 | Add a new cell with cell_id MLS_RWS_01 (already exists). | `add_cell` returns error — duplicate cell_id |
| ERR7 | Deploy a 700 MHz n28 cell in Malleswaram. | LLM refuses — n28 excluded by design (coverage extends to Peenya) |
| ERR8 | Plan a network with a budget of $1. | `plan_network` returns minimal/empty plan or budget-infeasible error |
| ERR9 | Apply plan_id abc-nonexistent. | `apply_plan` returns error — plan_id not found |
| ERR10 | Add a cell with missing required fields (no lat/lon). | LLM asks for missing parameters before calling `add_cell` |

### Boundary and Edge Cases

| # | Query | Expected behaviour |
|---|---|---|
| ERR11 | Query UE data for UE-DOESNOTEXIST-9999. | `query_ue` returns empty usage_records and mobility_events — no error, just empty |
| ERR12 | Get alerts for the last 0 minutes. | `get_alerts` with last_minutes=0 returns empty list — no error |
| ERR13 | Move MLS_RWS_01 to DU-MLS-1 (it is already on DU-MLS-1). | `move_cell` may succeed as no-op or return a same-DU warning |
| ERR14 | Show SON status for the last 1000 minutes. | `get_son_status` returns data for the available window — no crash |
| ERR15 | Get congestion report for top 100 cells (only 30 exist). | `optimize_congestion` with top_n=100 returns all 30 cells — no error |

### System Stress Queries

| # | Query | Expected behaviour |
|---|---|---|
| ERR16 | Move all cells from DU-MLS-1 to DU-MLS-2 (12 cells). | Each `move_cell` call succeeds; DU-MLS-2 becomes heavily overloaded |
| ERR17 | Remove all B40 cells from the network. | Each `remove_cell` succeeds; `query_network` shows 25 remaining cells |
| ERR18 | Generate 5 plans back-to-back without applying any. | Each `plan_network` returns a new plan_id; all are stored in-memory |
| ERR19 | Apply a plan then immediately apply a second plan. | Second `apply_plan` replaces the first; topology reflects plan 2 |

---

## 5-Day Demo Flow

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

### Day 2 — Anomaly detection and SON alerting

```
1. What anomalies currently exist in the network?
2. Which cells are overloaded?
3. Show all CRITICAL alerts from the last hour.
4. What autonomous actions did the SON agent take in the last hour?
5. Show the congestion report with neighbor headroom hints.
```

**Demonstrates:** KPI agent LSTM anomaly classification, alert pipeline,
congestion scoring, SON action history.

---

### Day 3 — Root cause, UE impact, and planning

```
1. Which cell is most congested right now?
2. Show UE latency data on that cell.
3. Deploy a new n78 cell near the congested area.
4. What is the estimated cost of this deployment?
5. Apply the plan and show the updated topology.
```

**Demonstrates:** UE-level impact analysis, planning agent, live topology update.

---

### Day 4 — Autonomous optimization and live topology change

```
1. Show the full congestion report.
2. Move the most overloaded cell to the lightest DU.
3. Ensure no cell exceeds 80% utilization.
4. Add a temporary capacity cell MLS_EVENT_01 near Sankey Tank.
5. What actions did you take to optimize the network?
```

**Demonstrates:** Congestion-driven cell moves, live add/remove, explainability.

---

### Day 5 — Intent-based commands and multi-period planning

```
1. Prepare the network for a large event expected near Sankey Tank.
2. Plan a phased rollout to handle permanent demand growth.
3. What is the optimal build schedule across periods?
4. Apply the plan and compare capacity before and after.
5. What would happen if no action is taken on the remaining overloaded cells?
```

**Demonstrates:** Intent-based NL commands, multi-period MIP planning,
before/after capacity comparison, counterfactual explanation.

---

## Known Gaps (queries that will fail or give incomplete answers)

| Query | Reason |
|---|---|
| Show the top 10 UEs by throughput | `query_ue` returns per-UE records but no server-side sort by throughput |
| List UEs currently streaming video | Slice types simulated but not filterable per-UE via chat |
| Which UEs had more than 3 handovers today? | `query_ue` returns last mobility event per UE, not a count over the day |
| Show the UE with the highest RACH attempts | RACH procedures not implemented in simulator |
| Show all neighboring cells of MLS_RWS_01 | No direct NRL tool; `/neighbors` is used internally by `optimize_congestion` |
| Explain the connection path from UE_X to the core | No per-UE session or E2E path tracking |
| Deploy a 700 MHz cell | n28 excluded by design — coverage extends to Peenya (~8.4 km radius) |
