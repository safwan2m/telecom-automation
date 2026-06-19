#!/usr/bin/env python3
"""
Telecom Network Orchestrator — dual-backend LLM chat agent.

Backend A (default): Google Gemini tool-calling via google-genai SDK.
Backend B:           Claude CLI via CustomAnthropicClient (claude -p).
                     Activated by setting CLAUDE_CLI_PATH in the environment.

POST /chat    {"message": "...", "session_id": "default"} → streaming text
GET  /history {"session_id": "default"}
DELETE /history
GET  /tools
GET  /health
"""

import os
import json
import copy
import logging
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from google import genai
from google.genai import types

import tools as T
import tracing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

T.CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://controller:8080")
T.PLANNING_URL   = os.environ.get("PLANNING_URL",   "http://planning-api:8081")
T.INFLUX_URL     = os.environ.get("INFLUX_URL",     "http://influxdb:8086")
T.INFLUX_TOKEN   = os.environ.get("INFLUX_TOKEN",   "telecom-super-secret-auth-token-2026")
T.INFLUX_ORG     = os.environ.get("INFLUX_ORG",     "telecom")
T.INFLUX_BUCKET  = os.environ.get("INFLUX_BUCKET",  "telecom_metrics")

# ── Backend selection ─────────────────────────────────────────────────────────

CLAUDE_CLI_PATH = os.environ.get("CLAUDE_CLI_PATH", "").strip()

if CLAUDE_CLI_PATH:
    from custom_anthropic_client import CustomAnthropicClient
    claude_client = CustomAnthropicClient()
    MODEL_NAME    = f"claude/{os.environ.get('ANTHROPIC_MODEL_NAME', 'sonnet')}"
    USE_CLAUDE    = True
    gemini        = None
    log.info("Backend: Claude CLI at %s  model=%s", CLAUDE_CLI_PATH, MODEL_NAME)
else:
    GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
    MODEL_NAME     = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    gemini         = genai.Client(api_key=GOOGLE_API_KEY)
    USE_CLAUDE     = False
    claude_client  = None
    log.info("Backend: Gemini  model=%s", MODEL_NAME)

tracing.setup()

