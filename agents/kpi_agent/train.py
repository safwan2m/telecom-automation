"""
Synthetic data generator + LSTM trainer for the KPI classifier.

Improvements over the original:
  • 9 features (added cqi, bler_pct, latency_ms to match updated model + DU simulator)
  • Realistic class imbalance (70% NORMAL / 15% OVERLOAD / 8% UNDERLOAD /
    5% SINR_LOW / 2% POWER_WASTE) — matching real-world cellular network stats
  • Separate 4G vs 5G sub-profiles per class (5G NR has higher power, PRB, and tput)
  • Temporal correlation within sequences (slow drift between timesteps)

Run standalone:
    py train.py

Or import from kpi_agent.py:
    from train import train_model
    model = train_model("kpi_model.pt")
"""

import logging
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from model import KPIClassifier, SEQ_LEN, N_FEATURES, N_CLASSES, LABELS, normalise

log = logging.getLogger(__name__)

EPOCHS     = 60
BATCH_SIZE = 256
LR         = 1e-3

# ── Per-class, per-technology sample counts ───────────────────────────────────
# Proportions: 70 / 15 / 8 / 5 / 2 across the full dataset.
# Split evenly between 5G and 4G sub-profiles within each class.
# Total samples = 2 × sum(class_counts) for 5G + 4G.

CLASS_COUNTS = {
    "NORMAL":      3500,   # dominant class; 70%
    "OVERLOAD":     750,   # 15%
    "UNDERLOAD":    400,   # 8%
    "SINR_LOW":     250,   # 5%
    "POWER_WASTE":  100,   # 2%
}

# Feature order:
#   prb_dl_pct, sinr_db, connected_ues, power_w, packet_loss_pct,
#   dl_throughput_mbps, cqi, bler_pct, latency_ms

# Spec format: (means, stds) for each feature
# 5G NR profiles (64T64R mMIMO, n78 3500 MHz)
_5G_SPECS: dict[str, tuple[list, list]] = {
    "NORMAL": (
        [55,  20, 350,  520, 0.05, 1400,  11, 1.5,  12],
        [14,   4, 130,  200, 0.05,  500,   2, 0.8,   4],
    ),
    "OVERLOAD": (
        [94,  11, 720,  940, 0.85, 3100,   7, 8.0,  38],
        [ 3,   3,  70,   55, 0.40,  200,   2, 2.5,   8],
    ),
    "UNDERLOAD": (
        [ 9,  24,  20,  330, 0.01,  190,  14, 0.3,   9],
        [ 4,   5,   8,  120, 0.01,  100,   1, 0.2,   2],
    ),
    "SINR_LOW": (
        [54,   1, 290,  580, 1.60,  720,   3, 12.0,  45],
        [20,   2, 100,  200, 0.80,  300,   2,  3.0,  12],
    ),
    "POWER_WASTE": (
        [13,  24,   8,  880, 0.01,  145,  14, 0.2,   9],
        [ 5,   5,   3,  100, 0.01,   60,   1, 0.1,   2],
    ),
}

# 4G LTE profiles (4T4R macro, B3 1800 MHz / B40 2300 MHz)
_4G_SPECS: dict[str, tuple[list, list]] = {
    "NORMAL": (
        [48,  22, 130,  120, 0.04,  110,  10, 1.2,  15],
        [12,   4,  50,   45, 0.04,   40,   2, 0.6,   5],
    ),
    "OVERLOAD": (
        [92,  12, 230,  195, 0.75,  140,   6, 7.0,  52],
        [ 4,   3,  20,   10, 0.35,   10,   2, 2.0,  12],
    ),
    "UNDERLOAD": (
        [ 8,  25,  10,   65, 0.01,   18,  13, 0.2,  11],
        [ 3,   5,   4,   20, 0.01,    8,   1, 0.1,   3],
    ),
    "SINR_LOW": (
        [50,   0, 120,  140, 1.40,   85,   3, 10.0,  60],
        [18,   2,  45,   50, 0.70,   30,   2,  2.5,  15],
    ),
    "POWER_WASTE": (
        [10,  26,   5,  175, 0.01,   12,  13, 0.1,  10],
        [ 4,   5,   2,   25, 0.01,    5,   1, 0.05,  2],
    ),
}


