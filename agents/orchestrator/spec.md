# Orchestrator Agent — Specification

FastAPI service on port 8082. Accepts natural-language operator commands, drives a multi-step tool-calling loop against the active LLM backend, and streams the response back in real time.

## Routes

```
POST   /chat          {"message": "...", "session_id": "default"} → streaming text/plain
GET    /history       ?session_id=  → [{"role", "content"}, ...]
DELETE /history       ?session_id=  → {"status": "cleared", "session_id": "..."}
GET    /tools         → [{"name", "description"}, ...]  (16 tools)
GET    /health        → {"status": "ok", "model": "<name>", "backend": "gemini"|"claude-cli"}
```

## Backend selection

Determined at startup by `CLAUDE_CLI_PATH`:

| Condition | Backend | Notes |
|---|---|---|
| `CLAUDE_CLI_PATH` non-empty | Claude CLI | Active in Docker — compose sets `/usr/bin/claude` |
| `CLAUDE_CLI_PATH` empty/unset | Gemini | Requires `GOOGLE_API_KEY` |

`MODEL_NAME` reported by `/health`:
- Claude: `claude/<ANTHROPIC_MODEL_NAME>` (default `claude/sonnet`)
- Gemini: value of `GEMINI_MODEL` (code default `gemini-2.0-flash`; Docker sets `gemini-2.5-flash`)

## System prompt

Two-part prompt injected on every `/chat` request:

**Static** (`SYSTEM_PROMPT`) — included verbatim:
```
You are an expert RAN operations assistant for a Bangalore 4G/5G NSA deployment.
You have access to tools to query live network state, move RAN components, run the
planning engine, and retrieve alerts.

Network overview (Malleswaram, North Bangalore):
- 30 cells across 10 macro tower sites (3 sectors per site), all in Malleswaram
- Cell naming: MLS_<SITE>_<SECTOR> — sites: RWS, 18C, BEL, SNK (DU-MLS-1 / north),
  SPG, 3MN, 10C (DU-MLS-2 / central), MGR, CHD, 6CR (DU-MLS-3 / south-west)
- Each site has: 1× 5G n78 3500 MHz + 1× 4G B3 1800 MHz; high-traffic sites also carry
  1× 5G n41 2500 MHz, residential sites carry 1× 4G B40 2300 MHz
- 700 MHz (n28) NOT deployed — radius extends beyond Malleswaram to Peenya
- 3 Distributed Units under 1 Centralised Unit:
    CU-MLS → DU-MLS-1 (north: RWS, 18C, BEL, SNK — 12 cells)
              DU-MLS-2 (central: SPG, 3MN, 10C — 9 cells)
              DU-MLS-3 (south-west: MGR, CHD, 6CR — 9 cells)
- Multi-vendor (Nokia, Ericsson, Samsung, ZTE — ~25% each by site)
- 18,400 peak active UEs (40,000 residents + 15% commuter overhead × 40% operator share)
- 5G n78 (3500 MHz, 64T64R): 3800 Mbps peak, max 900 UEs/sector, 900–1000 W, ~830 m radius
- 5G n41 (2500 MHz, 64T64R): 3000 Mbps peak, max 700 UEs/sector, 900–1000 W, ~1.2 km radius
- 4G B3  (1800 MHz, 4T4R):   150 Mbps,  max 250 UEs/sector, 200 W, ~1.27 km radius
- 4G B40 (2300 MHz, 4T4R):   150 Mbps,  max 300 UEs/sector, 200 W, ~1.0 km radius

Guidelines:
- Always call query_network first if you need current state before taking actions.
- Explain what you observe before acting. Ask for confirmation before move_cell/move_du/apply_plan.
- Flag: overloaded cells (PRB > 85%), SINR below 5 dB, power waste (high W, few UEs).
- 5G cells at idle draw ~225–260 W — POWER_WASTE only actionable when power is high relative to UEs.
- When the operator asks to plan a network, call plan_network and summarise before asking to apply.
- Be concise. Bullet points are fine for status summaries.
```

**Dynamic** (`build_network_context()`): calls `GET /network` on the Controller (5 s timeout) and formats every cell as one line: `  <cell_id> (<area>) → DU=... | UEs=... | PRB=...% | SINR=...dB | Power=...W`. Prepended with `\n\nCurrent network snapshot:\n`. Appended to the static prompt on every request. Returns `(Network snapshot unavailable — controller may be starting up.)` on failure.

