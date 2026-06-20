#!/usr/bin/env python3
"""
Telecom Orchestrator CLI — interactive chat with the network AI agent.

Usage:
    py chat.py
    py chat.py --url http://localhost:8082   # if orchestrator runs elsewhere
    py chat.py --session ops-team            # named session (keeps history)

Commands (type in the chat):
    /status     - quick network status
    /alerts     - show recent KPI alerts
    /cells      - list all cells
    /plan       - generate a network plan
    /son        - show recent SON autonomous actions
    /ue         - show UE usage and mobility events
    /history    - print conversation history
    /clear      - reset conversation
    /tools      - list available agent tools
    quit / exit - exit the CLI
"""

import sys
import json
import argparse
import urllib.request
import urllib.error

def parse_args():
    p = argparse.ArgumentParser(description="Telecom Orchestrator Chat CLI")
    p.add_argument("--url",     default="http://localhost:8082", help="Orchestrator base URL")
    p.add_argument("--session", default="default",               help="Session ID")
    return p.parse_args()


def post(url: str, body: dict) -> str:
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        return f"[HTTP {e.code}] {e.read().decode()}"
    except Exception as e:
        return f"[Error] {e}"


def get(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read().decode()
    except Exception as e:
        return f"[Error] {e}"


def delete(url: str) -> str:
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()
    except Exception as e:
        return f"[Error] {e}"


SHORTCUTS = {
    "/status":  "What is the current status of all cells, DUs, and CUs? Summarise in a table.",
    "/alerts":  "Show me all recent KPI alerts from the last 60 minutes.",
    "/cells":   "List all cells with their current connected UEs, PRB utilisation, and DU assignment.",
    "/plan":    "I want to plan a network deployment. Ask me for all the required parameters before proceeding.",
    "/son":     "Show me the recent SON autonomous actions and their outcomes.",
    "/ue":      "Show me UE usage and mobility events from the last 30 minutes.",
}


def main():
    args    = parse_args()
    base    = args.url.rstrip("/")
    session = args.session

    # Check orchestrator health
    health = get(f"{base}/health")
    try:
        h = json.loads(health)
        print(f"\n  Telecom Orchestrator  |  model: {h.get('model','?')}  |  {base}")
    except Exception:
        print(f"\n  WARNING: Orchestrator may not be running at {base}")
        print(f"  Response: {health}\n")

    print("  Type a message or a shortcut (/status /alerts /cells /plan /son /ue)")
    print("  /history  /clear  /tools  |  quit to exit")
    print("  " + "─" * 60 + "\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye.")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye.")
            break

        if user_input == "/history":
            raw = get(f"{base}/history?session_id={session}")
            try:
                hist = json.loads(raw)
                for msg in hist:
                    role = msg.get("role", "?").upper()
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        print(f"  [{role}] {content[:200]}")
            except Exception:
                print(raw)
            print()
            continue

        if user_input == "/clear":
            result = delete(f"{base}/history?session_id={session}")
            print(f"  History cleared.\n")
            continue

        if user_input == "/tools":
            raw = get(f"{base}/tools")
            try:
                tools = json.loads(raw)
                for t in tools:
                    print(f"  • {t['name']:20s} — {t['description'][:70]}")
            except Exception:
                print(raw)
            print()
            continue

        # Expand shortcuts
        message = SHORTCUTS.get(user_input, user_input)

        print("\nAgent: ", end="", flush=True)
        response = post(f"{base}/chat", {"message": message, "session_id": session})
        print(response)
        print()


if __name__ == "__main__":
    main()
