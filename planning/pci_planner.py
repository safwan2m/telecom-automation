"""
PCI (Physical Cell ID) planner — collision and confusion free assignment.

3GPP TS 36.211 / 38.211:
  PCI range: 0–1007
  Collision:  two adjacent cells share the same PCI                → forbidden
  Confusion:  two adjacent cells share the same PCI mod 3 value    → should avoid

Algorithm: greedy graph-colouring using adjacency within radio range.
"""

from __future__ import annotations
from placement import haversine_km

ADJACENCY_RADIUS_KM = 3.0   # cells within this range are RF neighbours
PCI_MAX             = 1007


def build_adjacency(cells: list[dict]) -> dict[str, set[str]]:
    """Return {cell_id: {neighbour_cell_ids}} for all RF neighbours."""
    adj: dict[str, set[str]] = {c["cell_id"]: set() for c in cells}
    for i, a in enumerate(cells):
        for b in cells[i + 1:]:
            dist = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            if dist <= ADJACENCY_RADIUS_KM:
                adj[a["cell_id"]].add(b["cell_id"])
                adj[b["cell_id"]].add(a["cell_id"])
    return adj


def assign_pcis(cells: list[dict]) -> dict[str, int]:
    """
    Assign a PCI to each cell such that:
    - No two neighbours share the same PCI (collision free).
    - No two neighbours share the same PCI mod 3 (confusion free).

    Returns {cell_id: pci}.
    """
    adj  = build_adjacency(cells)
    pcis: dict[str, int] = {}

    # Process cells in descending degree order (most-constrained first)
    order = sorted(cells, key=lambda c: len(adj[c["cell_id"]]), reverse=True)

    for cell in order:
        cid      = cell["cell_id"]
        nbr_pcis = {pcis[n] for n in adj[cid] if n in pcis}
        nbr_mod3 = {pcis[n] % 3 for n in adj[cid] if n in pcis}

        # Find the smallest PCI that avoids collision AND confusion
        candidate = 0
        while candidate <= PCI_MAX:
            if candidate not in nbr_pcis and candidate % 3 not in nbr_mod3:
                break
            candidate += 1

        pcis[cid] = candidate % (PCI_MAX + 1)

    return pcis


def validate_plan(cells: list[dict], pcis: dict[str, int]) -> list[str]:
    """Return list of violation descriptions; empty list means clean plan."""
    adj        = build_adjacency(cells)
    violations = []
    checked    = set()

    for cell in cells:
        cid = cell["cell_id"]
        for nbr_id in adj[cid]:
            pair = tuple(sorted([cid, nbr_id]))
            if pair in checked:
                continue
            checked.add(pair)
            p1, p2 = pcis.get(cid), pcis.get(nbr_id)
            if p1 is None or p2 is None:
                continue
            if p1 == p2:
                violations.append(f"COLLISION: {cid}(PCI={p1}) ↔ {nbr_id}(PCI={p2})")
            elif p1 % 3 == p2 % 3:
                violations.append(f"CONFUSION: {cid}(PCI={p1},mod3={p1%3}) ↔ {nbr_id}(PCI={p2},mod3={p2%3})")

    return violations
