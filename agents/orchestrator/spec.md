# Orchestrator Agent — Specification

FastAPI service on port 8082. Accepts natural-language operator commands over HTTP, drives a multi-step tool-calling loop, and streams the response back in real time. Supports two LLM backends selected at startup.

## Backend selection

Backend is determined by the `CLAUDE_CLI_PATH` env var at startup:

| Condition | Backend | Active in Docker? |
|---|---|---|
| `CLAUDE_CLI_PATH` non-empty | Claude CLI | **Yes** — docker-compose sets `/usr/bin/claude` |
| `CLAUDE_CLI_PATH` empty/unset | Gemini | Only if `CLAUDE_CLI_PATH` is explicitly unset |

**Claude CLI backend** (`CLAUDE_CLI_PATH` non-empty): spawns the `claude -p` process via `CustomAnthropicClient`. `TOOL_SCHEMAS` are passed as-is (already in Anthropic native format — no translation needed). Model selected via `ANTHROPIC_MODEL_NAME` (default: `sonnet`). Session history stored as `_claude_sessions: dict[str, list[{"role", "content"}]]`.

**Gemini backend** (`CLAUDE_CLI_PATH` empty): uses `google-genai` SDK, requires `GOOGLE_API_KEY`. Tool schemas translated from Anthropic-style JSON to Gemini `function_declarations` at startup via `_clean_params()`. Model selected via `GEMINI_MODEL` (code default: `gemini-2.0-flash`; docker-compose overrides to `gemini-2.5-flash`). Session history stored as `_gemini_sessions: dict[str, list[types.Content]]`.

`GET /health` returns `{"status": "ok", "model": "<name>", "backend": "gemini"|"claude-cli"}`.

## Request / Response flow

```
User message  (POST /chat)
      │
      ├─► build_network_context()  ──GET /network──► Controller
      │         (live cell snapshot appended to system prompt)
      │
      ▼
SYSTEM_PROMPT + live snapshot + session history
      │
      ▼
  ┌──────────────────────────────────────────────┐
  │  Gemini 2.5 Flash  (non-streaming API call)  │
  └──────────────────────────────────────────────┘
      │
      ├── text parts → yield to caller (streaming)
      │
      └── function_call parts → tool-calling loop:
              ├─ yield "*[calling tool: name...]*"
              ├─ execute_tool(name, args)  (Python call)
              ├─ append FunctionResponse to history
              └─ call Gemini again  →  repeat until no tool calls
```

## System prompt

Two-part prompt injected on every request:

- **Static** (`SYSTEM_PROMPT`): 30-cell network overview — site naming convention (`MLS_<SITE>_<SECTOR>`), DU/CU hierarchy, per-band UE limits and power specs, operator guidelines (confirm before destructive actions, flag overloads, bullet summaries)
- **Dynamic** (`build_network_context()`): calls `GET /network` on the Controller and formats every cell as one line — `cell_id (area) → DU=... | UEs=... | PRB=...% | SINR=...dB | Power=...W`. Appended to the static prompt on every request so the LLM always sees current live state. Returns a warning message if the Controller is unreachable.

## Tool-calling loop

Each `/chat` request runs a `while True` loop until Gemini returns no function calls:

1. `gemini.models.generate_content(model, contents=history, config)` — synchronous, non-streaming
2. `model_content` (the full assistant turn) is appended to session history
3. Any text parts are yielded immediately to the streaming caller
4. For each `function_call` in the response:
   - Yield `\n\n*[calling tool: name...]*\n` (visible in the chat UI)
   - Call `T.TOOL_MAP[name](args)` — synchronous Python, hits Controller / Planning API / InfluxDB over HTTP
   - JSON-sanitise the result (`json.dumps(result, default=str)`) so proto Struct accepts it
   - Append a `FunctionResponse` part to a new `user` turn in history
5. Go to step 1 with the updated history. Break when the response contains no function calls.

