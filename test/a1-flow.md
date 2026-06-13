# A1 Query Flow — "What anomalies currently exist in the network?"

```mermaid
flowchart TD
    A([User]) -->|"What anomalies currently\nexist in the network?"| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + live snapshot\n+ user message| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Function call:\nget_alerts\n{minutes: 60}| C

    C -->|GET /alerts?minutes=60| E[Controller\nport 8080]

    E -->|Flux query:\nalerts measurement\nlast 60 min, all severities| G[(InfluxDB\nport 8086)]

    G -->|Alert records:\ncell_id, alert_type, severity,\ntimestamp, confidence| E

    E -->|Alert list response| C

    C -->|Tool result:\nalerts[] sorted by severity| D

    D -->|"Groups by severity & type:\nOVERLOAD, UNDERLOAD,\nSINR_LOW, POWER_WASTE"| D

    D -->|Natural language reply:\nsummary of anomalies\nby type and severity| C

    C -->|HTTP response\n{reply}| B

    B -->|Print response| A
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1 | User | Types query into `chat.py` |
| 2 | `chat.py` | POSTs `{session, message}` to Orchestrator `/chat` |
| 3 | Orchestrator | Builds system prompt; forwards to Gemini |
| 4 | Gemini LLM | Emits `get_alerts(minutes=60)` — all severities, last 60 min |
| 5 | Orchestrator | Dispatches `get_alerts` → `GET /alerts?minutes=60` on Controller |
| 6 | Controller | Queries InfluxDB `alerts` measurement for last 60 min |
| 7 | InfluxDB | Returns alert records written by the KPI Agent (LSTM classifier) |
| 8 | Controller | Returns alert list to Orchestrator |
| 9 | Orchestrator | Passes tool result to Gemini |
| 10 | Gemini LLM | Groups alerts by severity (CRITICAL, WARNING, INFO) and type |
| 11 | Gemini LLM | Generates natural language anomaly summary |
| 12 | Orchestrator | Returns reply to `chat.py` |
| 13 | `chat.py` | Prints anomaly report to user |

## Alert schema (InfluxDB `alerts` measurement)

| Field | Description |
|-------|-------------|
| `cell_id` | Cell that triggered the alert |
| `alert_type` | OVERLOAD \| UNDERLOAD \| SINR_LOW \| POWER_WASTE \| NORMAL |
| `severity` | CRITICAL \| WARNING \| INFO |
| `confidence` | LSTM model softmax confidence (≥ 0.70 to act) |
| `timestamp` | Time of detection |
| `du_id` | DU managing the flagged cell |

## KPI Agent → InfluxDB → Orchestrator pipeline

```
DU simulators ──push KPIs──► InfluxDB
                                  │
KPI Agent ──LSTM classify──► alerts measurement ──► get_alerts tool ──► Gemini
```
