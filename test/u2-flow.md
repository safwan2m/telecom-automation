# U2 Query Flow — "How many UEs are connected to MLS_RWS_01?"

```mermaid
flowchart TD
    A([User]) -->|"How many UEs are connected\nto MLS_RWS_01?"| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + live network snapshot\n+ user message| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Function call:\nquery_cell\n{cell_id: 'MLS_RWS_01'}| C

    C -->|GET /cells/MLS_RWS_01| E[Controller\nport 8080]

    E -->|Read cell config\n& DU/CU assignment| F[(topology.json)]
    E -->|Flux query: 30-min KPI series\nfor MLS_RWS_01| G[(InfluxDB\nport 8086)]

    F -->|Cell config\n{pci, band, du_id, cu_id, …}| E
    G -->|Time series:\nconnected_ues, prb_dl_pct,\nsinr_db, dl_throughput_mbps| E

    E -->|Cell detail response:\nconfig + 30-min KPI series| C

    C -->|Tool result:\nMLS_RWS_01 KPIs| D

    D -->|"Extract latest connected_ues\nfrom KPI series"| D

    D -->|Natural language reply:\n"MLS_RWS_01 currently has X UEs\nconnected."| C

    C -->|HTTP response\n{reply}| B

    B -->|Print response| A
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1 | User | Types query into `chat.py` |
| 2 | `chat.py` | POSTs `{session, message}` to Orchestrator `/chat` |
| 3 | Orchestrator | Builds system prompt with live network snapshot; forwards to Gemini |
| 4 | Gemini LLM | Identifies cell name `MLS_RWS_01`; emits `query_cell(cell_id="MLS_RWS_01")` |
| 5 | Orchestrator | Dispatches `query_cell` → `GET /cells/MLS_RWS_01` on Controller |
| 6 | Controller | Reads `topology.json` for cell config (PCI, band, DU/CU assignment) |
| 7 | Controller | Runs Flux query against InfluxDB for 30-min KPI series for `MLS_RWS_01` |
| 8 | Controller | Returns merged cell config + KPI time series |
| 9 | Orchestrator | Returns tool result to Gemini |
| 10 | Gemini LLM | Extracts the latest `connected_ues` value from the KPI series |
| 11 | Gemini LLM | Generates natural language answer |
| 12 | Orchestrator | Returns reply to `chat.py` |
| 13 | `chat.py` | Prints answer to user |

## Contrast with U1

| Aspect | U1 | U2 |
|--------|----|----|
| Tool called | `query_network` | `query_cell` |
| Controller endpoint | `GET /network` | `GET /cells/MLS_RWS_01` |
| Scope | All 30 cells | Single cell |
| KPI data | Latest snapshot per cell | 30-min time series for one cell |
| LLM aggregation | Sum `connected_ues` across all cells | Extract latest `connected_ues` for one cell |

## Key data path

```
topology.json ──┐
                ├──► Controller /cells/MLS_RWS_01 ──► Orchestrator ──► Gemini (extract) ──► User
InfluxDB ───────┘
(30-min KPI series for MLS_RWS_01)
```
