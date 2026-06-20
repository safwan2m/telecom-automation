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

---

## Loophole Test Queries

These queries are designed to expose **structural gaps** in the simulator, KPI agent,
LSTM model, and planning API. Each query names the loophole it targets and what
correct behaviour would look like vs what the system actually does.

---

### L — Simulator Blind Spots

| # | Query | Loophole exposed | Expected vs Actual |
|---|---|---|---|
| L1 | Which UE connected to MLS_RWS_01 is consuming the most bandwidth right now? Ask again 30 seconds later. | `usage_sample()` picks only **8 UEs per tick** from a pool of up to 900. The "top UE" changes completely every 10 s because it reflects a random 8-UE window, not all 900. | **Expected:** stable top-UE ranking. **Actual:** answer changes every query — 99% of UEs are never visible. |
| L2 | How many UEs are in the network right now? Compare that number to the theoretical peak of 18,400. | During the 7 PM peak the simulator caps each cell at `max_ues`. Overflow UEs are **silently dropped** — they are not handed to neighbouring cells. | **Expected:** ~18,400 at peak. **Actual:** sum of `max_ues` across 30 cells (~16,300). Missing UEs are nowhere in the system. |
| L3 | Show handover events for MLS_RWS_01. Which cells are the handover targets? | `mobility_events()` picks `target = random.choice(neighbours)` regardless of signal strength or load. In a real network handovers go to the strongest-RSRP neighbour. | **Expected:** targets are the closest, highest-RSRP cells. **Actual:** any cell in the same DU is equally likely — even lightly-covered ones far away. |
| L4 | Two adjacent overloaded cells (MLS_RWS_01 and MLS_18C_01) are both at 95% PRB. Does either cell's SINR reflect inter-cell interference from the other? | `interference_dbm = -100 + load × 20 + gaussian` depends only on the **cell's own load**, not on neighbours. Co-channel interference between adjacent cells is not modelled. | **Expected:** adjacent 5G n78 cells at 95% load should show elevated interference and suppressed SINR. **Actual:** each cell's SINR is independent of the other. |
| L5 | At what local time (IST) does the simulator show peak traffic? Check `prb_dl_pct` at 1 PM IST vs 7 PM IST. | `load_factor()` uses `datetime.now()` inside the Docker container, which runs in **UTC**. `HOURLY_LOAD[19]` (peak = 1.00) fires at 19:00 UTC = 00:30 IST the next day. The true evening peak is not simulated at the right local time. | **Expected:** peak PRB at ~7 PM IST. **Actual:** peak fires at 19:00 container-timezone (UTC if no TZ env var set), ~5.5 h off from Bangalore IST. |
| L6 | Can two UEs on the same cell ever have the same UE ID? Describe how UE IDs are generated. | In `tick()`, new UEs are added as `f"UE-{cell_id}-{random.randint(0,9999):04d}"`. With 900 UEs and only 10,000 possible suffixes, the birthday paradox gives ~**5% collision probability** per cell. A colliding UE silently overwrites the previous one's slice type. | **Expected:** unique UE IDs. **Actual:** ~1 in 20 ticks on a full cell overwrites an existing UE entry with a different slice assignment. |

---

### L — KPI Agent / SON Behaviour Gaps

