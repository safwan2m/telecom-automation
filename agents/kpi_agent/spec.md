# KPI Monitoring Agent — Specification

Background process (no HTTP port). Polls InfluxDB on a fixed cadence, maintains a per-cell sliding-window feature buffer, classifies cell state with a BiLSTM model, and dispatches autonomous SON actions without operator involvement.

## Startup sequence

```
main()
  ├─ load_or_train()        load kpi_model.pt or train from scratch + save
  ├─ connect_influx()       up to 19 attempts × 6 s delay; raises on failure
  ├─ write_api = client.write_api(SYNCHRONOUS)
  ├─ buffers = defaultdict(lambda: deque(maxlen=SEQ_LEN))   per-cell feature history
  └─ while True:
        cells = query_latest_cell_kpis(client)
        if cells: analyse(model, cells, buffers, write_api, cycle)
        cycle += 1
        time.sleep(POLL_SEC)
```

## KPI Flux query

`query_latest_cell_kpis()` — measurement `cell_kpi`, `range(start: -3m)`:

```flux
filter fields: prb_dl_pct | sinr_db | connected_ues | power_w |
               packet_loss_pct | dl_throughput_mbps | cqi | bler_pct | latency_ms
|> last()
|> pivot(rowKey: ["cell_id","area","du_id","cu_id"], columnKey: ["_field"], valueColumn: "_value")
```

**Null/missing field defaults** (applied when value is `None` or absent):

| Field | Default |
|---|---|
| `prb_dl_pct` | 0.0 |
| `sinr_db` | 20.0 |
| `connected_ues` | 0.0 |
| `power_w` | 0.0 |
| `packet_loss_pct` | 0.0 |
| `dl_throughput_mbps` | 0.0 |
| `cqi` | 10.0 |
| `bler_pct` | 1.0 |
| `latency_ms` | 15.0 |

Returns empty list on query failure (logged as ERROR).

## Feature extraction

`extract_features(cell_dict) → list[float]` — exactly 9 values in this order:

```
[prb_dl_pct, sinr_db, connected_ues, power_w, packet_loss_pct,
 dl_throughput_mbps, cqi, bler_pct, latency_ms]
```

Appended raw (un-normalised) to each cell's `deque(maxlen=SEQ_LEN)` buffer. Normalisation happens inside `infer()`.

## Analysis cycle — `analyse()`

```
build du_avg: {du_id: mean(prb_dl_pct)} across all cells this cycle

per cell:
  feats = extract_features(cell)
  buffers[cell_id].append(feats)
  has_history = len(buffer) >= SEQ_LEN

  if has_history:
      cls, conf = infer(model, buffer)     source = "AI"
  else:
      cls, conf = rule_classify(cell)      source = "RULE", conf = -1.0

  act = (source == "RULE") or (conf >= MIN_CONFIDENCE)

  dispatch SON action for cls (see below)

log: "Cycle N | cells=X | normal=X | overload=X | ..."
```

## Rule-based fallback (while buffer fills)

Applied in priority order — first match wins:

| Priority | Condition | Class |
|---|---|---|
| 1 | `prb_dl_pct > OVERLOAD_PRB` | OVERLOAD |
| 2 | `prb_dl_pct < UNDERLOAD_PRB` | UNDERLOAD |
| 3 | `sinr_db < SINR_MIN_DB` | SINR_LOW |
| 4 | `power_w > POWER_WASTE_W` AND `connected_ues < POWER_WASTE_UE` | POWER_WASTE |
| 5 | (none) | NORMAL |

## Congestion score

`congestion_score(cell) → float [0, 1]` — multi-factor severity index:

```
PRB     = min(prb_dl_pct / 100,  1.0)
SINR    = max(0,  1 − sinr_db / 25)       # 25 dB = no contribution
BLER    = min(bler_pct / 20,     1.0)      # 20% = max contribution
LATENCY = min(latency_ms / 150,  1.0)     # 150 ms = max contribution

score = 0.40×PRB + 0.20×SINR + 0.20×BLER + 0.20×LATENCY   (rounded to 3 dp)
```