Multiple tools can be called per response (Gemini may batch them); all are executed and their results fed back in a single user turn before the next model call.

## Tool schema translation

`tools.py` stores all tool schemas in **Anthropic-style JSON** (`name`, `description`, `input_schema` with JSON Schema `properties`).

- **Claude CLI backend**: schemas used as-is — no translation needed.
- **Gemini backend**: `_clean_params()` strips `default` fields (Gemini rejects them), removes empty `enum` arrays (arises from the `""` sentinel in `severity` enum), and deep-copies to avoid mutating `TOOL_SCHEMAS`. Produces `GEMINI_TOOLS = [{"function_declarations": [...]}]`.

## Tool inventory

| Tool | HTTP call | Purpose |
|---|---|---|
| `query_network` | `GET /network` on Controller | Full topology + live KPIs for all 30 cells |
| `list_cells` | `GET /cells?area=&du_id=&cu_id=` | Filtered cell list with KPIs |
| `query_cell` | `GET /cells/{id}` | Single cell config + 30-min KPI time series |
| `move_cell` | `POST /move/cell` | Reassign a cell to a different DU |
| `move_du` | `POST /move/du` | Reassign a DU to a different CU |
| `plan_network` | `POST /plan` | Heuristic or MIP-optimal placement + PCI + slice planning |
| `plan_network_multi_period` | `POST /plan/multi-period` | Multi-period MIP (Case A phased rollout / Case B diurnal shift) |
| `apply_plan` | `POST /plan/apply` | Push accepted plan to Controller as live topology |
| `get_alerts` | InfluxDB Flux query (direct) | Recent KPI anomaly alerts tagged by severity and type |
| `query_ue` | InfluxDB Flux query (direct) | UE-level usage and mobility data (filter by ue_id or cell_id) |
| `get_son_status` | InfluxDB Flux query (direct) | SON action summary + counts by type, last 10 actions, active alert severity |
| `add_cell` | `POST /cells/add` on Controller | Deploy a new cell via chat; auto-assigns PCI if not provided |
| `remove_cell` | `DELETE /cells/{id}` on Controller | Decommission a cell and remove from DU assignment |
| `optimize_congestion` | `GET /congestion` on Controller | Live congestion scores ranked by PRB/SINR/BLER/latency with neighbour headroom hints |

## Tool schema design — planning tools

`plan_network` and `plan_network_multi_period` use `"required": []` (empty) in their tool schemas. All parameters default to `None` in the Python function. The tool description explicitly instructs the LLM: *"call with only the values the operator has explicitly provided — do NOT infer or assume missing values."*

**Why this matters:** if the tool schema lists fields as `"required"`, the LLM treats them as values it must supply before making the call. It infers plausible values from context (area from the system prompt, density 500, budget 2 M, etc.) and calls the tool with a fully-populated body. The planning server receives all fields as non-null, finds nothing missing, and generates a plan without the operator ever being asked. Empty `"required"` breaks that inference loop — the LLM calls with whatever the operator has actually stated, the server's missing-fields check triggers for the rest, and the LLM relays the list back to the operator in the next turn.

All other tools keep their existing `"required"` arrays — this design only applies to the planning tools because they are the ones that need interactive operator input before proceeding.

## Session management

- Two in-memory session stores: `_gemini_sessions` (list of `types.Content`) and `_claude_sessions` (list of `{"role", "content"}` dicts) — one is active depending on the backend
- Multiple sessions coexist independently per `session_id` (e.g. `default`, `ops-team`, `map-abc1234`)
- `DELETE /history` clears a session; sessions are lost on container restart (no persistence)
- `GET /history` normalises either session format into flat `{"role", "content"}` dicts; tool calls shown as `[Calling name]`, results as `[Tool result: name]`

## Streaming

`POST /chat` returns a FastAPI `StreamingResponse` wrapping a **synchronous generator** (`chat_turn`). Starlette runs sync generators in a thread pool, so the blocking Gemini API call and tool HTTP calls do not stall the asyncio event loop. The caller receives plain `text/plain` chunks:

