# O1 Query Flow — "Optimize the network for load balancing."

> Multi-tool flow: Gemini issues `get_alerts` then iteratively calls `move_cell`
> for each OVERLOAD cell, moving it to the lightest available DU.

```mermaid
flowchart TD
    A([User]) -->|"Optimize the network\nfor load balancing."| B[chat.py\nlocalhost:8082]
    B -->|HTTP POST /chat| C[Orchestrator\nport 8082]
    C -->|System prompt + snapshot| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Tool call 1:\nget_alerts\n{alert_type: OVERLOAD}| C
    C -->|GET /alerts?alert_type=OVERLOAD| E[Controller\nport 8080]
    E -->|Flux query: OVERLOAD alerts| G[(InfluxDB\nalerts measurement)]
    G -->|Overloaded cell list| E
    E -->|Alert list| C
    C -->|Tool result 1:\nOVERLOAD cells| D

    D -->|"Identify lightest DU\nfor each overloaded cell"| D

    D -->|Tool call 2:\nmove_cell\n{cell_id, target_du_id}| C
    C -->|POST /move/cell| E2[Controller\nport 8080]
    E2 -->|Write| F2[(topology.json\natomic update)]
    F2 -->|Updated assignment| E2
    E2 -->|Move confirmed| C
    C -->|Tool result 2:\ncell moved| D

    D -->|"Repeat move_cell\nfor each overloaded cell"| D

    D -->|Natural language reply:\nlist of moves made,\nbefore/after DU loads| C
    C -->|HTTP response| B
    B -->|Print response| A
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1–3 | User → Orchestrator | Standard chat path |
| 4 | Gemini LLM | Tool call 1: `get_alerts(alert_type="OVERLOAD")` |
| 5 | Orchestrator | `GET /alerts?alert_type=OVERLOAD` on Controller |
| 6 | Controller | Queries InfluxDB: OVERLOAD alerts from KPI Agent |
| 7 | Controller | Returns list of overloaded cells + their current DU |
| 8 | Orchestrator | Returns alert list to Gemini |
| 9 | Gemini LLM | Identifies lightest DU for each overloaded cell |
| 10 | Gemini LLM | Tool call 2: `move_cell(cell_id=X, target_du_id=Y)` |
| 11 | Orchestrator | `POST /move/cell` on Controller |
| 12 | Controller | Atomically updates `topology.json` (write to .tmp, rename) |
| 13 | Controller | DU simulators pick up change within `TOPO_POLL_SEC` (5 s) |
| 14 | Controller | Returns move confirmation |
| 15 | Gemini LLM | Repeats step 10–14 for each overloaded cell |
| 16 | Gemini LLM | Generates summary: cells moved, before/after DU load |
| 17–18 | Orchestrator → User | Returns reply |

## Multi-tool loop

```
get_alerts → [cell_A on DU-MLS-3, cell_B on DU-MLS-1]

  move_cell(cell_A, DU-MLS-2)  → topology.json updated
  move_cell(cell_B, DU-MLS-3)  → topology.json updated

DU simulators reload topology within 5 s → KPIs normalise on next poll
```

## topology.json update mechanism

```
Controller writes: topology.json.tmp  →  rename to topology.json  (atomic)
DU simulators poll every TOPO_POLL_SEC (5 s)  →  reconfigure cells live
```
