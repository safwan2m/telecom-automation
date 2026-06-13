# O2 Query Flow — "Move the most overloaded cell to a lighter DU."

> Multi-tool flow: `query_network` to rank cells by PRB load and DUs by avg load,
> then `move_cell` to execute the single best move.

```mermaid
flowchart TD
    A([User]) -->|"Move the most overloaded\ncell to a lighter DU."| B[chat.py\nlocalhost:8082]
    B -->|HTTP POST /chat| C[Orchestrator\nport 8082]
    C -->|System prompt + snapshot| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Tool call 1:\nquery_network\n{}| C
    C -->|GET /network| E[Controller\nport 8080]
    E -->|Read| F[(topology.json)]
    E -->|Flux query:\nprb_dl_pct per cell| G[(InfluxDB\nport 8086)]
    F --> E
    G -->|PRB & UE counts per cell| E
    E -->|Full network snapshot| C
    C -->|Tool result 1:\nall cells with PRB + DU| D

    D -->|"Rank cells by prb_dl_pct\n→ top cell = most overloaded\nRank DUs by avg PRB\n→ bottom DU = lightest"| D

    D -->|Tool call 2:\nmove_cell\n{cell_id, target_du_id}| C
    C -->|POST /move/cell| E2[Controller\nport 8080]
    E2 -->|Atomic write| F2[(topology.json)]
    F2 --> E2
    E2 -->|Move confirmed| C
    C -->|Tool result 2:\ncell moved| D

    D -->|Natural language reply:\nwhich cell moved, from/to DU,\nexpected load improvement| C
    C -->|HTTP response| B
    B -->|Print response| A
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1–3 | User → Orchestrator | Standard chat path |
| 4 | Gemini LLM | Tool call 1: `query_network()` — get live PRB data |
| 5 | Orchestrator | `GET /network` on Controller |
| 6 | Controller | Reads topology.json + InfluxDB PRB per cell |
| 7 | Controller | Returns full network snapshot |
| 8 | Orchestrator | Returns snapshot to Gemini |
| 9 | Gemini LLM | Ranks cells by `prb_dl_pct` → identifies most overloaded |
| 10 | Gemini LLM | Ranks DUs by avg PRB → identifies lightest target DU |
| 11 | Gemini LLM | Tool call 2: `move_cell(cell_id=X, target_du_id=Y)` |
| 12 | Orchestrator | `POST /move/cell` on Controller |
| 13 | Controller | Atomically updates `topology.json` |
| 14 | Controller | Returns move confirmation |
| 15 | Gemini LLM | Generates reply: cell moved, from/to DU, expected improvement |
| 16–17 | Orchestrator → User | Returns reply |

## Decision logic (Gemini steps 9–10)

```
cells sorted by prb_dl_pct (desc):
  MLS_RWS_01  → 94%  ← most overloaded (on DU-MLS-1)
  MLS_18C_01  → 87%
  ...

DUs sorted by avg_prb (asc):
  DU-MLS-3    → 38%  ← lightest
  DU-MLS-2    → 61%
  DU-MLS-1    → 79%  ← source (already overloaded)

move_cell(MLS_RWS_01, DU-MLS-3)
```

## Contrast with O1

| Aspect | O1 | O2 |
|--------|----|----|
| Step 1 tool | `get_alerts` (LSTM output) | `query_network` (raw PRB) |
| Scope | All OVERLOAD cells | Single worst cell |
| Moves | Multiple `move_cell` calls | Single `move_cell` |
| Selection | KPI Agent classification | LLM PRB ranking |