app = FastAPI(title="Telecom Orchestrator", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Session history — format depends on backend
_gemini_sessions: dict[str, list] = {}   # types.Content objects
_claude_sessions: dict[str, list] = {}   # {"role", "content"} dicts

SYSTEM_PROMPT = """You are an expert RAN operations assistant for a Bangalore 4G/5G NSA deployment.
You have access to tools to query live network state, move RAN components, run the planning engine, and retrieve alerts.

Network overview (Malleswaram, North Bangalore):
- 30 cells across 10 macro tower sites (3 sectors per site), all in Malleswaram
- Cell naming: MLS_<SITE>_<SECTOR> — sites: RWS, 18C, BEL, SNK (DU-MLS-1 / north),
  SPG, 3MN, 10C (DU-MLS-2 / central), MGR, CHD, 6CR (DU-MLS-3 / south-west)
- Each site has: 1× 5G n78 3500 MHz (capacity) + 1× 4G B3 1800 MHz (NSA anchor); high-traffic sites also carry 1× 5G n41 2500 MHz, residential sites carry 1× 4G B40 2300 MHz
- 700 MHz (n28) is NOT deployed — coverage radius would extend beyond Malleswaram to Peenya
- Mixed 4G LTE (B3/B40) and 5G NR (n78/n41) — NSA core, shared AMF/SMF/UPF
- 3 Distributed Units under 1 Centralised Unit:
    CU-MLS → DU-MLS-1 (north: RWS, 18C, BEL, SNK sites — 12 cells)
              DU-MLS-2 (central: SPG, 3MN, 10C sites — 9 cells)
              DU-MLS-3 (south-west: MGR, CHD, 6CR sites — 9 cells)
- Multi-vendor (Nokia, Ericsson, Samsung, ZTE — ~25% each by site)
- Scale: 18,400 peak active UEs (population 40,000 + 15% commuter overhead × 40% operator share)
- 5G NR n78 (3500 MHz, 64T64R): up to 3800 Mbps peak, max 900 UEs/sector, 900–1000 W, radius ~830 m
- 5G NR n41 (2500 MHz, 64T64R): up to 3000 Mbps peak, max 700 UEs/sector, 900–1000 W, radius ~1.2 km (5 sites: RWS, 18C, SNK, SPG, 10C)
- 4G LTE B3 (1800 MHz, 4T4R): 150 Mbps, max 250 UEs/sector, 200 W, radius ~1.27 km (NSA anchor — all 10 sites)
- 4G LTE B40 (2300 MHz, 4T4R): 150 Mbps, max 300 UEs/sector, 200 W, radius ~1.0 km (5 sites: BEL, 3MN, MGR, CHD, 6CR)

Guidelines:
- Always call query_network first if you need current state before taking actions.
- Explain what you observe before acting. Ask for confirmation before move_cell/move_du/apply_plan.
- When reporting KPIs, flag: overloaded cells (PRB > 85%), SINR below 5 dB, power waste (high W, few UEs).
- 5G cells at idle draw ~225–260 W — POWER_WASTE is only actionable when power is high relative to UEs.
- When the operator asks to plan a network, call plan_network and summarise before asking to apply it.
- Be concise. Bullet points are fine for status summaries.
"""


# ── Gemini tool schemas ───────────────────────────────────────────────────────

def _clean_params(params: dict) -> dict:
    """Strip fields the Gemini API rejects."""
    if "properties" in params:
        for prop in params["properties"].values():
            prop.pop("default", None)
            if "enum" in prop:
                prop["enum"] = [e for e in prop["enum"] if e != ""]
                if not prop["enum"]:
                    del prop["enum"]
    return params


GEMINI_TOOLS = [{
    "function_declarations": [
        {
            "name":        s["name"],
            "description": s["description"],
            "parameters":  _clean_params(copy.deepcopy(s["input_schema"])),
        }
        for s in T.TOOL_SCHEMAS
    ]
}]


# ── Gemini API call with explicit LangSmith span ─────────────────────────────

def _call_gemini(contents: list, config, _parent_run=None) -> object:
    span = tracing.tool_span(_parent_run, name="gemini.generate_content",
                             inputs={"model": MODEL_NAME, "num_contents": len(contents)})
    try:
        result = gemini.models.generate_content(model=MODEL_NAME, contents=contents, config=config)
        tracing.end_run(span, outputs={"model": MODEL_NAME})
        return result
    except Exception as e:
        tracing.end_run(span, error=str(e))
        raise


# ── Context injection ─────────────────────────────────────────────────────────

def build_network_context() -> str:
    try:
        r = httpx.get(f"{T.CONTROLLER_URL}/network", timeout=5.0)
        net = r.json()
        lines = []
        for cid, c in net.get("cells", {}).items():
            kpi = c.get("kpi", {})
            lines.append(
                f"  {cid} ({c.get('area','')}) → DU={c.get('du_id','')} | "
                f"UEs={kpi.get('connected_ues','?')} | PRB={kpi.get('prb_dl_pct','?')}% | "
                f"SINR={kpi.get('sinr_db','?')}dB | Power={kpi.get('power_w','?')}W"
            )
        return "\n\nCurrent network snapshot:\n" + "\n".join(lines)
    except Exception:
        return "\n\n(Network snapshot unavailable — controller may be starting up.)"


# ── Tool execution (shared by both backends) ──────────────────────────────────

_MAX_TOOL_RESULT_CHARS = 40_000  # ~10k tokens — keeps tool results inside model context limits

def execute_tool(name: str, args: dict, _parent_run=None) -> dict:
    fn = T.TOOL_MAP.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    span = tracing.tool_span(_parent_run, name=name, inputs=args)
    try:
        result = fn(args)
        if not isinstance(result, dict):
            result = {"result": result}
        result = json.loads(json.dumps(result, default=str))
        # Guard against oversized results overflowing the model context window
        serialized = json.dumps(result)
        if len(serialized) > _MAX_TOOL_RESULT_CHARS:
            result = {
                "warning": f"Result too large ({len(serialized):,} chars). Add filters to narrow the query.",
                "hint": f"For {name}: supply cell_id, ue_id, or reduce last_minutes.",
            }
        tracing.end_run(span, outputs=result)
        return result
    except Exception as e:
        tracing.end_run(span, error=str(e))
        return {"error": str(e)}


# ── Gemini chat turn ──────────────────────────────────────────────────────────

def chat_turn_gemini(session_id: str, user_message: str):
    """Run one full Gemini turn (including tool loops) and yield text chunks."""
    ls_run = tracing.start_run(
        "chat/gemini",
        {"message": user_message, "model": MODEL_NAME, "session_id": session_id},
    )
    history = _gemini_sessions.setdefault(session_id, [])
    history.append(types.Content(
        role="user",
        parts=[types.Part(text=user_message)],
    ))

    system = SYSTEM_PROMPT + build_network_context()
    config = types.GenerateContentConfig(
        system_instruction=system,
        tools=GEMINI_TOOLS,
    )

    response_chunks: list[str] = []
    try:
        while True:
            response = _call_gemini(history, config, _parent_run=ls_run)

            model_content = response.candidates[0].content
            history.append(model_content)

            text_parts = [p.text for p in model_content.parts if getattr(p, "text", None)]
            tool_calls = [p.function_call for p in model_content.parts
                          if getattr(p, "function_call", None) and p.function_call.name]

            if text_parts:
                chunk = "".join(text_parts)
                response_chunks.append(chunk)
                yield chunk

            if not tool_calls:
                break

            fn_parts = []
            for tc in tool_calls:
                yield f"\n\n*[calling tool: {tc.name}...]*\n"
                result = execute_tool(tc.name, dict(tc.args), _parent_run=ls_run)
                log.info("Tool %s -> %s", tc.name, str(result)[:120])
                fn_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=tc.name,
                            response=result,
                        )
                    )
                )

            history.append(types.Content(role="user", parts=fn_parts))
            yield "\n"

    except Exception as e:
        err = str(e)
        log.error("Gemini API error: %s", err)
        tracing.end_run(ls_run, error=err)
        if "429" in err or "quota" in err.lower() or "ResourceExhausted" in err:
            yield "\n\n[Error] Gemini quota exceeded. Wait a moment or check https://ai.dev/rate-limit\n"
        else:
            yield f"\n\n[Error] {err}\n"
        return

    tracing.end_run(ls_run, outputs={"response": "".join(response_chunks)})