## Request / Response flow

```
POST /chat
  ├─ build_network_context()  ──GET /network──► Controller
  ├─ system = SYSTEM_PROMPT + live snapshot
  └─ route to active backend's chat_turn generator
        ├─ (Gemini)  chat_turn_gemini(session_id, user_message)
        └─ (Claude)  chat_turn_claude(session_id, user_message)

Both generators yield text/plain chunks:
  - LLM text
  - "\n\n*[calling tool: <name>...]*\n"  before each tool execution
  - "\n\n[Error] ..."  on API failure / quota exhaustion
  - "\n" between tool loop iterations
```

`StreamingResponse` wraps a **synchronous generator**; Starlette runs it in a thread pool so blocking HTTP calls inside the loop do not stall the asyncio event loop.

## Tool-calling loop — Gemini backend

```python
while True:
    response = gemini.models.generate_content(model, contents=history, config)
    history.append(response.candidates[0].content)   # model turn
    yield text parts
    if no function_calls: break
    for tc in tool_calls:
        yield "*[calling tool: name...]*"
        result = execute_tool(tc.name, dict(tc.args))
        fn_parts.append(FunctionResponse(name, response=result))
    history.append(Content(role="user", parts=fn_parts))  # all results in one user turn
    yield "\n"
```

Gemini may batch multiple tool calls in one response; all are executed before the next model call. On exception: roll back `history[turn_start:]`, yield `[Error]`. 429/quota errors detected by checking for `"429"`, `"quota"`, or `"ResourceExhausted"` in the exception message.

Session history format: `list[types.Content]` (`_gemini_sessions` dict).

## Tool-calling loop — Claude CLI backend

```python
while True:
    response = claude_client.messages.create(system=system, tools=TOOL_SCHEMAS, messages=history)
    yield text parts
    if stop_reason == "end_turn" or no tool_blocks:
        history.append({"role": "assistant", "content": response.content})
        break
    history.append({"role": "assistant", "content": response.content})
    tool_results = []
    for tb in tool_blocks:
        yield "*[calling tool: name...]*"
        result = execute_tool(tb.name, tb.input)
        tool_results.append({"type": "tool_result", "tool_use_id": tb.id,
                              "content": json.dumps(result)})
    history.append({"role": "user", "content": tool_results})
    yield "\n"
```

`CustomAnthropicClient` only ever returns one tool call per response (by design — see below). On exception: roll back `history[turn_start:]`, yield `[Error]`.

**History trimming** (applied at the start of each turn):
- Cap at `_MAX_HISTORY_MESSAGES = 20`; trim from oldest, always aligning to a plain user message (never start with an assistant or tool_result turn).
- Belt-and-suspenders size check: while total JSON size > `_MAX_HISTORY_CHARS = 120,000` chars, pop oldest messages, re-aligning to a plain user message.

Session history format: `list[{"role", "content"}]` (`_claude_sessions` dict).

## CustomAnthropicClient

`custom_anthropic_client.py` — drop-in for `anthropic.Anthropic()`. Exposes `client.messages.create(system, tools, messages)`. Backed by `claude -p` subprocess. No Anthropic API key required.

### CLI invocation

```
claude -p --model <ANTHROPIC_MODEL_NAME> --output-format json
```

Prompt is passed on stdin (avoids ARG_MAX). Timeout: 120 s. Output is the `--output-format json` envelope: `{"type": "result", "result": "<text>", "is_error": false}`. The `result` field is the model's inner JSON response.

### Prompt structure

Four sections joined with `\n\n`:

1. **Guardrails** — "You are a stateless completion endpoint… reply with EXACTLY ONE JSON object and NOTHING else."
2. **System instructions** — the caller's `system` string.
3. **Tools + response contract** — each tool listed as `name: description\n  input_schema: <json>`, followed by the contract:
   - If a tool call is needed: `{"type": "tool_use", "name": "...", "input": {...}}`
   - Final answer: `{"type": "text", "text": "..."}`
   - **Call only ONE tool per response.**
