# Dataset Reference & Synthetic Generation

## Reference Datasets

The following Kaggle datasets informed KPI value ranges, column naming, and class distributions:

1. Cellular network performance data: https://www.kaggle.com/datasets/suraj520/cellular-network-performance-data
2. 5G Network KPI dataset: https://www.kaggle.com/datasets/srikumarnayak/5g-network-kpi-dataset
3. Telecom Network dataset: https://www.kaggle.com/datasets/praveenaparimi/telecom-network-dataset
4. Cellular Network analysis dataset: https://www.kaggle.com/datasets/suraj520/cellular-network-analysis-dataset

## Synthetic Dataset Generator

`dataset_generator.py` (same directory) produces a synthetic CSV dataset for the Malleswaram 30-cell deployment.

```bash
python dataset_generator.py --days 70 --seed 42 --out malleswaram_kpi.csv
```

### Output

| Property | Value |
|---|---|
| Rows | 50,400 (70 days × 24 hours × 30 cells) |
| Columns | 32 |
| Class distribution | 70% NORMAL / 15% OVERLOAD / 8% UNDERLOAD / 5% SINR_LOW / 2% POWER_WASTE |
| Weekend factor | 0.75× peak load on Saturday/Sunday |

### Columns

**Identity**: timestamp, cell_id, area, du_id, cu_id, vendor, generation, band, freq_mhz, pci, lat, lon

**Hardware**: antenna_config, tx_power_w, idle_power_w, peak_dl_mbps, max_ues

**KPIs**: connected_ues, prb_dl_pct, sinr_db, rsrp_dbm, rsrq_db, dl_throughput_mbps, ul_throughput_mbps, packet_loss_pct, power_w, cqi, mcs, bler_pct, latency_ms, jitter_ms, interference_dbm

**Label**: state (NORMAL / OVERLOAD / UNDERLOAD / SINR_LOW / POWER_WASTE)

### Live DU Simulator KPIs

The `du/du_simulator.py` generates the same extended KPI set in real time (every 10 s) and writes to InfluxDB `cell_kpi` measurement. All 7 new fields (rsrq_db, cqi, mcs, bler_pct, latency_ms, jitter_ms, interference_dbm) are physics-based and correlated with SINR/load.