Used to assess severity before triggering OVERLOAD actions and for pre-emptive steering.

## Cooldown gate

`_cell_cooldown: dict[str, float]` — maps `cell_id → last_action_timestamp`.

`_is_cooling_down(cell_id)`: returns `True` if `now − last_action < COOLDOWN_SEC`.  
`_mark_action(cell_id)`: sets `last_action = time.time()`.

Applied before all SON write/move actions **except UNDERLOAD** (no cooldown on UNDERLOAD).

## SON action dispatch

### Class 0 — NORMAL

No alert written. If `congestion_score > 0.65` AND not cooling down:
- Write `son_actions`: action_type=`PRE_EMPTIVE_STEER`, message includes score, PRB, BLER, latency.
- Mark cooldown.

### Class 1 — OVERLOAD

If `act` AND not cooling down:

1. **Always write alert**: `alerts` severity=`WARNING`, type=`OVERLOAD`, metric=prb_dl_pct, threshold=OVERLOAD_PRB.

2. **1st choice — neighbor steering** (preferred; no topology change):
   - `GET /neighbors/{cell_id}` (3 s timeout, exceptions swallowed).
   - Find neighbor where `prb_dl_pct < OVERLOAD_PRB − 25` (i.e., has ≥25% headroom) with lowest PRB.
   - If found: write `son_actions` action_type=`NEIGHBOR_LOAD_STEER`. Mark cooldown. Set `neighbor_steered = True`.

3. **2nd choice — DU move** (only when `neighbor_steered == False` AND `score > 0.75`):
   - Find lightest DU where `du_avg < OVERLOAD_PRB − 20` (different from current DU).
   - Call `POST /move/cell` (5 s timeout).
   - Write `alerts` severity=`INFO`, type=`LOAD_BALANCE` (outcome message).
   - Write `son_actions` action_type=`LOAD_BALANCE`. Mark cooldown.

### Class 2 — UNDERLOAD

If `act` (no cooldown check):
- Write `alerts` severity=`INFO`, type=`UNDERLOAD`, metric=prb_dl_pct, threshold=UNDERLOAD_PRB.
- Write `son_actions` action_type=`TRAFFIC_STEER`: recommend handing remaining UEs to cells on the least-loaded OTHER DU (by `du_avg`) to enable sleep/DTX.

### Class 3 — SINR_LOW

If `act`:
- Write `alerts` severity=`CRITICAL`, type=`SINR_DEGRADATION`, metric=sinr_db, threshold=SINR_MIN_DB.
- `POST /son/pci-reopt {cell_id, du_id}` (3 s timeout, best-effort — all exceptions swallowed; no cooldown on HTTP call).
- Write `son_actions` action_type=`PCI_REOPT_REQUEST`.

*(Note: Controller has no `/son/pci-reopt` route yet — always 404s. The SON write still records the intent.)*

### Class 4 — POWER_WASTE

If `act`:
- Write `alerts` severity=`WARNING`, type=`POWER_WASTE`, metric=power_w, threshold=POWER_WASTE_W.
- Write `son_actions` action_type=`DTX_RECOMMEND`: message includes current watts, UE count, and estimated saving at 35% (i.e., `power_w × 0.35`).

## InfluxDB write schemas

### `alerts` measurement

| Kind | Name | Value |
|---|---|---|
| tag | `severity` | `CRITICAL` / `WARNING` / `INFO` |
| tag | `cell_id` | e.g. `MLS_RWS_01` |
| tag | `du_id` | e.g. `DU-MLS-1` |
| tag | `alert_type` | `OVERLOAD` / `UNDERLOAD` / `SINR_DEGRADATION` / `POWER_WASTE` / `LOAD_BALANCE` |
| field | `message` | human-readable description string |
| field | `metric_value` | float — the triggering KPI value |
| field | `threshold` | float — the threshold it crossed |
| field | `ai_confidence` | float — softmax confidence (−1.0 for rule-based) |

### `son_actions` measurement

