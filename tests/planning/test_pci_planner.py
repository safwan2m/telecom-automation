"""Tests for agents/planning/pci_planner.py — greedy PCI colouring."""
import pytest
from pci_planner import build_adjacency, assign_pcis, validate_plan, ADJACENCY_RADIUS_KM


def _cell(cell_id, lat, lon):
    return {"cell_id": cell_id, "lat": lat, "lon": lon}


# Place 4 cells all within 1 km of each other (complete graph)
DENSE_CELLS = [
    _cell("A", 13.000, 77.570),
    _cell("B", 13.001, 77.571),
    _cell("C", 13.002, 77.570),
    _cell("D", 13.001, 77.569),
]

# Two cells 100 km apart (no edges)
SPARSE_CELLS = [
    _cell("X", 13.000, 77.000),
    _cell("Y", 14.000, 78.000),
]


# ── build_adjacency ──────────────────────────────────────────────────────────

def test_adjacency_co_located_are_neighbors():
    cells = [_cell("A", 13.0, 77.5), _cell("B", 13.0, 77.5)]
    adj = build_adjacency(cells)
    assert "B" in adj["A"]
    assert "A" in adj["B"]


def test_adjacency_distant_not_neighbors():
    adj = build_adjacency(SPARSE_CELLS)
    assert len(adj["X"]) == 0
    assert len(adj["Y"]) == 0


def test_adjacency_within_radius_are_neighbors():
    # DENSE_CELLS are all <1 km apart, well within ADJACENCY_RADIUS_KM
    adj = build_adjacency(DENSE_CELLS)
    for cell in DENSE_CELLS:
        cid = cell["cell_id"]
        assert len(adj[cid]) == len(DENSE_CELLS) - 1


def test_adjacency_empty():
    assert build_adjacency([]) == {}


# ── assign_pcis ──────────────────────────────────────────────────────────────

def test_assign_pcis_no_collisions_dense():
    """The fix we implemented: dense graph must produce zero collisions."""
    pcis = assign_pcis(DENSE_CELLS)
    result = validate_plan(DENSE_CELLS, pcis)
    assert result["collisions"] == [], f"Unexpected collisions: {result['collisions']}"


def test_assign_pcis_no_collisions_sparse():
    """Isolated cells each get their own PCI (greedy starts at 0)."""
    pcis = assign_pcis(SPARSE_CELLS)
    result = validate_plan(SPARSE_CELLS, pcis)
    assert result["collisions"] == []


def test_assign_pcis_all_cells_assigned():
    pcis = assign_pcis(DENSE_CELLS)
    assert set(pcis.keys()) == {c["cell_id"] for c in DENSE_CELLS}


def test_assign_pcis_pci_in_valid_range():
    pcis = assign_pcis(DENSE_CELLS)
    for cid, pci in pcis.items():
        assert 0 <= pci <= 1007, f"{cid} got PCI={pci} outside valid range"


def test_assign_pcis_empty():
    assert assign_pcis([]) == {}


def test_assign_pcis_single_cell():
    cells = [_cell("SOLO", 13.0, 77.5)]
    pcis = assign_pcis(cells)
    assert pcis["SOLO"] == 0   # greedy starts at 0, no neighbours


def test_assign_pcis_full_30_cell_network():
    """Regression: the original bug produced collisions on the 30-cell deployment."""
    from placement import CANDIDATE_CELLS
    # Expand each site to 3 sectors (simulate the real 30-cell network)
    cells = []
    for i, base in enumerate(CANDIDATE_CELLS):
        for s in range(3):
            cells.append({
                **base,
                "cell_id": f"{base['cell_id'][:-2]}{s+1:02d}",
            })
    pcis = assign_pcis(cells)
    result = validate_plan(cells, pcis)
    assert result["collisions"] == [], \
        f"{len(result['collisions'])} collision(s) in 30-cell network"


# ── validate_plan ─────────────────────────────────────────────────────────────

def test_validate_plan_clean():
    pcis = assign_pcis(DENSE_CELLS)
    result = validate_plan(DENSE_CELLS, pcis)
    assert isinstance(result, dict)
    assert "collisions" in result
    assert "confusions" in result
    assert result["collisions"] == []


def test_validate_plan_detects_collision():
    """Manually assign the same PCI to two adjacent cells — must be detected."""
    cells = [_cell("A", 13.0, 77.5), _cell("B", 13.0, 77.5)]
    bad_pcis = {"A": 5, "B": 5}   # collision
    result = validate_plan(cells, bad_pcis)
    assert len(result["collisions"]) == 1
    assert "COLLISION" in result["collisions"][0]


def test_validate_plan_detects_confusion():
    cells = [_cell("A", 13.0, 77.5), _cell("B", 13.0, 77.5)]
    # PCI 0 (mod3=0) and PCI 3 (mod3=0) → confusion
    confused_pcis = {"A": 0, "B": 3}
    result = validate_plan(cells, confused_pcis)
    assert len(result["confusions"]) == 1
    assert "CONFUSION" in result["confusions"][0]
