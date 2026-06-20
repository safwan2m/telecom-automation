# Dev Environment — Specification

12-container Docker Compose stack for the Malleswaram simulation. All containers share a single internal bridge network (`telecom_net`). External ports are for developer access only; inter-container calls use service names.

## Container map

| Service | Image | Port | Source |
|---|---|---|---|
| `influxdb` | `influxdb:2.7` | 8086 | official image |
| `grafana` | `grafana/grafana:10.2.0` | 3000 | official image |
| `controller` | custom | 8080 | `agents/controller/` |
| `planning-api` | custom | 8081 | `agents/planning/` |
| `orchestrator` | custom | 8082 | `agents/orchestrator/` |
| `map-server` | custom | 8083 | `agents/map_server/` |
| `kpi-agent` | custom | — | `agents/kpi_agent/` |
| `core-sim` | custom | — | `dev-env/simulators/core/` |
| `cu-mls-1` | custom | — | `dev-env/simulators/cu/` |
| `du-mls-1` | custom | — | `dev-env/simulators/du/` |
| `du-mls-2` | custom | — | `dev-env/simulators/du/` |
| `du-mls-3` | custom | — | `dev-env/simulators/du/` |

All three DU instances and the one CU instance run the **same Docker image**; the `DU_ID` / `CU_ID` env var differentiates their identity at runtime.

## Topology source of truth

`dev-env/config/topology.json` is mounted into all containers at `/config/topology.json`. The Controller writes it; all DU and CU simulators poll it every `TOPO_POLL_SEC` (5 s) and reconfigure themselves live. No restart needed for topology changes.

## InfluxDB measurements

| Measurement | Tags | Key fields | Writer |
|---|---|---|---|
| `cell_kpi` | `cell_id`, `du_id`, `cu_id`, `vendor`, `band`, `generation` | `connected_ues`, `prb_dl_pct`, `sinr_db`, `rsrp_dbm`, `power_w`, `dl_throughput_mbps`, `packet_loss_pct`, `cqi`, `bler_pct`, `latency_ms`, `handover_count`, `coverage_radius_m` | DU simulator |
| `du_kpi` | `du_id`, `cu_id` | `cpu_utilisation_pct`, `memory_utilisation_pct`, `f1_msg_rate`, `fronthaul_latency_us`, `active_cells`, `total_connected_ues` | DU simulator |
| `cu_kpi` | `cu_id` | `active_dus`, `total_pdcp_throughput_mbps`, `handover_success_rate`, `rrc_connected_ues`, `signalling_load_pct` | CU simulator |
| `core_kpi` | `core_id` | `registered_ues`, `active_pdp_sessions`, `amf_cpu_pct`, `smf_cpu_pct`, `upf_throughput_mbps`, `authentication_rate` | Core simulator |
| `ue_mobility` | `ue_id`, `cell_id` | `event_type`, `target_cell_id`, `rsrp_dbm`, `ho_duration_ms`, `distance_from_cell_m` | DU simulator |
| `ue_usage` | `ue_id`, `cell_id` | `slice_type`, `dl_bytes`, `ul_bytes`, `latency_ms`, `jitter_ms`, `packet_loss_pct` | DU simulator |
| `alerts` | `cell_id`, `du_id`, `alert_type`, `severity` | `message`, `prb_pct`, `sinr_db`, `ai_confidence` | KPI agent |
| `son_actions` | `cell_id`, `du_id`, `action_type` | `from_du`, `to_du`, `confidence`, `details` | KPI agent |
| `topology_event` | `cell_id`, `event_type` | `from_du`, `to_du`, `triggered_by` | Controller |

## Grafana dashboards

5 dashboards provisioned from `dev-env/grafana/provisioning/dashboards/default.yaml` on first boot:

| Dashboard | File | Key panels |
|---|---|---|
| Network Overview | `network_overview.json` | Total UEs (stat), active cells (stat), avg DL Mbps (stat), avg SINR dB (stat), overloaded cells (stat), total power W (stat); UE count / PRB / SINR / power timeseries |
| Cell KPI | `cell_kpi.json` | Per-cell PRB %, SINR dB, RSRP dBm, throughput Mbps, power W, CQI, BLER + latency; generation filter variable |
| UE Analytics | `ue_analytics.json` | UE slice distribution (donut), latency / jitter / bytes by slice, HO event rate and duration timeseries |
| SON Alerts | `son_alerts.json` | CRITICAL / WARNING counts (stat), SON action counts by type, AI confidence timeseries, SON action log table |
| DU/CU Performance | `du_cu_performance.json` | DU CPU / memory / fronthaul latency / F1 msg rate; CU PDCP throughput; core registered UEs; UPF throughput |

## Simulator design

### DU simulator (`dev-env/simulators/du/du_simulator.py`)

- Simulates one DU managing multiple cells (cell list read from topology.json on each poll)
- Generates realistic 4G + 5G KPIs using COST-231-Hata for coverage radius; HOURLY_LOAD for diurnal UE variation
- UE pool: UEs randomly associate to cells proportional to max_ues; handovers simulated when RSRP < threshold
- Writes `cell_kpi`, `du_kpi`, `ue_mobility`, `ue_usage` every `PUSH_INTERVAL_SEC` (10 s)
- Detects new/removed cells from topology.json poll — no restart required

### CU simulator (`dev-env/simulators/cu/cu_simulator.py`)

- Aggregates across DUs registered under this CU (read from topology.json)
- Computes CU-level KPIs from DU totals (PDCP throughput, RRC UEs, signalling load)
- Writes `cu_kpi` every `PUSH_INTERVAL_SEC`

### Core simulator (`dev-env/simulators/core/core_simulator.py`)

- Single instance simulating AMF + SMF + UPF
- MAX_UES_TOTAL = 1,625,000 (capacity ceiling; registered_ues << this in the 30-cell scenario)
- HOURLY_LOAD: 24-element list of fractional load factors (0.05 at 3 am to 1.00 at 7 pm)
- Writes `core_kpi` every `PUSH_INTERVAL_SEC`

## Running the stack

```bash
cd dev-env
docker compose up --build          # start everything (first build ~4 min)
docker compose down -v             # stop and wipe volumes (InfluxDB data + Grafana)
docker compose logs -f kpi-agent   # tail a specific service
```

`.env` file required in `dev-env/` — see `.env.example` for all required variables.