4. **Conversation so far** — `_serialize_messages(messages)`:
   - `user, content=str` → `[user] <text>`
   - `assistant, content=[ToolUseBlock]` → `[assistant] called tool <name> (id=...) with input <json>`
   - `assistant, content=[TextBlock]` → `[assistant] <text>`
   - `user, content=[tool_result dicts]` → `[user] tool result for <tool_use_id>: <content>`

### Response parsing

`_parse(result_text)` → duck-typed `Response(stop_reason, content)`:
- `{"type": "tool_use", "name": ..., "input": {...}}` → `stop_reason="tool_use"`, `content=[ToolUseBlock]`
- `{"type": "text", "text": ...}` → `stop_reason="end_turn"`, `content=[TextBlock]`
- Unstructured / markdown-fenced JSON: attempts fence stripping, then first-`{`/last-`}` extraction, falls back to treating raw text as a TextBlock.

Duck-typed classes: `ToolUseBlock(name, input, id)`, `TextBlock(text)`, `Response(stop_reason, content)` — all read by the Claude chat loop identically to real Anthropic SDK objects.

## Tool schema translation — Gemini

`_clean_params(params)` applied per-schema at startup, deep-copied to avoid mutating `TOOL_SCHEMAS`:
- Removes `"default"` from every property (Gemini API rejects it).
- Strips the `""` sentinel from `"enum"` arrays; deletes the `"enum"` key entirely if the array becomes empty.

Result: `GEMINI_TOOLS = [{"function_declarations": [...]}]`.

Claude CLI backend uses `TOOL_SCHEMAS` as-is (Anthropic native format).

## Tool execution

`execute_tool(name, args)`:
1. Looks up `TOOL_MAP[name]`; returns `{"error": "Unknown tool: name"}` if not found.
2. Calls the Python function; wraps non-dict returns in `{"result": ...}`.
3. JSON round-trips the result (`json.dumps(result, default=str)` then `json.loads`) to sanitise proto Struct types.
4. **Size cap**: if serialized result > `_MAX_TOOL_RESULT_CHARS = 40,000` chars, discards it and returns:
   ```json
   {"warning": "Result too large (N chars). Add filters to narrow the query.",
    "hint": "For <tool>: supply cell_id, ue_id, or reduce last_minutes."}
   ```

## LangSmith tracing

`tracing.py` — optional LangSmith integration. All helpers are no-ops when inactive.

**Active when:** `langsmith` package is installed AND (`LANGSMITH_API_KEY` or `LANGCHAIN_API_KEY`) is set AND (`LANGSMITH_TRACING=true` or `LANGCHAIN_TRACING_V2=true`).

**Trace structure:** each `/chat` request opens a root `chain` run via `start_run("chat/gemini"|"chat/claude", {message, model, session_id})`. Each `execute_tool` call opens a child `tool` span via `tool_span(parent_run, name, inputs)`. Both are closed with `end_run(run, outputs|error)`.

Parent run handle is passed explicitly through the call stack (not via context var) because generator `yield` breaks async context propagation.

Output fields > 20 MB are truncated to `"[TRUNCATED N MB]"` before posting to LangSmith.

**Env vars** (both prefix variants accepted):

| LangSmith var | LangChain alias | Purpose |
|---|---|---|
| `LANGSMITH_API_KEY` | `LANGCHAIN_API_KEY` | Auth token |
| `LANGSMITH_TRACING` | `LANGCHAIN_TRACING_V2` | Set to `true` to enable |
| `LANGSMITH_PROJECT` | `LANGCHAIN_PROJECT` | Project name (default `telecom_tracing`) |
| `LANGSMITH_ENDPOINT` | `LANGCHAIN_ENDPOINT` | API endpoint |

## Session management

- **Gemini sessions**: `_gemini_sessions: dict[str, list[types.Content]]`
- **Claude sessions**: `_claude_sessions: dict[str, list[{"role","content"}]]`
- Multiple sessions coexist per `session_id` (`"default"`, `"ops-team"`, `"map-xxxxxxx"`, etc.)
- `DELETE /history` pops the session key from the active store; returns `{"status": "cleared", "session_id": "..."}`.
- Sessions are lost on container restart — no persistence.

**GET /history normalization** — both session formats are flattened to `[{"role","content"}]`:
- Gemini: `Content.role == "model"` → `"assistant"`; `function_call` parts → `[Calling name]`; `function_response` parts → `[Tool result: name]`.
- Claude: plain string content → passthrough; `TextBlock.text` → text; `ToolUseBlock.name` → `[Calling name]`; `tool_result` dict → `[Tool result: tool_use_id]`.

