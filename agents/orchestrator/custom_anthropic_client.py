"""
CustomAnthropicClient — a `claude -p` stand-in for `client.messages.create`
---------------------------------------------------------------------------
We have no Anthropic API key, but the `claude` CLI is installed and logged in.
This class lets the blocks keep their hand-written agent loops unchanged: it
mimics the Anthropic Messages API surface (`client.messages.create(...)`) but is
backed by `claude -p` under the hood.

The tricky part: `claude -p` is itself an agent that returns free text and can
run its own tools, whereas `client.messages.create` returns STRUCTURED output
(`stop_reason` + `content` blocks carrying `tool_use` intents). We bridge that
gap entirely inside this class:

  1. Flatten `system` + `tools` + the full `messages` history into ONE prompt
     string (this is the conversation memory — no --continue/--resume).
  2. Hardcode guardrails telling claude -p to NOT run tools or its own loop, and
     to reply with EXACTLY ONE JSON object in a fixed schema.
  3. Parse that JSON back into duck-typed objects that look just like an
     Anthropic response (`.stop_reason`, `.content[*].type/.id/.name/.input/.text`).

The caller's loop still owns tool *dispatch* — claude -p only ever NAMES a tool.
That preserves the Block 2 teaching point: frameworks are just this loop.

Config (read from .env):
  CLAUDE_CLI_PATH       absolute path to the `claude` binary
  ANTHROPIC_MODEL_NAME  model passed to `claude -p --model <value>` (e.g. "haiku")
"""

import os
import json
import time
import uuid
import subprocess

from dotenv import load_dotenv

load_dotenv()

# How long to wait for a single CLI round-trip before giving up.
_CLI_TIMEOUT_SECONDS = 120


# ── Duck-typed response objects ───────────────────────────────────────────────
#
# These mimic the shape of an Anthropic response so the existing agent loops read
# them unchanged: `response.stop_reason`, `response.content`, and per-block
# `.type` / `.id` / `.name` / `.input` / `.text`.
#
# They also have to SERIALIZE back into a prompt, because every loop does
#   messages.append({"role": "assistant", "content": response.content})
# and we re-inject the whole history on the next call (see _serialize_messages).


class ToolUseBlock:
    """Mirror of an Anthropic `tool_use` content block."""

    type = "tool_use"

    def __init__(self, name: str, input: dict, id: str = None):
        self.name = name
        self.input = input or {}
        self.id = id or f"toolu_{uuid.uuid4().hex[:24]}"


class TextBlock:
    """Mirror of an Anthropic `text` content block."""

    type = "text"

    def __init__(self, text: str):
        self.text = text


class Response:
    """Mirror of an Anthropic Message response (only the fields the loops read)."""

    def __init__(self, stop_reason: str, content: list):
        self.stop_reason = stop_reason
        self.content = content


# ── The client ────────────────────────────────────────────────────────────────


class _Messages:
    """Namespace object so `client.messages.create(...)` works verbatim."""

    def __init__(self, parent: "CustomAnthropicClient"):
        self._parent = parent

    def create(self, model=None, system=None, tools=None, messages=None, **ignored):
        # max_tokens / temperature / other SDK kwargs are accepted and ignored
        # on purpose (see SPEC.md decision #10).
        return self._parent._create(system=system, tools=tools, messages=messages or [])


