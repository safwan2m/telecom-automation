# F1 Query Flow — "Why was that site selected for the new cell?"

> **Context:** Follow-up after a `plan_network` call has already returned a plan.
> No additional tool call is needed — Gemini reasons from conversation history.

```mermaid
flowchart TD
    A([User]) -->|"Why was that site selected\nfor the new cell?"| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + conversation history\n+ plan_network result| D[Gemini LLM\ngemini-2.5-flash]

    D -->|"Reads plan from conversation history:\n- density_weight of candidate site\n- budget constraint check\n- SINR / coverage overlap check\n- assigned PCI / DU / slices"| D

    D -->|Natural language reply:\nexplains site selection criteria\nfrom planning logic| C

    C -->|HTTP response\n{reply}| B

    B -->|Print response| A

    style D fill:#5A238C,color:#fff
    style C fill:#0A2955,color:#fff
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1 | User | Types follow-up query into `chat.py` |
| 2 | `chat.py` | POSTs `{session, message}` to Orchestrator `/chat` |
| 3 | Orchestrator | Injects full conversation history (including plan result) into system prompt |
| 4 | Gemini LLM | **No tool call** — reads plan details from conversation context |
| 5 | Gemini LLM | Identifies candidate site, its `density_weight`, budget fit, SINR margin |
| 6 | Gemini LLM | Generates explanation of site selection logic |
| 7 | Orchestrator | Returns reply to `chat.py` |
| 8 | `chat.py` | Prints explanation to user |

## Data source: plan_network result (in conversation context)

| Field | Role in site selection |
|-------|------------------------|
| `density_weight` | Higher = more demand → preferred site |
| `estimated_cost_usd` | Must be within budget |
| `sinr_min_db` | Coverage overlap constraint |
| `pci` | Collision/confusion-free assignment confirms site viability |
| `du_id` | Assigned to lightest available DU |

## Key distinction from other queries

- **No tool call** — Gemini answers purely from conversation history
- **No Controller / InfluxDB interaction**
- Depends on a prior `plan_network` call in the same session
