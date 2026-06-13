# A2 Query Flow — "Identify overloaded cells."

> Two possible paths depending on Gemini's decision: `get_alerts` (faster, uses LSTM output)
> or `query_network` (direct PRB check).

```mermaid
flowchart TD
    A([User]) -->|"Identify overloaded cells."| B[chat.py\nlocalhost:8082]
    B -->|HTTP POST /chat| C[Orchestrator\nport 8082]
    C -->|System prompt + snapshot| D[Gemini LLM\ngemini-2.5-flash]

    D -->|PATH A:\nget_alerts\n{alert_type: OVERLOAD}| C
    D -->|PATH B:\nquery_network\n{}| C

    subgraph PATH_A [Path A — KPI Agent alerts]
        C -->|GET /alerts?alert_type=OVERLOAD| EA[Controller\nport 8080]
        EA -->|Flux query: OVERLOAD alerts\nlast 60 min| GA[(InfluxDB\nalerts measurement)]
        GA -->|OVERLOAD alert records| EA
        EA -->|Filtered alert list| C
    end

    subgraph PATH_B [Path B — Direct PRB check]
        C -->|GET /network| EB[Controller\nport 8080]
        EB -->|Read| FB[(topology.json)]
        EB -->|Flux query:\nprb_dl_pct per cell| GB[(InfluxDB\nKPI measurement)]
        FB --> EB
        GB -->|prb_dl_pct per cell| EB
        EB -->|Full network snapshot| C
    end

    C -->|Tool result| D
    D -->|"Path A: list cells with\nOVERLOAD alert\nPath B: filter cells\nwhere prb_dl_pct > 80%"| D
    D -->|Natural language reply:\noverloaded cell list\nwith PRB/UE details| C
    C -->|HTTP response| B
    B -->|Print response| A
```

## Step-by-step (Path A — get_alerts)

| Step | Actor | Action |
|------|-------|--------|
| 1–3 | User → Orchestrator | Standard chat path |
| 4 | Gemini LLM | Emits `get_alerts(alert_type="OVERLOAD", minutes=60)` |
| 5 | Orchestrator | `GET /alerts?alert_type=OVERLOAD` on Controller |
| 6 | Controller | Queries InfluxDB `alerts` measurement filtered by OVERLOAD |
| 7 | Controller | Returns list of cells the KPI Agent flagged as OVERLOAD |
| 8–9 | Orchestrator → Gemini | Passes alert list |
| 10 | Gemini LLM | Lists overloaded cells with severity and confidence |
| 11–13 | Gemini → User | Reply with overloaded cell names |

## Step-by-step (Path B — query_network)

| Step | Actor | Action |
|------|-------|--------|
| 4 | Gemini LLM | Emits `query_network()` to get live PRB data |
| 5 | Orchestrator | `GET /network` → Controller |
| 6–7 | Controller | topology.json + InfluxDB PRB per cell |
| 10 | Gemini LLM | Filters cells where `prb_dl_pct > 80` |

## Path A vs Path B

| Aspect | Path A (get_alerts) | Path B (query_network) |
|--------|---------------------|------------------------|
| Source | LSTM classifier output | Raw PRB KPI |
| Latency | Lower (pre-classified) | Higher (raw data) |
| Confidence | Includes model confidence score | N/A |
| Threshold | Model-defined (OVERLOAD class) | Fixed PRB > 80% |
| Best for | Quick anomaly triage | Precise PRB audit |