# ── Claude CLI chat turn ──────────────────────────────────────────────────────

def chat_turn_claude(session_id: str, user_message: str):
    """Run one full Claude CLI turn (including tool loops) and yield text chunks."""
    ls_run = tracing.start_run(
        "chat/claude",
        {"message": user_message, "model": MODEL_NAME, "session_id": session_id},
    )
    history = _claude_sessions.setdefault(session_id, [])
    history.append({"role": "user", "content": user_message})

    system = SYSTEM_PROMPT + build_network_context()

    response_chunks: list[str] = []
    try:
        while True:
            response = claude_client.messages.create(
                system=system,
                tools=T.TOOL_SCHEMAS,
                messages=history,
            )

            text_parts  = [b.text  for b in response.content if b.type == "text"]
            tool_blocks = [b       for b in response.content if b.type == "tool_use"]

            if text_parts:
                chunk = "".join(text_parts)
                response_chunks.append(chunk)
                yield chunk

            if response.stop_reason == "end_turn" or not tool_blocks:
                history.append({"role": "assistant", "content": response.content})
                break

            history.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tb in tool_blocks:
                yield f"\n\n*[calling tool: {tb.name}...]*\n"
                result = execute_tool(tb.name, tb.input, _parent_run=ls_run)
                log.info("Tool %s -> %s", tb.name, str(result)[:120])
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tb.id,
                    "content":     json.dumps(result, default=str),
                })

            history.append({"role": "user", "content": tool_results})
            yield "\n"

    except Exception as e:
        log.error("Claude CLI error: %s", e)
        tracing.end_run(ls_run, error=str(e))
        yield f"\n\n[Error] {e}\n"
        return

    tracing.end_run(ls_run, outputs={"response": "".join(response_chunks)})


# ── API routes ────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message:    str
    session_id: str = "default"


@app.get("/health")
def health():
    backend = "claude-cli" if USE_CLAUDE else "gemini"
    return {"status": "ok", "model": MODEL_NAME, "backend": backend}


@app.get("/tools")
def list_tools():
    return [{"name": t["name"], "description": t["description"]} for t in T.TOOL_SCHEMAS]


@app.post("/chat")
def chat(req: ChatRequest):
    gen = (chat_turn_claude(req.session_id, req.message)
           if USE_CLAUDE
           else chat_turn_gemini(req.session_id, req.message))
    return StreamingResponse(gen, media_type="text/plain")


@app.get("/history")
def get_history(session_id: str = "default"):
    if USE_CLAUDE:
        history = _claude_sessions.get(session_id, [])
        normalized = []
        for msg in history:
            role    = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str):
                normalized.append({"role": role, "content": content})
            elif isinstance(content, list):
                texts = []
                for item in content:
                    if hasattr(item, "text"):              # TextBlock
                        texts.append(item.text)
                    elif hasattr(item, "name"):             # ToolUseBlock
                        texts.append(f"[Calling {item.name}]")
                    elif isinstance(item, dict):
                        if item.get("type") == "tool_result":
                            texts.append(f"[Tool result: {item.get('tool_use_id', '')}]")
                        elif item.get("type") == "text":
                            texts.append(item.get("text", ""))
                normalized.append({"role": role, "content": " ".join(texts)})
        return normalized
    else:
        history = _gemini_sessions.get(session_id, [])
        normalized = []
        for content in history:
            role  = getattr(content, "role", "?")
            texts = []
            for part in getattr(content, "parts", []) or []:
                if getattr(part, "text", None):
                    texts.append(part.text)
                elif getattr(part, "function_call", None) and part.function_call.name:
                    texts.append(f"[Calling {part.function_call.name}]")
                elif getattr(part, "function_response", None) and part.function_response.name:
                    texts.append(f"[Tool result: {part.function_response.name}]")
            normalized.append({
                "role":    "assistant" if role == "model" else role,
                "content": " ".join(texts),
            })
        return normalized


@app.delete("/history")
def clear_history(session_id: str = "default"):
    if USE_CLAUDE:
        _claude_sessions.pop(session_id, None)
    else:
        _gemini_sessions.pop(session_id, None)
    return {"status": "cleared", "session_id": session_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("orchestrator:app", host="0.0.0.0", port=8082, reload=False)
