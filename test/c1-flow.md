# C1 Query Flow — "How many cells are deployed in the network?"

```mermaid
flowchart TD
    A([User]) -->|"How many cells are deployed\nin the network?"| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + live network snapshot\n+ user message| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Function call:\nquery_network\n{}| C

    C -->|GET /network| E[Controller\nport 8080]

    E -->|Read| F[(topology.json\nCU/DU/cell assignments)]
    E -->|Flux query: latest KPI\nper cell| G[(InfluxDB\nport 8086)]

    F -->|Cell config| E
    G -->|KPIs per cell| E

    E -->|Merged snapshot:\ncells[] with KPIs| C

    C -->|Tool result:\nfull cell list| D

    D -->|"Count cells[] array length\n→ 30 cells total"| D

    D -->|Natural language reply:\n"There are 30 cells deployed\nin the network."| C

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
| 7 | Controller | Runs Flux query against InfluxDB for latest KPIs per cell |
| 8 | Controller | Merges topology + KPIs; returns full 30-cell snapshot |
| 9 | Orchestrator | Returns tool result to Gemini |
| 10 | Gemini LLM | Counts the length of the `cells[]` array |
| 11 | Gemini LLM | Generates natural language answer |
| 12 | Orchestrator | Returns reply to `chat.py` |
| 13 | `chat.py` | Prints answer to user |

## Contrast with similar queries

| Aspect | U1 | U3 | C1 |
|--------|----|----|-----|
| Tool | `query_network` | `query_network` | `query_network` |
| Endpoint | `GET /network` | `GET /network` | `GET /network` |
| LLM aggregation | Sum `connected_ues` | Rank by `connected_ues` | Count `cells[]` length |
| Answer type | Total UE count | Top cell name + UE count | Cell deployment count |

## Key data path

```
topology.json ──┐
                ├──► Controller /network ──► Orchestrator ──► Gemini (count) ──► User
InfluxDB ───────┘
(cells[] array length → 30 cells)
```