- LLM text — one chunk per Gemini response turn (not token-by-token; Gemini's non-streaming API returns the full turn at once)
- `\n\n*[calling tool: name...]*\n` — emitted before each tool execution
- `\n\n[Error] ...` — on quota exhaustion (`429`), rate limit, or API failure
- Quota/rate-limit errors are detected by checking for `"429"`, `"quota"`, or `"ResourceExhausted"` in the exception message

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CLAUDE_CLI_PATH` | `""` (Gemini) / `/usr/bin/claude` (Docker) | Path to `claude` binary; non-empty activates Claude CLI backend |
| `ANTHROPIC_MODEL_NAME` | `sonnet` | Claude model alias (Claude CLI backend only) |
| `GOOGLE_API_KEY` | required (Gemini only) | Gemini API authentication |
| `GEMINI_MODEL` | `gemini-2.0-flash` (code) / `gemini-2.5-flash` (Docker) | Gemini model name (Gemini backend only) |
| `CONTROLLER_URL` | `http://controller:8080` | Context injection + move_cell / move_du tools |
| `PLANNING_URL` | `http://planning-api:8081` | plan_network / apply_plan tools |
| `INFLUX_URL` | `http://influxdb:8086` | get_alerts / query_ue / get_son_status (direct Flux queries) |
| `INFLUX_TOKEN` / `INFLUX_ORG` / `INFLUX_BUCKET` | — | InfluxDB authentication for direct queries |

## Routes

```
POST   /chat          {"message": "...", "session_id": "default"}  → streaming text/plain
GET    /history?session_id=
DELETE /history?session_id=
GET    /tools          → [{"name", "description"}]
GET    /health         → {"status": "ok", "model": "<name>", "backend": "gemini"|"claude-cli"}
```

---

## chat.py — Operator CLI Client

`chat.py` (project root) is a standalone terminal REPL that connects to the orchestrator's REST API. It contains no LLM logic — it is a pure UI layer that formats requests and prints responses.

### Usage

```bash
py chat.py                                    # default: localhost:8082, session "default"
py chat.py --url http://remote-host:8082      # remote orchestrator
py chat.py --session ops-team                 # named session (isolated history)
```

On startup it calls `GET /health` and prints a banner showing the active model name and orchestrator URL. If the orchestrator is unreachable, it prints a warning but continues.

### Built-in commands

| Command | Action |
|---|---|
| `/status` | Expands → *"What is the current status of all cells, DUs, and CUs? Summarise in a table."* |
| `/alerts` | Expands → *"Show me all recent KPI alerts from the last 60 minutes."* |
| `/cells` | Expands → *"List all cells with their current connected UEs, PRB utilisation, and DU assignment."* |
| `/plan` | Expands → *"I want to plan a network deployment. Ask me for all the required parameters before proceeding."* |
| `/son` | Expands → *"Show me the recent SON autonomous actions and their outcomes."* |
| `/ue` | Expands → *"Show me UE usage and mobility events from the last 30 minutes."* |
| `/history` | `GET /history?session_id=...` — prints past turns (role + first 200 chars) |
| `/clear` | `DELETE /history?session_id=...` — resets server-side conversation |
| `/tools` | `GET /tools` — lists all available agent tools with short descriptions |
| `quit` / `exit` / `q` | Exits the CLI |

Any other input is sent as-is to `POST /chat` with the current `session_id`.

### Transport

Pure Python stdlib (`urllib.request`, `urllib.error`) — no external dependencies. The `/chat` call blocks until the server closes the response body (full response appears at once, not token-by-token). For live streaming output use the map server's integrated chat panel (browser Fetch API with `ReadableStream`).

### Session semantics

`--session` sets the `session_id` field in every request. Multiple operators can run separate `chat.py` instances with different `--session` names against the same orchestrator without sharing context or history. The map server's chat panel uses a randomised session ID (`map-xxxxxxx`) per page load.
