# T1 Query Flow — "Show the network topology."

```mermaid
flowchart TD
    A([User]) -->|"Show the network topology."| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + live network snapshot\n+ user message| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Function call:\nquery_network\n{}| C

    C -->|GET /network| E[Controller\nport 8080]

    E -->|Read| F[(topology.json\nCU → DU → cell hierarchy)]
    E -->|Flux query: latest KPI\nper cell| G[(InfluxDB\nport 8086)]

    F -->|Full CU/DU/cell structure| E
    G -->|KPIs per cell| E

    E -->|Merged snapshot:\ncus{}, dus{}, cells{}| C

    C -->|Tool result:\nfull topology object| D

    D -->|"Walk CU → DU → cell tree\nformat as hierarchy"| D

    D -->|Natural language reply:\nCU-MLS → DU-MLS-1/2/3\n→ 30 cells with config| C

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
| 6 | Controller | Reads `topology.json` — full CU/DU/cell hierarchy |
| 7 | Controller | Runs Flux query against InfluxDB for latest KPIs per cell |
| 8 | Controller | Returns merged snapshot: `cus{}`, `dus{}`, `cells{}` |
| 9 | Orchestrator | Returns tool result to Gemini |
| 10 | Gemini LLM | Walks the CU → DU → cell tree and formats it as a hierarchy |
| 11 | Gemini LLM | Generates natural language reply showing CU-MLS → DU-MLS-1/2/3 → 30 cells |
| 12 | Orchestrator | Returns reply to `chat.py` |
| 13 | `chat.py` | Prints topology to user |

## Topology hierarchy returned

```
CU-MLS  (host: cu-mls)
├── DU-MLS-1  (host: du-mls-1)  — 14 cells
│   ├── MLS_RWS_01  5G n78
│   ├── MLS_RWS_03  4G B3
│   ├── MLS_18C_01  5G n78
│   └── … (11 more)
├── DU-MLS-2  (host: du-mls-2)  — 9 cells
│   ├── MLS_RWS_02  5G n41
│   ├── MLS_SPG_01  5G n78
│   └── … (7 more)
└── DU-MLS-3  (host: du-mls-3)  — 7 cells
    ├── MLS_MGR_02  4G B40
    ├── MLS_10C_02  5G n41
    └── … (5 more)
```

## Key data path

```
topology.json ──┐  (cus{}, dus{}, cells{} — full hierarchy)
                ├──► Controller /network ──► Orchestrator ──► Gemini (walk tree) ──► User
InfluxDB ───────┘  (latest KPIs per cell — enriches the topology view)
```