def _make_sequence(means: list, stds: list) -> list[list[float]]:
    """SEQ_LEN timesteps with temporal drift so the LSTM learns trends."""
    base = np.array(means) + np.random.randn(N_FEATURES) * np.array(stds) * 0.5
    seq  = []
    for _ in range(SEQ_LEN):
        step = base + np.random.randn(N_FEATURES) * np.array(stds) * 0.15
        step = np.clip(step, 0, None)
        seq.append(normalise(step.tolist()))
        base += np.random.randn(N_FEATURES) * np.array(stds) * 0.05
    return seq


def generate_data() -> tuple[np.ndarray, np.ndarray]:
    np.random.seed(0)
    X, y = [], []

    for label_idx, label_name in enumerate(LABELS.values()):
        n = CLASS_COUNTS[label_name]
        means_5g, stds_5g = _5G_SPECS[label_name]
        means_4g, stds_4g = _4G_SPECS[label_name]

        # Split n evenly between 5G and 4G sub-profiles
        for i in range(n):
            if i % 2 == 0:
                seq = _make_sequence(means_5g, stds_5g)
            else:
                seq = _make_sequence(means_4g, stds_4g)
            X.append(seq)
            y.append(label_idx)

    X = np.array(X, dtype=np.float32)
    y = np.array(y, dtype=np.int64)
    idx = np.random.permutation(len(y))
    return X[idx], y[idx]


def train_model(save_path: str = "kpi_model.pt") -> KPIClassifier:
    total = sum(CLASS_COUNTS.values())
    log.info("Generating synthetic data: %d samples (realistic class distribution) …", total)
    for name, n in CLASS_COUNTS.items():
        log.info("  %-15s  %d samples (%.0f%%)", name, n, 100 * n / total)

    X, y = generate_data()

    split  = int(len(y) * 0.8)
    X_tr, X_va = torch.tensor(X[:split]), torch.tensor(X[split:])
    y_tr, y_va = torch.tensor(y[:split]), torch.tensor(y[split:])

    # Weighted sampler to handle class imbalance in training
    class_sizes  = [CLASS_COUNTS[LABELS[i]] for i in range(N_CLASSES)]
    sample_w     = [1.0 / class_sizes[yi] for yi in y_tr.tolist()]
    sampler      = torch.utils.data.WeightedRandomSampler(
        weights=torch.tensor(sample_w), num_samples=len(sample_w), replacement=True
    )

    train_dl = DataLoader(TensorDataset(X_tr, y_tr),
                          batch_size=BATCH_SIZE, sampler=sampler)
    val_dl   = DataLoader(TensorDataset(X_va, y_va), batch_size=BATCH_SIZE)

    model     = KPIClassifier()
    criterion = nn.CrossEntropyLoss()
    optimiser = torch.optim.Adam(model.parameters(), lr=LR)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimiser, T_max=EPOCHS)

    log.info("Training bidirectional LSTM (%d features) for %d epochs …",
             N_FEATURES, EPOCHS)
    for epoch in range(1, EPOCHS + 1):
        model.train()
        total_loss = 0.0
        for xb, yb in train_dl:
            optimiser.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimiser.step()
            total_loss += loss.item()
        scheduler.step()

        if epoch % 10 == 0 or epoch == EPOCHS:
            model.eval()
            correct = 0
            with torch.no_grad():
                for xb, yb in val_dl:
                    correct += (model(xb).argmax(1) == yb).sum().item()
            acc = correct / len(y_va) * 100
            log.info("  epoch %2d/%d — loss %.4f — val-acc %.1f%%",
                     epoch, EPOCHS, total_loss / len(train_dl), acc)

    torch.save(model.state_dict(), save_path)
    log.info("Model weights saved → %s", save_path)

    # Per-class accuracy breakdown
    model.eval()
    class_correct = [0] * N_CLASSES
    class_total   = [0] * N_CLASSES
    with torch.no_grad():
        for xb, yb in val_dl:
            preds = model(xb).argmax(1)
            for pred, true in zip(preds, yb):
                class_total[true.item()]   += 1
                class_correct[true.item()] += int(pred == true)
    for cls in range(N_CLASSES):
        if class_total[cls]:
            log.info("  %-12s  %3d / %3d  (%.0f%%)",
                     LABELS[cls],
                     class_correct[cls], class_total[cls],
                     class_correct[cls] / class_total[cls] * 100)

    return model


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    train_model()
