# I1 Query Flow — "Improve user experience in the Malleswaram railway station area."

> Intent-based SON command. Gemini autonomously decides the action sequence:
> assess area cells → analyse KPIs → execute move_cell or plan_network.

```mermaid
flowchart TD
    A([User]) -->|"Improve user experience in\nthe Malleswaram railway\nstation area."| B[chat.py\nlocalhost:8082]
    B -->|HTTP POST /chat| C[Orchestrator\nport 8082]
    C -->|System prompt + snapshot| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Tool call 1:\nquery_network\n{}| C
    C -->|GET /network| E[Controller\nport 8080]
    E -->|Read| F[(topology.json)]
    E -->|Flux query: all KPIs| G[(InfluxDB\nport 8086)]
    F --> E
    G -->|connected_ues, PRB,\nSINR, throughput per cell| E
    E -->|Full network snapshot| C
    C -->|Tool result 1| D

    D -->|"Identify cells near railway station\n(lat≈13.008, lon≈77.576)\nAssess: PRB load, SINR, throughput"| D

    D -->|Tool call 2 option A:\nmove_cell\n{cell_id, target_du_id}| C
    D -->|Tool call 2 option B:\nplan_network\n{area, spectrum_bands}| C

    C -->|Execute chosen action| E2[Controller / Planning API]
    E2 -->|Topology update\nor new plan| F2[(topology.json\nor plan store)]
    F2 --> E2
    E2 -->|Action confirmed| C
    C -->|Tool result 2| D

    D -->|Natural language reply:\nactions taken, expected\nUX improvements| C
    C -->|HTTP response| B
    B -->|Print response| A
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1–3 | User → Orchestrator | Standard chat path |
| 4 | Gemini LLM | Tool call 1: `query_network()` — assess all cells |
| 5–7 | Orchestrator → Controller | `GET /network`, reads topology + InfluxDB |
| 8 | Orchestrator | Returns full snapshot to Gemini |
| 9 | Gemini LLM | Identifies railway station area cells (MLS_RWS_01/02/03 at lat≈13.008) |
| 10 | Gemini LLM | Assesses: high PRB? low SINR? overcrowded UEs? |
| 11 | Gemini LLM | **Decides action autonomously:** move_cell (if DU imbalance) or plan_network (if capacity gap) |
| 12 | Orchestrator | Executes chosen tool (move_cell or plan_network) |
| 13 | Controller / Planning API | Updates topology or generates capacity plan |
| 14 | Gemini LLM | Optionally executes further tool calls |
| 15 | Gemini LLM | Generates intent fulfilment summary |
| 16–17 | Orchestrator → User | Returns reply |

## Gemini's autonomous decision tree

```
query_network → railway station cells (MLS_RWS_*)

IF prb_dl_pct > 80% AND lighter DU available:
    → move_cell(cell, lighter_du)         # rebalance load

ELSE IF connected_ues near max_ues:
    → plan_network(area="Malleswaram",    # add capacity
                   spectrum_bands=["n78"])

ELSE IF sinr_db < 5:
    → explain SINR issue; suggest physical site improvement
```

## What makes I1 different from O1/O2

| Aspect | O1/O2 (Optimization) | I1 (Intent) |
|--------|----------------------|-------------|
| Trigger | Explicit operator command | Natural language goal |
| Scope | Predefined: load balance | LLM decides scope autonomously |
| Tool selection | Fixed sequence | LLM picks tools based on diagnosis |
| Action | move_cell | move_cell OR plan_network OR both |
