"""
LSTM-based KPI classifier for 5G cell anomaly detection.

Input : sequence of 6 KPI readings per cell  (shape: batch × SEQ_LEN × N_FEATURES)
Output: one of 5 cell-state classes

Classes
-------
0  NORMAL        — no action needed
1  OVERLOAD      — PRB consistently high → try to move cell
2  UNDERLOAD     — PRB consistently very low → sleep candidate
3  SINR_LOW      — signal quality degraded → raise critical alert
4  POWER_WASTE   — high power draw with very few connected UEs
"""

import torch
import torch.nn as nn

SEQ_LEN    = 6     # timesteps of history fed to the model  (6 × 10 s = 60 s)
N_FEATURES = 6     # features extracted per timestep
N_CLASSES  = 5

LABELS = {
    0: "NORMAL",
    1: "OVERLOAD",
    2: "UNDERLOAD",
    3: "SINR_LOW",
    4: "POWER_WASTE",
}

# Min-value and range used for [0,1] normalisation
# Ranges cover both 4G (lower end) and 5G NR (upper end) hardware
FEATURE_NORM: list[tuple[float, float]] = [
    (0.0,   100.0),    # prb_dl_pct          → 0–100 %
    (-5.0,  35.0),     # sinr_db             → –5 to +30 dB
    (0.0,   800.0),    # connected_ues       → 0–800 (Nokia 5G NR max)
    (0.0,   1200.0),   # power_w             → 0–1200 W (5G 64T64R full load)
    (0.0,   5.0),      # packet_loss_pct     → 0–5 %
    (0.0,   4000.0),   # dl_throughput_mbps  → 0–4000 Mbps (5G NR peak)
]


def normalise(raw: list[float]) -> list[float]:
    return [(v - mn) / rng for v, (mn, rng) in zip(raw, FEATURE_NORM)]


class KPIClassifier(nn.Module):
    """
    Two-layer bidirectional LSTM followed by a two-layer MLP classifier.
    Bidirectional allows the model to see both early-warning trends and
    instantaneous spikes within the 60-second window.
    """

    def __init__(self, hidden: int = 64):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size  = N_FEATURES,
            hidden_size = hidden,
            num_layers  = 2,
            batch_first = True,
            dropout     = 0.25,
            bidirectional = True,
        )
        self.head = nn.Sequential(
            nn.Linear(hidden * 2, 64),   # × 2 because bidirectional
            nn.ReLU(),
            nn.Dropout(0.25),
            nn.Linear(64, N_CLASSES),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (batch, seq_len, features)
        out, _ = self.lstm(x)
        return self.head(out[:, -1, :])   # last timestep's combined state
