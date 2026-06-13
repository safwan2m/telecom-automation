# U1 Query Flow — "How many UEs are currently active in the network?"

```mermaid
flowchart TD
    A([User]) -->|"How many UEs are currently active\nin the network?"| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + live network snapshot\n+ user message| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Function call:\nquery_network\n{}| C

    C -->|GET /network| E[Controller\nport 8080]

    E -->|Read| F[(topology.json\nCU/DU/cell assignments)]
    E -->|Flux query: last KPI\nper cell| G[(InfluxDB\nport 8086)]

    F -->|Cell config| E
    G -->|connected_ues per cell\nprb_dl_pct, sinr_db, …| E

    E -->|Merged snapshot:\ncells[] with KPIs| C

    C -->|Tool result:\ncells with connected_ues| D

    D -->|"Sum connected_ues\nacross all 30 cells\n→ total active UEs"| D

    D -->|Natural language reply:\n"There are X UEs currently active\nacross 30 cells."| C

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
| 8 | Controller | Merges topology + KPIs; returns full cell list |
| 9 | Orchestrator | Returns tool result to Gemini |
| 10 | Gemini LLM | Sums `connected_ues` across all cells to get network total |
| 11 | Gemini LLM | Generates natural language answer |
| 12 | Orchestrator | Returns reply to `chat.py` |
| 13 | `chat.py` | Prints answer to user |

## Key data path

```
topology.json ──┐
                ├──► Controller /network ──► Orchestrator ──► Gemini (sum) ──► User
InfluxDB ───────┘
(connected_ues per cell)
```
