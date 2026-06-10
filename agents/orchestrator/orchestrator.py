#!/usr/bin/env python3
"""
Telecom Network Orchestrator — LLM chat agent with Google Gemini tool-calling.

POST /chat    {"message": "...", "session_id": "default"} → streaming text
GET  /history {"session_id": "default"}
DELETE /history
GET  /tools
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
MODEL_NAME     = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

T.CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://controller:8080")
T.PLANNING_URL   = os.environ.get("PLANNING_URL",   "http://planning-api:8081")
T.INFLUX_URL     = os.environ.get("INFLUX_URL",     "http://influxdb:8086")
T.INFLUX_TOKEN   = os.environ.get("INFLUX_TOKEN",   "telecom-super-secret-auth-token-2026")
T.INFLUX_ORG     = os.environ.get("INFLUX_ORG",     "telecom")
T.INFLUX_BUCKET  = os.environ.get("INFLUX_BUCKET",  "telecom_metrics")

gemini = genai.Client(api_key=GOOGLE_API_KEY)

app = FastAPI(title="Telecom Orchestrator", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# session_id -> list of types.Content objects (full conversation history)
_sessions: dict[str, list] = {}

SYSTEM_PROMPT = """You are an expert RAN operations assistant for a Bangalore 4G/5G NSA deployment.
You have access to tools to query live network state, move RAN components, run the planning engine, and retrieve alerts.

Network overview (South-East Bangalore Tech Corridor):
- 40 cells across 10 areas (4 cells per area, blanket coverage):
    Whitefield, Marathahalli, KR Puram, Bellandur, Indiranagar,
    Koramangala, HSR Layout, BTM Layout, Jayanagar, Electronic City
- Each area has: 1× 5G n78 (capacity), 1× 5G n28 (coverage), 1× 4G B3, 1× 4G B40
- Mixed 4G LTE (B3/B40) and 5G NR (n78/n28) — NSA core, shared AMF/SMF/UPF
- 10 Distributed Units (one per area) across 2 Centralised Units:
    CU-EAST  → DU-EAST-1 (Whitefield), DU-EAST-2 (Marathahalli),
               DU-EAST-3 (KR Puram), DU-EAST-4 (Bellandur)
    CU-SOUTH → DU-CENTRAL-1 (Indiranagar), DU-CENTRAL-2 (Koramangala),
               DU-SOUTH-1 (HSR Layout), DU-SOUTH-2 (BTM Layout),
               DU-SOUTH-3 (Jayanagar), DU-SOUTH-4 (Electronic City)
- Multi-vendor 25% each (10 cells each): Nokia AirScale MAA 64T64R / AWHFA,
  Ericsson AIR 6449 / RBS 6402, Samsung TM500 64T64R / RRU, ZTE AAU 5614 / RRU
- Scale: 1.625M active UEs (50% of Bangalore 13M pop × 25% operator share)
- 5G NR n78 (3.5 GHz, 64T64R): up to 3800 Mbps peak, 680–800 UEs/cell, 900–1000 W
- 5G NR n28 (700 MHz, 64T64R): up to 3800 Mbps peak, 680–800 UEs/cell, 900–1000 W
- 4G LTE B3/B40: 150 Mbps, 340–400 UEs/cell, 200 W

Guidelines:
- Always call query_network first if you need current state before taking actions.
- Explain what you observe before acting. Ask for confirmation before move_cell/move_du/apply_plan.
- When reporting KPIs, flag: overloaded cells (PRB > 85%), SINR below 5 dB, power waste (high W, few UEs).
- 5G cells at idle draw ~225–260 W — POWER_WASTE is only actionable when power is high relative to UEs.
- When the operator asks to plan a network, call plan_network and summarise before asking to apply it.
- Be concise. Bullet points are fine for status summaries.
"""


# ── Tool schemas (Anthropic → Gemini format) ──────────────────────────────────

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


# ── Tool execution ────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict) -> dict:
    fn = T.TOOL_MAP.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        result = fn(args)
        if not isinstance(result, dict):
            result = {"result": result}
        # Sanitise — proto Struct cannot hold non-serialisable values
        return json.loads(json.dumps(result, default=str))
    except Exception as e:
        return {"error": str(e)}


# ── Chat logic ────────────────────────────────────────────────────────────────

def chat_turn(session_id: str, user_message: str):
    """Run one full turn (including tool loops) and yield text chunks."""
    history = _sessions.setdefault(session_id, [])
    history.append(types.Content(
        role="user",
        parts=[types.Part(text=user_message)],
    ))

    system = SYSTEM_PROMPT + build_network_context()
    config = types.GenerateContentConfig(
        system_instruction=system,
        tools=GEMINI_TOOLS,
    )

    try:
        while True:
            response = gemini.models.generate_content(
                model=MODEL_NAME,
                contents=history,
                config=config,
            )

            model_content = response.candidates[0].content
            history.append(model_content)

            text_parts = [p.text for p in model_content.parts if getattr(p, "text", None)]
            tool_calls = [p.function_call for p in model_content.parts
                          if getattr(p, "function_call", None) and p.function_call.name]

            if text_parts:
                yield "".join(text_parts)

            if not tool_calls:
                break

            # Execute every requested tool
            fn_parts = []
            for tc in tool_calls:
                yield f"\n\n*[calling tool: {tc.name}...]*\n"
                result = execute_tool(tc.name, dict(tc.args))
                log.info("Tool %s → %s", tc.name, str(result)[:120])
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
        if "429" in err or "quota" in err.lower() or "ResourceExhausted" in err:
            yield "\n\n[Error] Gemini quota exceeded. Wait a moment or check https://ai.dev/rate-limit\n"
        else:
            yield f"\n\n[Error] {err}\n"


# ── API routes ────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message:    str
    session_id: str = "default"


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME}


@app.get("/tools")
def list_tools():
    return [{"name": t["name"], "description": t["description"]} for t in T.TOOL_SCHEMAS]


@app.post("/chat")
def chat(req: ChatRequest):
    return StreamingResponse(
        chat_turn(req.session_id, req.message),
        media_type="text/plain",
    )


@app.get("/history")
def get_history(session_id: str = "default"):
    history = _sessions.get(session_id, [])
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
    _sessions.pop(session_id, None)
    return {"status": "cleared", "session_id": session_id}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("orchestrator:app", host="0.0.0.0", port=8082, reload=False)