## Tool inventory

16 tools in `TOOL_SCHEMAS` / `TOOL_MAP`:

| Tool | Backend call | Key details |
|---|---|---|
| `query_network` | `GET /network` Controller | Full topology + live KPIs |
| `list_cells` | `GET /cells?area=&du_id=&cu_id=` Controller | Optional filters; all optional |
| `query_cell` | `GET /cells/{cell_id}` Controller | 30-min KPI time series |
| `move_cell` | `POST /move/cell` Controller | `{cell_id, to_du_id}` |
| `move_du` | `POST /move/du` Controller | `{du_id, to_cu_id}` |
| `list_areas` | `GET /areas` Planning API | All named Malleswaram sub-localities |
| `get_area_cells` | `GET /areas/{area_id}/cells` Planning API | Cells covering ≥20% of area; accepts area_id or name substring |
| `plan_network` | `POST /plan` Planning API | See below |
| `apply_plan` | `POST /plan/apply` Planning API | `{plan_id}` |
| `list_suspended_cells` | `GET /cells/suspended?area=` Planning API | Cells with hardware installed, not transmitting |
| `get_alerts` | InfluxDB direct | See below |
| `query_ue` | InfluxDB direct | See below |
| `get_son_status` | InfluxDB direct | See below |
| `add_cell` | `POST /cells/add` Controller | See below |
| `remove_cell` | `DELETE /cells/{cell_id}` Controller | — |
| `optimize_congestion` | `GET /congestion` Controller | See below |

HTTP helpers: `_ctrl(path, method, body)` uses 8 s timeout; `_plan(path, method, body)` uses 30 s timeout. Both return `{"error": str(e)}` on failure.

### `plan_network` — body construction

Only non-None args are included. `embb_fraction / urllc_fraction / mmtc_fraction / peak_hour` are nested under `traffic_profile`. `e2e_latency_ms` is wrapped as `{"latency_constraints": {"e2e_ms": value, "fronthaul_us": 100.0}}`.

`"required": []` in the tool schema — the LLM must call with only what the operator has stated; the planning server returns a missing-fields list for the rest. Prevents the LLM from inferring unstated parameters.

### `get_alerts` — Flux query

Measurement: `alerts`, field: `message`. Optional `severity` filter tag. `sort(desc: true)`, `limit(n: 50)`. Returns raw InfluxDB rows (fields prefixed with `_` are stripped except `_time`).

### `query_ue` — Flux queries

Two queries merged in the return dict:

- **usage** (`ue_usage` measurement): fields `dl_bytes`, `ul_bytes`, `latency_ms`, `jitter_ms`, `packet_loss`; `last()`, `toFloat()`. Default limit 30 records (unfiltered) / 50 (with ue_id or cell_id). For cell_id: filters on `cell_id` tag.
- **mobility** (`ue_mobility` measurement): fields `ho_duration_ms`, `rsrp_source`, `rsrp_target`, `velocity_kmh`; `last()`. Default limit 10 records (unfiltered) / 20 (filtered). For cell_id: filters on `source_cell == cell_id OR target_cell == cell_id`.

Both use `group(columns: [])` before `limit()` so the limit is a global cap, not per-series. Unfiltered calls prepend a warning to the result.

### `get_son_status` — Flux queries

Two queries over `last_minutes`:

- **son_actions**: measurement `son_actions`, field `message`, `sort(desc: true)`, `limit(n: 20)`. Action type counts aggregated in Python from the `action_type` tag. Returns `recent_actions` = first 10 rows.
- **alerts**: measurement `alerts`, field `message`, `sort(desc: true)`, `limit(n: 50)`. Alert severity counts from `severity` tag.

Return shape: `{window_minutes, total_son_actions, action_type_counts, total_alerts, alert_severity_counts, recent_actions}`.

### `add_cell` — per-vendor hardware defaults

Hardware is looked up from built-in tables by `(generation, vendor)` and merged into the POST body. `pci: 0` is passed — the Controller auto-assigns a collision-free PCI.

**5G defaults (64T64R, 3500 MHz):**

