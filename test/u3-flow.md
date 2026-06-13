# U3 Query Flow — "Which cell is serving the highest number of UEs right now?"

```mermaid
flowchart TD
    A([User]) -->|"Which cell is serving the highest\nnumber of UEs right now?"| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + live network snapshot\n+ user message| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Function call:\nquery_network\n{}| C

    C -->|GET /network| E[Controller\nport 8080]

    E -->|Read| F[(topology.json\nCU/DU/cell assignments)]
    E -->|Flux query: latest KPI\nper cell| G[(InfluxDB\nport 8086)]

    F -->|Cell config| E
    G -->|connected_ues per cell\nprb_dl_pct, sinr_db, …| E

    E -->|Merged snapshot:\ncells[] with KPIs| C

    C -->|Tool result:\nall 30 cells with connected_ues| D

    D -->|"Rank all 30 cells by connected_ues\n→ identify top cell"| D

    D -->|Natural language reply:\n"Cell X is serving the most UEs\nwith Y UEs connected."| C

    C -->|HTTP response\n{reply}| B

    B -->|Print response| A
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1 | User | Types query into `chat.py` |
| 2 | `chat.py` | POSTs `{session, message}` to Orchestrator `/chat` |
| 3 | Orchestrator | Builds system prompt with live network snapshot; forwards to Gemini |
| 4 | Gemini LLM | Decides `query_network` is the right tool; emits a function call |
| 5 | Orchestrator | Dispatches `query_network` → `GET /network` on Controller |
| 6 | Controller | Reads `topology.json` for cell/DU/CU config |
| 7 | Controller | Runs Flux query against InfluxDB for latest `connected_ues` per cell |
| 8 | Controller | Merges topology + KPIs; returns full 30-cell snapshot |
| 9 | Orchestrator | Returns tool result to Gemini |
| 10 | Gemini LLM | Ranks all 30 cells by `connected_ues`; picks the highest |
| 11 | Gemini LLM | Generates natural language answer naming the top cell |
| 12 | Orchestrator | Returns reply to `chat.py` |
| 13 | `chat.py` | Prints answer to user |

## Contrast with U1 and U2

| Aspect | U1 | U2 | U3 |
|--------|----|----|-----|
| Tool called | `query_network` | `query_cell` | `query_network` |
| Controller endpoint | `GET /network` | `GET /cells/MLS_RWS_01` | `GET /network` |
| Scope | All 30 cells | Single cell | All 30 cells |
| KPI data | Latest snapshot per cell | 30-min time series for one cell | Latest snapshot per cell |
| LLM aggregation | Sum `connected_ues` → total | Extract latest `connected_ues` | Rank by `connected_ues` → top cell |
| Answer type | Single number | Single number | Cell name + UE count |

## Key data path

```
topology.json ──┐
                ├──► Controller /network ──► Orchestrator ──► Gemini (rank) ──► User
InfluxDB ───────┘
(connected_ues per cell — all 30 cells ranked, top-1 returned)
```