| # | Query | Loophole exposed | Expected vs Actual |
|---|---|---|---|
| L7 | MLS_RWS_01 has been at 92% PRB for the last 8 minutes. Show SON actions taken on it in that window. | `COOLDOWN_SEC = 300`. After the first action, the cell is locked for **5 minutes**. If the move fails or the DU target is also overloaded, no retry fires. The cell stays critical with only alert writes — no corrective action. | **Expected:** repeated steering until PRB drops. **Actual:** one action attempt, then silence for 5 min regardless of outcome. |
| L8 | How many alerts exist for MLS_SNK_01 in the last 60 minutes? | No alert deduplication exists. Every KPI agent cycle (~every 10–30 s) writes a **new alert row** for every problem cell. One persistently overloaded cell produces 120–360 identical alert rows per hour. | **Expected:** 1–3 alerts (initial detection + re-check). **Actual:** potentially hundreds of identical rows filling InfluxDB. |
| L9 | MLS_BEL_01 is classified OVERLOAD with a congestion score of 0.70. Its neighbours are all at 65–70% PRB. What SON action fires? | Neighbor steering requires `neighbour PRB < OVERLOAD_PRB − 25 = 60%`. A DU move requires `score > 0.75`. At score 0.70 with neighbours at 65–70%, **neither branch fires**. The cell is in the dead zone. | **Expected:** some action (partial steering, advisory). **Actual:** OVERLOAD alert is written but no steering, no move — cell remains unaddressed indefinitely. |
| L10 | What does the PRE_EMPTIVE_STEER SON action actually change in the network? Check topology before and after. | `_write_son_action(..., "PRE_EMPTIVE_STEER", ...)` only inserts a row into the `son_actions` InfluxDB measurement. **No handover command is sent to the cell, no UE is moved, no API is called.** It is a log entry masquerading as an action. | **Expected:** UEs proactively handed to a less-loaded neighbour. **Actual:** topology and UE distribution are unchanged. |
| L11 | Restart the KPI agent container. What happens to anomaly detection in the first 60 seconds? | `buffers` (per-cell deques) are **in-memory** and lost on restart. For the first 6 poll cycles (60 s at 10 s intervals), every cell falls back to **rule-based classification**. LSTM trend detection is blind during this window. | **Expected:** continuous LSTM-based detection. **Actual:** first 60 s after any KPI agent restart reverts to threshold rules; trending pre-overload conditions go undetected. |
| L12 | Stop DU-MLS-2 for 4 minutes. Does the KPI agent raise an alert about the missing cells? | The KPI agent queries `range(start: -3m)`. Cells that stop writing after exactly 3 minutes **disappear silently** from monitoring — no "cell gone silent" alert exists. | **Expected:** alert when a cell stops reporting. **Actual:** cells vanish from the analyse() loop with no notification; `cells` list simply shrinks. |
| L13 | Show the LSTM classification confidence for a 5G n78 cell serving exactly 900 UEs (at max_ues). | LSTM normalises `connected_ues` on `[0, 800]`. At 900 UEs, the normalised value is `900/800 = 1.125` — **outside the [0,1] training range**. The LSTM receives an out-of-distribution input for cells at max capacity. | **Expected:** reliable NORMAL/OVERLOAD classification. **Actual:** LSTM confidence may be artificially low or class may be incorrect; the model was never trained on values > 800 UEs. |
| L14 | Check the `son_actions` table in InfluxDB for any entry with `action_type = LOAD_BALANCE`. Cross-reference with the `_last_moved` variable in kpi_agent.py. | `_last_moved: dict[str, float]` is defined at module level but **never read or written** in the analysis loop. `_is_cooling_down` and `_mark_action` use `_cell_cooldown` instead. `_last_moved` is dead code — any logic relying on it has no effect. | **Expected:** cooldown tracking via `_last_moved`. **Actual:** cooldown is tracked via `_cell_cooldown`; `_last_moved` is unused. |

---

### L — Planning API Durability Gaps

| # | Query | Loophole exposed | Expected vs Actual |
|---|---|---|---|
| L15 | Generate a network plan, restart the planning API container, then try to apply the plan using the same plan_id. | Plans are stored in `_plans = {}` (in-memory dict in `planner_api.py`). A **container restart wipes all plans**. `apply_plan` returns "plan not found" with no recovery path. | **Expected:** plan persists across restarts (disk or DB). **Actual:** plan_id becomes invalid after any restart; operator must regenerate the plan. |
| L16 | Apply a new plan while the network is serving 7,000 live UEs. What happens to those UEs? | `apply_plan` calls `POST /topology/replace` which **atomically replaces the entire topology**. All 30 existing cells are removed and replaced with the plan's cells in one write. All in-flight UE sessions on the old cells are orphaned. | **Expected:** rolling migration or at least a warning before replacement. **Actual:** immediate full-topology replacement with no UE migration — equivalent to a network wipe. |
| L17 | Generate a plan for 10 new cells near existing cells. Do any of the new cells conflict on PCI with the already-deployed 30 cells? | The PCI planner (`pci_planner.py`) assigns PCIs collision-free **within the generated plan only**. It does not read the current topology's PCI assignments. A new cell can be assigned PCI 15 even if a nearby existing cell already uses PCI 15. | **Expected:** PCI checked against both new and existing cells. **Actual:** PCI collision between plan cells and deployed cells is possible and undetected. |
| L18 | Plan a network where all 30 cells are assigned to DU-MLS-1 (by forcing du_count=1). Does the plan warn that DU-MLS-1 would be overloaded? | `assign_dus` groups cells by geography into `du_count` DUs. No check is made on **DU processing capacity** (cpu_pct, max schedulable cells). A plan can assign 30 cells to one DU with no warning. | **Expected:** capacity check per DU. **Actual:** any cell-to-DU assignment is accepted regardless of DU load or cell count. |

---

## Known Gaps (queries that will fail or give incomplete answers)

| Query | Reason |
|---|---|
| Show the top 10 UEs by throughput | No per-UE session tracking — only cell-aggregate data |
| List UEs currently streaming video | Slice types simulated but not queryable per-UE via chat |
| Which UEs had more than 3 handovers today? | Handover events in InfluxDB but no tool to query them |
| Show the UE with the highest RACH attempts | RACH procedures not implemented |
| Show all neighboring cells of Cell_X | No Neighbor Relation List (NRL) in topology |
| Explain the connection path from UE_X to the core | No per-UE session or path tracking |
| Deploy a 700 MHz cell | n28 excluded by design — coverage extends to Peenya |

Q: Would like to deploy a new cell in Milk colony in Malleswaram, the user density is expected to spike by 300 users. Is the current network capable of handling that?
coordinates_hallucinated.
