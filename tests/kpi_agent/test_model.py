"""Tests for agents/kpi_agent/model.py — LSTM classifier and normalisation."""
import pytest

torch = pytest.importorskip("torch")   # skip entire module if torch unavailable

from model import KPIClassifier, normalise, FEATURE_NORM, N_FEATURES, N_CLASSES, SEQ_LEN


# ── normalise ────────────────────────────────────────────────────────────────

def test_normalise_min_values_yield_zero():
    raw = [mn for mn, _ in FEATURE_NORM]
    result = normalise(raw)
    for i, v in enumerate(result):
        assert abs(v) < 1e-9, f"Feature {i}: expected 0.0, got {v}"


def test_normalise_max_values_yield_one():
    raw = [mn + rng for mn, rng in FEATURE_NORM]
    result = normalise(raw)
    for i, v in enumerate(result):
        assert abs(v - 1.0) < 1e-9, f"Feature {i}: expected 1.0, got {v}"


def test_normalise_midpoint():
    raw = [mn + rng / 2 for mn, rng in FEATURE_NORM]
    result = normalise(raw)
    for i, v in enumerate(result):
        assert abs(v - 0.5) < 1e-9, f"Feature {i}: expected 0.5, got {v}"


def test_normalise_length():
    raw = [0.0] * N_FEATURES
    assert len(normalise(raw)) == N_FEATURES


# ── KPIClassifier ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def model():
    m = KPIClassifier(hidden=32)
    m.eval()
    return m


def test_classifier_output_shape(model):
    x = torch.zeros(1, SEQ_LEN, N_FEATURES)
    with torch.no_grad():
        out = model(x)
    assert out.shape == (1, N_CLASSES), f"Expected (1, {N_CLASSES}), got {out.shape}"


def test_classifier_batch_output_shape(model):
    batch = 8
    x = torch.randn(batch, SEQ_LEN, N_FEATURES)
    with torch.no_grad():
        out = model(x)
    assert out.shape == (batch, N_CLASSES)


def test_classifier_output_finite(model):
    x = torch.randn(4, SEQ_LEN, N_FEATURES)
    with torch.no_grad():
        out = model(x)
    assert torch.isfinite(out).all(), "Model produced NaN or Inf logits"


def test_classifier_softmax_sums_to_one(model):
    import torch.nn.functional as F
    x = torch.randn(3, SEQ_LEN, N_FEATURES)
    with torch.no_grad():
        logits = model(x)
        probs  = F.softmax(logits, dim=-1)
    for i in range(probs.shape[0]):
        total = probs[i].sum().item()
        assert abs(total - 1.0) < 1e-5, f"Sample {i}: softmax sums to {total}"
