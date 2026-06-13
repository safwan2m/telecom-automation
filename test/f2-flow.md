# F2 Query Flow — "What cells are affected by the new deployment?"

> **Context:** Follow-up after `plan_network`. Gemini cross-references new plan cells
> with current topology via `query_network`.

```mermaid
flowchart TD
    A([User]) -->|"What cells are affected by\nthe new deployment?"| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + conversation history\n+ plan result + user message| D[Gemini LLM\ngemini-2.5-flash]

    D -->|Function call:\nquery_network\n{}| C

    C -->|GET /network| E[Controller\nport 8080]

    E -->|Read| F[(topology.json\nexisting cell positions)]
    E -->|Flux query: latest KPIs| G[(InfluxDB\nport 8086)]

    F -->|Current 30-cell topology| E
    G -->|PRB, SINR per cell| E

    E -->|Current network snapshot| C

    C -->|Tool result:\ncurrent topology + KPIs| D

    D -->|"Cross-reference:\n- new plan cells vs existing cells\n- proximity / PCI overlap\n- PRB impact on neighbours"| D

    D -->|Natural language reply:\nlist of cells potentially\naffected by new deployment| C

    C -->|HTTP response\n{reply}| B

    B -->|Print response| A
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1 | User | Types follow-up query into `chat.py` |
| 2 | `chat.py` | POSTs `{session, message}` to Orchestrator `/chat` |
| 3 | Orchestrator | Injects conversation history (plan result) + user message |
| 4 | Gemini LLM | Decides to call `query_network` for current topology |
| 5 | Orchestrator | Dispatches `query_network` → `GET /network` |
| 6 | Controller | Reads `topology.json` + InfluxDB KPIs |
| 7 | Controller | Returns current 30-cell network snapshot |
| 8 | Orchestrator | Returns tool result to Gemini |
| 9 | Gemini LLM | Cross-references new plan cells (from history) with current topology |
| 10 | Gemini LLM | Identifies cells with PCI proximity, coverage overlap, or PRB impact |
| 11 | Gemini LLM | Generates list of affected cells with reasoning |
| 12 | Orchestrator | Returns reply to `chat.py` |
| 13 | `chat.py` | Prints answer to user |

## Cross-reference logic (Gemini step 9–10)

```
plan_cells = [from conversation history: new cells with lat/lon, PCI, band]
current_cells = [from query_network result: existing 30 cells]

affected = []
for new_cell in plan_cells:
    for existing in current_cells:
        if proximity(new_cell, existing) < threshold:
            affected.append(existing)
        if pci_conflict(new_cell.pci, existing.pci):
            affected.append(existing)
```
