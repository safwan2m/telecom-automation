# I2 Query Flow — "Reduce call drops in the southern cells."

> Intent-based SON command. Gemini diagnoses SINR issues in southern cells
> via `get_alerts` + `query_network`, then executes `move_cell`.

```mermaid
flowchart TD
    A([User]) -->|"Reduce call drops\nin the southern cells."| B[chat.py\nlocalhost:8082]
    B -->|HTTP POST /chat| C[Orchestrator\nport 8082]
    C -->|System prompt + snapshot| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Tool call 1:\nget_alerts\n{alert_type: SINR_LOW}| C
    C -->|GET /alerts?alert_type=SINR_LOW| E[Controller\nport 8080]
    E -->|Flux query:\nSINR_LOW alerts| G[(InfluxDB\nalerts measurement)]
    G -->|SINR_LOW records\nwith cell_id & severity| E
    E -->|SINR alert list| C
    C -->|Tool result 1:\nSINR_LOW cells| D

    D -->|Tool call 2:\nquery_network\n{}| C
    C -->|GET /network| E2[Controller\nport 8080]
    E2 -->|Read| F2[(topology.json)]
    E2 -->|Flux query:\nsinr_db, PRB per cell| G2[(InfluxDB\nKPI measurement)]
    F2 --> E2
    G2 -->|sinr_db + location\nper cell| E2
    E2 -->|Network snapshot| C
    C -->|Tool result 2:\ncell KPIs + locations| D

    D -->|"Filter southern cells\n(lat < 13.000)\nCross-check SINR_LOW alerts\nvs actual sinr_db values"| D

    D -->|Tool call 3:\nmove_cell\n{cell_id, target_du_id}| C
    C -->|POST /move/cell| E3[Controller\nport 8080]
    E3 -->|Atomic write| F3[(topology.json)]
    F3 --> E3
    E3 -->|Move confirmed| C
    C -->|Tool result 3| D

    D -->|Natural language reply:\nSINR diagnosis, cells moved,\nexpected call-drop reduction| C
    C -->|HTTP response| B
    B -->|Print response| A
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1–3 | User → Orchestrator | Standard chat path |
| 4 | Gemini LLM | Tool call 1: `get_alerts(alert_type="SINR_LOW")` |
| 5–7 | Orchestrator → Controller | Query InfluxDB alerts for SINR_LOW |
| 8 | Orchestrator | Returns SINR_LOW alert list to Gemini |
| 9 | Gemini LLM | Tool call 2: `query_network()` — get SINR values + locations |
| 10–12 | Orchestrator → Controller | `GET /network`, InfluxDB sinr_db per cell |
| 13 | Gemini LLM | Filters southern cells (lat < 13.000): MLS_MGR, MLS_CHD, MLS_6CR, MLS_SPG |
| 14 | Gemini LLM | Cross-checks SINR_LOW alerts with live sinr_db values |
| 15 | Gemini LLM | Decides move_cell to reduce DU contention and improve SINR |
| 16 | Gemini LLM | Tool call 3: `move_cell(cell_id=X, target_du_id=Y)` |
| 17–19 | Orchestrator → Controller | Updates topology.json |
| 20 | Gemini LLM | Generates reply: cells rebalanced, expected SINR improvement |
| 21–22 | Orchestrator → User | Returns reply |

## Southern cells (lat < 13.000)

| Cell | Site | lat | Current issue |
|------|------|-----|---------------|
| MLS_MGR_01/02/03 | Margosa Rd | 12.996 | SINR_LOW possible |
| MLS_CHD_01/02/03 | Chord Rd | 12.993 | SINR_LOW possible |
| MLS_6CR_01/02/03 | 6th Cross | 12.997 | SINR_LOW possible |
| MLS_SPG_01/02/03 | Sampige Rd | 12.999 | SINR_LOW possible |

## Three-tool sequence

```
Tool 1: get_alerts(SINR_LOW)   → which cells are flagged
Tool 2: query_network()        → confirm sinr_db values + find lightest DU
Tool 3: move_cell(X, DU-Y)    → rebalance to reduce interference
```