| Vendor | hardware_model | peak_dl_mbps | idle_power_w |
|---|---|---|---|
| Nokia | AirScale MAA 64T64R | 3800 | 250 |
| Ericsson | AIR 6449 | 3600 | 240 |
| Samsung | TM500 64T64R | 3400 | 225 |
| ZTE | AAU 5614 | 3200 | 250 |

**4G defaults (4T4R, 1800 MHz):**

| Vendor | hardware_model | peak_dl_mbps | idle_power_w |
|---|---|---|---|
| Nokia | Flexi Multiradio 10 AWHFA | 150 | 80 |
| Ericsson | Radio 4449 | 150 | 70 |
| Samsung | NR RU 4T4R | 150 | 65 |
| ZTE | AARU 4T4R | 150 | 75 |

### `optimize_congestion`

Fetches `GET /congestion?top_n=<top_n>` from Controller (default 10). For the top-5 CRITICAL or HIGH cells, additionally calls `GET /neighbors/{cell_id}?max_neighbors=3` and attaches the neighbor list as `cell["neighbors"]`. Returns: `{summary, top_congested_cells, guidance}`.

### Tool schema design note

`plan_network` uses `"required": []` so the LLM never infers missing values. All other tools keep their `"required"` arrays. This is intentional: plan_network needs interactive operator input; the planning server's missing-fields response drives the conversation.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CLAUDE_CLI_PATH` | `""` (code) / `/usr/bin/claude` (Docker) | Non-empty → Claude CLI backend |
| `ANTHROPIC_MODEL_NAME` | `sonnet` | Claude model alias (Claude backend only) |
| `GOOGLE_API_KEY` | required | Gemini auth (Gemini backend only) |
| `GEMINI_MODEL` | `gemini-2.0-flash` (code) / `gemini-2.5-flash` (Docker) | Gemini model |
| `CONTROLLER_URL` | `http://controller:8080` | Context injection + move/query tools |
| `PLANNING_URL` | `http://planning-api:8081` | plan_network / apply_plan / list_areas tools |
| `INFLUX_URL` | `http://influxdb:8086` | Direct Flux queries |
| `INFLUX_TOKEN` | `telecom-super-secret-auth-token-2026` | InfluxDB auth |
| `INFLUX_ORG` | `telecom` | InfluxDB org |
| `INFLUX_BUCKET` | `telecom_metrics` | InfluxDB bucket |

---

## chat.py — Operator CLI Client

Standalone terminal REPL at project root. Pure stdlib (`urllib.request`) — no external dependencies. Contains no LLM logic.

### Usage

```bash
py chat.py                                   # localhost:8082, session "default"
py chat.py --url http://host:8082            # remote orchestrator
py chat.py --session ops-team               # named session
```

On startup: `GET /health` (10 s timeout); prints banner `Telecom Orchestrator | model: <name> | <url>`. Prints warning and continues if unreachable.

### Shortcuts (expanded before sending to /chat)

| Input | Expanded message |
|---|---|
| `/status` | `What is the current status of all cells, DUs, and CUs? Summarise in a table.` |
| `/alerts` | `Show me all recent KPI alerts from the last 60 minutes.` |
| `/cells` | `List all cells with their current connected UEs, PRB utilisation, and DU assignment.` |
| `/plan` | `I want to plan a network deployment. Ask me for all the required parameters before proceeding.` |
| `/son` | `Show me the recent SON autonomous actions and their outcomes.` |
| `/ue` | `Show me UE usage and mobility events from the last 30 minutes.` |

### Local commands (not sent to orchestrator)

| Command | Action |
|---|---|
| `/history` | `GET /history?session_id=...`; prints `[ROLE] content[:200]` per turn |
| `/clear` | `DELETE /history?session_id=...`; prints `History cleared.` |
| `/tools` | `GET /tools`; prints `• name — description[:70]` per tool |
| `quit` / `exit` / `q` | Exit |

### Transport

`POST /chat` blocks with 60 s timeout — full response printed at once (no streaming). `/history`, `/tools`, `/health` use 10 s timeout. The map server's browser chat panel uses `fetch` with `ReadableStream` for live streaming output.

### Session semantics

`--session` sets `session_id` in every request. Different `--session` names are fully isolated on the server. The map server uses `map-<7 random chars>` per page load.
