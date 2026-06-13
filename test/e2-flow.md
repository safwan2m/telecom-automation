# E2 Query Flow — "Why did you move that cell to a different DU?"

> Pure LLM explainability — no tool call. Gemini explains the reasoning
> behind a specific prior `move_cell` decision using conversation history.

```mermaid
flowchart TD
    A([User]) -->|"Why did you move that\ncell to a different DU?"| B[chat.py\nlocalhost:8082]

    B -->|HTTP POST /chat\n{session, message}| C[Orchestrator\nport 8082]

    C -->|System prompt + conversation history\n(including move_cell call + alert context)| D[Gemini LLM\ngemini-2.5-flash]

    D -->|"No tool call — reads from context:\n- which cell was moved\n- source DU PRB at time of move\n- target DU PRB at time of move\n- alert that triggered the decision"| D

    D -->|Natural language reply:\nstep-by-step rationale for\nthe specific move decision| C

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
| 3 | Orchestrator | Injects conversation history including the move_cell call + its preceding alerts |
| 4 | Gemini LLM | **No tool call** — reconstructs decision from context |
| 5 | Gemini LLM | Identifies: which cell, source DU, target DU, and triggering alert |
| 6 | Gemini LLM | Recalls DU load comparison at time of decision |
| 7 | Gemini LLM | Generates causal explanation of the move |
| 8 | Orchestrator | Returns reply to `chat.py` |
| 9 | `chat.py` | Prints explanation to user |

## What Gemini reconstructs from conversation history

```
Alert context:
  MLS_RWS_01 → OVERLOAD (CRITICAL, confidence=0.94)
  DU-MLS-1 avg PRB = 92%

DU load at decision time:
  DU-MLS-1  92%  ← source (overloaded)
  DU-MLS-2  61%
  DU-MLS-3  38%  ← target (lightest)

Decision:
  move_cell(MLS_RWS_01, DU-MLS-3)
```

Gemini explains:
> "MLS_RWS_01 was moved from DU-MLS-1 to DU-MLS-3 because DU-MLS-1 was at 92% PRB
> utilisation with a CRITICAL OVERLOAD alert (confidence 94%). DU-MLS-3 was the
> lightest available DU at 38% PRB, making it the best target to rebalance load."

## Contrast with E1

| Aspect | E1 | E2 |
|--------|----|----|
| Scope | All actions in session | Single specific move |
| Answer | Ordered action list | Causal chain for one decision |
| Depth | Breadth (what happened) | Depth (why it happened) |
| Follow-up typical use | After O1 (batch optimization) | After O2 (single move) |