class CustomAnthropicClient:
    """Drop-in replacement for `anthropic.Anthropic()`."""

    def __init__(self):
        self.cli_path = os.environ.get("CLAUDE_CLI_PATH")
        if not self.cli_path:
            raise RuntimeError(
                "CLAUDE_CLI_PATH is not set. Add it to .env "
                "(e.g. CLAUDE_CLI_PATH=/path/to/claude)."
            )
        self.model = os.environ.get("ANTHROPIC_MODEL_NAME", "haiku")
        self.messages = _Messages(self)

    # ── Connectivity self-test ────────────────────────────────────────────────

    def test_connection(self) -> bool:
        """
        Run a trivial `claude -p` round-trip to confirm the CLI is reachable and
        authenticated. Mirrors test_gemini.py. Returns True on success, prints and
        returns False otherwise.
        """
        try:
            raw = self._call_cli("Reply with exactly: API access confirmed")
            text = self._extract_result_text(raw)
            print(f"Success: claude CLI reachable (model={self.model}) -> {text!r}")
            return True
        except Exception as e:  # noqa: BLE001 — surface anything that goes wrong
            print(f"Failed: {e}")
            return False

    # ── The create() bridge ───────────────────────────────────────────────────

    def _create(self, system, tools, messages) -> Response:
        prompt = self._build_prompt(system, tools, messages)
        raw = self._call_cli(prompt)
        result_text = self._extract_result_text(raw)
        return self._parse(result_text)

    # ── Prompt assembly ───────────────────────────────────────────────────────

    def _build_prompt(self, system, tools, messages) -> str:
        parts = []

        # 1. Hardcoded guardrails — claude -p must behave like a stateless
        #    completion endpoint, not an agent.
        parts.append(
            "You are a stateless completion endpoint that emulates the Anthropic "
            "Messages API. Follow these rules EXACTLY:\n"
            "- Do NOT run, execute, or simulate any tools yourself.\n"
            "- Do NOT start your own agent loop or take multiple turns.\n"
            "- Produce a SINGLE response for the latest turn only.\n"
            "- Reply with EXACTLY ONE JSON object and NOTHING else: no prose, no "
            "explanation, no markdown code fences."
        )

        # 2. The caller's system prompt (the agent's personality/constraints).
        if system:
            parts.append("## System instructions\n" + str(system))

        # 3. Tools as documentation + the response contract.
        if tools:
            tool_docs = []
            for t in tools:
                schema = t.get("input_schema", {})
                tool_docs.append(
                    f"- {t['name']}: {t.get('description', '')}\n"
                    f"  input_schema: {json.dumps(schema)}"
                )
            parts.append(
                "## Available tools\n"
                + "\n".join(tool_docs)
                + "\n\n## Response format\n"
                "If you need to call a tool to make progress, respond with:\n"
                '  {"type": "tool_use", "name": "<tool_name>", "input": { ...args... }}\n'
                "Otherwise, give your final answer to the user with:\n"
                '  {"type": "text", "text": "<your reply>"}\n'
                "Call only ONE tool per response. Use tool input that matches the "
                "schema. Do not invent tools that are not listed."
            )
        else:
            parts.append(
                "## Response format\n"
                "Respond with your final answer as a single JSON object:\n"
                '  {"type": "text", "text": "<your reply>"}'
            )

        # 4. The conversation so far — this IS the session memory.
        parts.append("## Conversation so far\n" + self._serialize_messages(messages))

        parts.append(
            "Now produce the single JSON object for the assistant's next turn."
        )
        return "\n\n".join(parts)

    def _serialize_messages(self, messages) -> str:
        """
        Render the messages[] history as plain text. Handles the three content
        shapes the loops produce:
          - user, content=str                      → the user's request / a note
          - assistant, content=[blocks]            → our ToolUseBlock / TextBlock
          - user, content=[tool_result dicts]      → results we fed back in
        """
        lines = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")

            if isinstance(content, str):
                lines.append(f"[{role}] {content}")
                continue

            if isinstance(content, list):
                for item in content:
                    lines.append(f"[{role}] {self._serialize_item(item)}")
                continue

            # Fallback for any unexpected shape.
            lines.append(f"[{role}] {content}")

        return "\n".join(lines) if lines else "(no prior messages)"

    def _serialize_item(self, item) -> str:
        # Our duck-typed blocks (assistant turns).
        if isinstance(item, ToolUseBlock):
            return f"called tool {item.name} (id={item.id}) with input {json.dumps(item.input)}"
        if isinstance(item, TextBlock):
            return item.text

        # tool_result dicts (user turns) or any raw dict blocks.
        if isinstance(item, dict):
            if item.get("type") == "tool_result":
                return (
                    f"tool result for {item.get('tool_use_id')}: {item.get('content')}"
                )
            if item.get("type") == "text":
                return item.get("text", "")
            if item.get("type") == "tool_use":
                return (
                    f"called tool {item.get('name')} with input "
                    f"{json.dumps(item.get('input', {}))}"
                )
            return json.dumps(item)

        return str(item)

    # ── CLI invocation ────────────────────────────────────────────────────────

    def _call_cli(self, prompt: str) -> str:
        """
        Invoke `claude -p` with the prompt on stdin (avoids ARG_MAX). Returns raw
        stdout. Prints elapsed time before/after so the per-turn cold-start cost
        is visible.
        """
        cmd = [
            self.cli_path,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
        ]

        print(f"[claude -p] invoking (model={self.model}) ...")
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=_CLI_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            print(f"[claude -p] TIMEOUT after {elapsed:.2f}s")
            raise RuntimeError(
                f"claude -p timed out after {_CLI_TIMEOUT_SECONDS}s"
            )
        elapsed = time.monotonic() - start
        print(f"[claude -p] returned in {elapsed:.2f}s (exit={proc.returncode})")

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude -p failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        if not proc.stdout.strip():
            raise RuntimeError(
                f"claude -p produced no output. stderr: {proc.stderr.strip()}"
            )
        return proc.stdout

    def _extract_result_text(self, raw_stdout: str) -> str:
        """
        Pull the model's text out of the `--output-format json` envelope:
          {"type":"result","result":"<text>", "is_error":false, ...}
        """
        try:
            envelope = json.loads(raw_stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Could not parse claude -p JSON envelope: {e}\nRaw: {raw_stdout[:500]}"
            )
        if envelope.get("is_error"):
            raise RuntimeError(f"claude -p reported an error: {envelope.get('result')}")
        result = envelope.get("result")
        if result is None:
            raise RuntimeError(
                f"claude -p envelope had no 'result' field: {raw_stdout[:500]}"
            )
        return result

    # ── Response parsing ──────────────────────────────────────────────────────

    def _parse(self, result_text: str) -> Response:
        """
        Turn the model's JSON contract object into a duck-typed Response:
          {"type":"tool_use","name":...,"input":{...}} → stop_reason="tool_use"
          {"type":"text","text":...}                   → stop_reason="end_turn"
        """
        obj = self._extract_json_object(result_text)

        kind = obj.get("type")
        if kind == "tool_use":
            block = ToolUseBlock(name=obj["name"], input=obj.get("input", {}))
            return Response(stop_reason="tool_use", content=[block])

        if kind == "text":
            return Response(stop_reason="end_turn", content=[TextBlock(obj.get("text", ""))])

        # The model ignored the contract. Don't silently mis-drive the loop —
        # fall back to treating the whole thing as a final text answer.
        return Response(stop_reason="end_turn", content=[TextBlock(result_text.strip())])

    def _extract_json_object(self, text: str) -> dict:
        """
        Parse a single JSON object out of the model's reply, tolerating stray
        markdown fences or surrounding prose.
        """
        cleaned = text.strip()

        # Strip ```json ... ``` or ``` ... ``` fences if present.
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)
            cleaned = cleaned[1] if len(cleaned) > 1 else text
            if cleaned.startswith("json"):
                cleaned = cleaned[len("json"):]
            cleaned = cleaned.strip().rstrip("`").strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Last resort: grab the first {...last} span and try that.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass

        # Couldn't find structured output — signal "plain text" to the caller.
        return {"type": "text", "text": text.strip()}