| Kind | Name | Value |
|---|---|---|
| tag | `cell_id` | — |
| tag | `du_id` | — |
| tag | `action_type` | `PRE_EMPTIVE_STEER` / `NEIGHBOR_LOAD_STEER` / `LOAD_BALANCE` / `TRAFFIC_STEER` / `PCI_REOPT_REQUEST` / `DTX_RECOMMEND` |
| field | `message` | human-readable description string |
| field | `confidence` | float — softmax confidence (−1.0 for rule-based) |

Both measurements use `SYNCHRONOUS` write mode.

## BiLSTM model (`model.py` — `KPIClassifier`)

```
Input: (batch, SEQ_LEN=6, N_FEATURES=9)
  └─ nn.LSTM(input=9, hidden=64, num_layers=2, bidirectional=True, dropout=0.25, batch_first=True)
       └─ take last timestep: out[:, -1, :]  shape (batch, 128)   # 64×2 bidirectional
            └─ nn.Linear(128→64) → ReLU → Dropout(0.25) → nn.Linear(64→5)
Output: logits (batch, 5)
```

Classes: `0=NORMAL`, `1=OVERLOAD`, `2=UNDERLOAD`, `3=SINR_LOW`, `4=POWER_WASTE`.

### Inference

`infer(model, buffer) → (class_idx, confidence)`:
```python
x = tensor([normalise(step) for step in buffer]).unsqueeze(0)  # (1, 6, 9)
logits = model(x)
probs  = softmax(logits, dim=1)[0]
cls    = probs.argmax()
conf   = probs[cls]
```

### Feature normalisation (`normalise`)

Linear min-max to `[0, 1]` using fixed per-feature ranges (covers 4G and 5G hardware):

| Feature | min | range |
|---|---|---|
| `prb_dl_pct` | 0.0 | 100.0 |
| `sinr_db` | −5.0 | 35.0 |
| `connected_ues` | 0.0 | 800.0 |
| `power_w` | 0.0 | 1200.0 |
| `packet_loss_pct` | 0.0 | 5.0 |
| `dl_throughput_mbps` | 0.0 | 4000.0 |
| `cqi` | 0.0 | 15.0 |
| `bler_pct` | 0.0 | 30.0 |
| `latency_ms` | 0.0 | 500.0 |

`normalise(raw) = [(v − min) / range  for v, (min, range) in zip(raw, FEATURE_NORM)]`

## Training (`train.py` — `train_model`)

### Dataset

5,000 sequences total (`np.random.seed(0)`). Each class generates `n` sequences split evenly between 5G NR and 4G LTE sub-profiles:

| Class | Count | % | Label index |
|---|---|---|---|
| NORMAL | 3500 | 70% | 0 |
| OVERLOAD | 750 | 15% | 1 |
| UNDERLOAD | 400 | 8% | 2 |
| SINR_LOW | 250 | 5% | 3 |
| POWER_WASTE | 100 | 2% | 4 |

Each sequence is generated by `_make_sequence(means, stds)`:
```
base = means + randn × stds × 0.5            # per-sequence baseline offset
for each of SEQ_LEN steps:
    step = base + randn × stds × 0.15        # per-step noise
    step = clip(step, 0, ∞)                  # no negatives
    append normalise(step)
    base += randn × stds × 0.05              # slow temporal drift
```

**5G NR sub-profiles** (64T64R mMIMO, n78 3500 MHz) — `(means, stds)` per class:

| Class | prb | sinr | ues | power | pkt_loss | tput | cqi | bler | lat |
|---|---|---|---|---|---|---|---|---|---|
| NORMAL | 55±14 | 20±4 | 350±130 | 520±200 | 0.05±0.05 | 1400±500 | 11±2 | 1.5±0.8 | 12±4 |
| OVERLOAD | 94±3 | 11±3 | 720±70 | 940±55 | 0.85±0.40 | 3100±200 | 7±2 | 8.0±2.5 | 38±8 |
| UNDERLOAD | 9±4 | 24±5 | 20±8 | 330±120 | 0.01±0.01 | 190±100 | 14±1 | 0.3±0.2 | 9±2 |
| SINR_LOW | 54±20 | 1±2 | 290±100 | 580±200 | 1.60±0.80 | 720±300 | 3±2 | 12.0±3.0 | 45±12 |
| POWER_WASTE | 13±5 | 24±5 | 8±3 | 880±100 | 0.01±0.01 | 145±60 | 14±1 | 0.2±0.1 | 9±2 |

