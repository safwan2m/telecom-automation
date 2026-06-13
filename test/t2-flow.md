# T2 Query Flow — "Which DU manages MLS_MGR_01?"

```mermaid
flowchart TD
    A([User]) -->|"Which DU manages MLS_MGR_01?"| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + live network snapshot\n+ user message| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Function call:\nquery_network\n{}| C

    C -->|GET /network| E[Controller\nport 8080]

    E -->|Read| F[(topology.json\nDU cell_ids lists)]
    E -->|Flux query: latest KPI\nper cell| G[(InfluxDB\nport 8086)]

    F -->|DU → cell_ids mapping| E
    G -->|KPIs per cell| E

    E -->|Merged snapshot:\ndus{du_id: {cell_ids:[]}}| C

    C -->|Tool result:\nfull topology with DU→cell map| D

    D -->|"Reverse lookup:\nscan each DU's cell_ids[]\nfind DU containing MLS_MGR_01"| D

    D -->|Natural language reply:\n"MLS_MGR_01 is managed by DU-MLS-1."| C

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
| 6 | Controller | Reads `topology.json` — DU `cell_ids[]` lists for all 3 DUs |
| 7 | Controller | Runs Flux query against InfluxDB for latest KPIs per cell |
| 8 | Controller | Returns merged snapshot with full DU → cell_ids mapping |
| 9 | Orchestrator | Returns tool result to Gemini |
| 10 | Gemini LLM | Scans each DU's `cell_ids[]` list to find which contains `MLS_MGR_01` |
| 11 | Gemini LLM | Generates natural language answer: "MLS_MGR_01 is managed by DU-MLS-1." |
| 12 | Orchestrator | Returns reply to `chat.py` |
| 13 | `chat.py` | Prints answer to user |

## Reverse lookup logic (Gemini step 10)

```
dus = {
  "DU-MLS-1": { cell_ids: ["MLS_RWS_03", ..., "MLS_MGR_01", ...] },  ← found here
  "DU-MLS-2": { cell_ids: ["MLS_SPG_03", ...] },
  "DU-MLS-3": { cell_ids: ["MLS_MGR_02", ...] },
}

for du_id, du in dus.items():
    if "MLS_MGR_01" in du["cell_ids"]:
        answer = f"MLS_MGR_01 is managed by {du_id}."
        break
```

## Contrast with T1

| Aspect | T1 | T2 |
|--------|----|----|
| Tool | `query_network` | `query_network` |
| Endpoint | `GET /network` | `GET /network` |
| LLM operation | Walk CU→DU→cell tree and format hierarchy | Reverse lookup: scan DU cell_ids to find target cell |
| Answer type | Full topology tree | Single DU name |
| Key field used | `cus{}`, `dus{}`, `cells{}` | `dus[*].cell_ids[]` |

## Key data path

```
topology.json ──┐  (dus{du_id: {cell_ids:[]}} — DU→cell mapping)
                ├──► Controller /network ──► Orchestrator ──► Gemini (reverse lookup) ──► User
InfluxDB ───────┘  (KPIs per cell — not critical for T2 but always included in /network)
```
