# C2 Query Flow — "Show details of MLS_RWS_01."

```mermaid
flowchart TD
    A([User]) -->|"Show details of MLS_RWS_01."| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + live network snapshot\n+ user message| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Function call:\nquery_cell\n{cell_id: 'MLS_RWS_01'}| C

    C -->|GET /cells/MLS_RWS_01| E[Controller\nport 8080]

    E -->|Read full cell config\nPCI, band, DU/CU, vendor, power| F[(topology.json)]
    E -->|Flux query: 30-min KPI series\nfor MLS_RWS_01| G[(InfluxDB\nport 8086)]

    F -->|Full cell config| E
    G -->|Time series:\nconnected_ues, prb_dl_pct,\nsinr_db, dl_throughput_mbps,\nho_success_rate, packet_loss_pct| E

    E -->|Cell detail response:\nfull config + 30-min KPI series| C

    C -->|Tool result:\nMLS_RWS_01 full detail| D

    D -->|"Format all config fields\n+ summarise 30-min KPI trends"| D

    D -->|Natural language reply:\nfull cell profile with\nconfig and live KPIs| C

    C -->|HTTP response\n{reply}| B

    B -->|Print response| A
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1 | User | Types query into `chat.py` |
| 2 | `chat.py` | POSTs `{session, message}` to Orchestrator `/chat` |
| 3 | Orchestrator | Builds system prompt with live network snapshot; forwards to Gemini |
| 4 | Gemini LLM | Identifies cell `MLS_RWS_01`; emits `query_cell(cell_id="MLS_RWS_01")` |
| 5 | Orchestrator | Dispatches `query_cell` → `GET /cells/MLS_RWS_01` on Controller |
| 6 | Controller | Reads `topology.json` for full MLS_RWS_01 config (PCI, band, vendor, power, max UEs, DU/CU) |
| 7 | Controller | Runs Flux query against InfluxDB: 30-min time series for all KPIs of MLS_RWS_01 |
| 8 | Controller | Returns merged full config + 30-min KPI time series |
| 9 | Orchestrator | Returns tool result to Gemini |
| 10 | Gemini LLM | Formats all config fields and summarises 30-min KPI trends |
| 11 | Gemini LLM | Generates detailed natural language cell profile |
| 12 | Orchestrator | Returns reply to `chat.py` |
| 13 | `chat.py` | Prints answer to user |

## Config fields returned (from topology.json)

| Field | Value for MLS_RWS_01 |
|-------|----------------------|
| Generation | 5G NR |
| Band | n78 (3500 MHz) |
| PCI | 1 |
| Vendor | Nokia |
| Hardware | AirScale MAA 64T64R |
| Antenna | 64T64R |
| Peak DL | 3800 Mbps |
| TX Power | 1000 W |
| Max UEs | 900 |
| DU | DU-MLS-1 |
| CU | CU-MLS |

## KPI time series returned (from InfluxDB)

| KPI | Description |
|-----|-------------|
| `connected_ues` | Active UEs on this cell over 30 min |
| `prb_dl_pct` | Downlink PRB utilisation % |
| `sinr_db` | Signal-to-interference-plus-noise ratio |
| `dl_throughput_mbps` | Downlink throughput |
| `ho_success_rate` | Handover success rate |
| `packet_loss_pct` | Packet loss percentage |

## Contrast with U2

| Aspect | U2 | C2 |
|--------|----|----|
| Query intent | UE count on a cell | Full cell details |
| Tool | `query_cell` | `query_cell` |
| Endpoint | `GET /cells/MLS_RWS_01` | `GET /cells/MLS_RWS_01` |
| LLM step | Extract `connected_ues` only | Format all config + all KPI trends |
| Answer | Single number | Full cell profile |

## Key data path

```
topology.json ──┐  (PCI, band, vendor, power, max_ues, DU/CU)
                ├──► Controller /cells/MLS_RWS_01 ──► Orchestrator ──► Gemini (format) ──► User
InfluxDB ───────┘  (30-min series: connected_ues, PRB, SINR, throughput, HO rate, packet loss)
```
