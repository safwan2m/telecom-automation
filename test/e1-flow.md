# E1 Query Flow — "What actions did you take to optimize the network?"

> Pure LLM explainability — no tool call. Gemini summarises all previous
> tool calls and their outcomes from the current conversation history.

```mermaid
flowchart TD
    A([User]) -->|"What actions did you take\nto optimize the network?"| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + full\nconversation history\n+ all tool call records| D[Gemini LLM\ngemini-2.5-flash]

    D -->|"No tool call — reads from\nconversation history:\n- get_alerts results\n- move_cell calls & outcomes\n- PRB before/after values"| D

    D -->|Natural language reply:\nordered list of actions,\nrationale, and observed effect| C

    C -->|HTTP response\n{reply}| B

    B -->|Print response| A

    style D fill:#5A238C,color:#fff
    style C fill:#0A2955,color:#fff
```

## Step-by-step

| Step | Actor | Action |
|------|-------|--------|
| 1 | User | Types explainability query into `chat.py` |
| 2 | `chat.py` | POSTs `{session, message}` to Orchestrator `/chat` |
| 3 | Orchestrator | Injects **full conversation history** including all prior tool calls and results |
| 4 | Gemini LLM | **No tool call** — reads action history from conversation context |
| 5 | Gemini LLM | Extracts: which tools were called, with what args, and what was returned |
| 6 | Gemini LLM | Reconstructs action sequence: alerts detected → cells moved → topology updated |
| 7 | Gemini LLM | Generates ordered action summary with rationale |
| 8 | Orchestrator | Returns reply to `chat.py` |
| 9 | `chat.py` | Prints action summary to user |

## What Gemini reads from conversation history

```
Turn N:   get_alerts(OVERLOAD) → [MLS_RWS_01 on DU-MLS-1, MLS_18C_01 on DU-MLS-1]
Turn N+1: move_cell(MLS_RWS_01, DU-MLS-3) → confirmed
Turn N+2: move_cell(MLS_18C_01, DU-MLS-2) → confirmed
Turn N+3: query_network() → DU-MLS-1 PRB dropped from 92% to 61%
```

Gemini synthesises:
> "I detected two OVERLOAD alerts on DU-MLS-1 and moved MLS_RWS_01 to DU-MLS-3
> and MLS_18C_01 to DU-MLS-2. DU-MLS-1 PRB load dropped from 92% to 61%."

## Key distinction

| Aspect | E1 | A1 / O1 |
|--------|----|----|
| Tool call | None | get_alerts / move_cell |
| Data source | Conversation history | Live InfluxDB + Controller |
| Tense | Past (what was done) | Present (current state) |
| Purpose | Explainability | Detection / Action |