**4G LTE sub-profiles** (4T4R macro, B3 1800 MHz / B40 2300 MHz):

| Class | prb | sinr | ues | power | pkt_loss | tput | cqi | bler | lat |
|---|---|---|---|---|---|---|---|---|---|
| NORMAL | 48±12 | 22±4 | 130±50 | 120±45 | 0.04±0.04 | 110±40 | 10±2 | 1.2±0.6 | 15±5 |
| OVERLOAD | 92±4 | 12±3 | 230±20 | 195±10 | 0.75±0.35 | 140±10 | 6±2 | 7.0±2.0 | 52±12 |
| UNDERLOAD | 8±3 | 25±5 | 10±4 | 65±20 | 0.01±0.01 | 18±8 | 13±1 | 0.2±0.1 | 11±3 |
| SINR_LOW | 50±18 | 0±2 | 120±45 | 140±50 | 1.40±0.70 | 85±30 | 3±2 | 10.0±2.5 | 60±15 |
| POWER_WASTE | 10±4 | 26±5 | 5±2 | 175±25 | 0.01±0.01 | 12±5 | 13±1 | 0.1±0.05 | 10±2 |

### Training loop

```
80/20 train/val split (random permutation)
WeightedRandomSampler — weight = 1/class_size per sample (balances mini-batches)
DataLoader: BATCH_SIZE=256, sampler for train; plain for val
Model: KPIClassifier()
Loss:  CrossEntropyLoss
Opt:   Adam(lr=1e-3)
Sched: CosineAnnealingLR(T_max=EPOCHS=60)
Grad clip: clip_grad_norm_(1.0) each step
EPOCHS=60; val accuracy logged every 10 epochs and at final epoch
Per-class accuracy breakdown logged after training
torch.save(model.state_dict(), save_path)
```

`load_or_train()`: loads with `torch.load(MODEL_PATH, map_location="cpu", weights_only=True)` if file exists; otherwise calls `train_model(MODEL_PATH)`. Sets `model.eval()` before returning.

## Rule-based fallback thresholds

| Threshold | Default | Env var override |
|---|---|---|
| Overload PRB | 85% | `OVERLOAD_PRB_PCT` |
| Underload PRB | 20% | `UNDERLOAD_PRB_PCT` |
| Min SINR | 5 dB | `SINR_MIN_DB` |
| Power waste W | 500 W | `POWER_WASTE_W` |
| Power waste min UEs | 15 | `POWER_WASTE_MIN_UES` |

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `INFLUX_URL` | `http://influxdb:8086` | KPI polling + alert/SON writes |
| `INFLUX_TOKEN` | `telecom-super-secret-auth-token-2026` | InfluxDB auth |
| `INFLUX_ORG` | `telecom` | InfluxDB org |
| `INFLUX_BUCKET` | `telecom_metrics` | InfluxDB bucket |
| `CONTROLLER_URL` | `http://controller:8080` | `move_cell` + neighbor lookup + pci-reopt |
| `POLL_INTERVAL_SEC` | `10` (code) / `30` (Docker) | Poll cadence |
| `MODEL_PATH` | `kpi_model.pt` | LSTM weights file |
| `MIN_CONFIDENCE` | `0.70` | Min softmax confidence to act on AI prediction |
| `SON_COOLDOWN_SEC` | `300` | Min seconds between SON actions on the same cell |

## File structure

| File | Purpose |
|---|---|
| `kpi_agent.py` | Poll loop, Flux query, feature extraction, congestion scoring, SON dispatcher |
| `model.py` | `KPIClassifier` — 2-layer BiLSTM + MLP head; `normalise()`; `FEATURE_NORM`; `LABELS`; `SEQ_LEN=6`; `N_FEATURES=9` |
| `train.py` | Synthetic dataset generation + LSTM training; `train_model(save_path)` |
| `kpi_model.pt` | Saved model weights (generated on first boot if absent) |
