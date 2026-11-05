#!/usr/bin/env python3
"""Forward ONI tool calls to the game Mod; make no decisions here.

The tool list lives in tools/oni-tools.json and is exposed in two forms:

    python tools/oni_tools.py mcp                      MCP server on stdio
    python tools/oni_tools.py call get_status '{}'     one call from the shell

``openai_tools()`` gives the same list as OpenAI function-calling tools.
The standard library is enough: MCP over stdio is newline-delimited JSON-RPC.
"""

from __future__ import annotations

import http.client
import json
import os
import pathlib
import socket
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, TextIO

SCHEMA_PATH = pathlib.Path(__file__).resolve().with_name("oni-tools.json")
MCP_PROTOCOL_VERSION = "2025-06-18"
# Longer than the Mod's main-thread timeout, so the Mod's answer always arrives.
CALL_TIMEOUT_SECONDS = 30.0
WAIT_LIMIT_SECONDS = 60.0
WAIT_POLL_SECONDS = 2.0


def bridge_dir() -> pathlib.Path:
    configured = os.environ.get("ONI_AI_BRIDGE_DIR", "").strip()
    if configured:
        return pathlib.Path(configured)
    return pathlib.Path.home() / "Documents" / "Klei" / "OxygenNotIncluded" / "ai-bridge"


def load_tools() -> List[Dict[str, Any]]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["tools"]


def _unknown_arguments(tool: str, args: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Refuse a misspelled argument: the Mod would read it as simply absent."""
    schema = next((entry for entry in load_tools() if entry["name"] == tool), None)
    if schema is None:
        return None
    known = schema["parameters"].get("properties", {})
    unknown = sorted(name for name in args if name not in known)
    if not unknown:
        return None
    return _failure(
        "invalid_argument",
        f"{tool} has no argument {', '.join(unknown)}.",
        f"It takes: {', '.join(known) or 'nothing'}.",
    )


def openai_tools() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in load_tools()
    ]


def _failure(code: str, message: str, hint: Optional[str] = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if hint:
        error["hint"] = hint
    return {"ok": False, "error": error}


def _needs_attention(event: Mapping[str, Any]) -> bool:
    if event.get("type") in ("duplicant_lost", "duplicant_low_breath"):
        return True
    return event.get("type") == "alert_raised" and event.get("severity") in ("critical", "bad")


def wait(args: Mapping[str, Any]) -> Dict[str, Any]:
    """Let the game run, and come back early if something needs the player."""
    seconds = min(max(float(args.get("seconds", 10)), 0.0), WAIT_LIMIT_SECONDS)
    since = int(args.get("since", 0))
    deadline = time.monotonic() + seconds
    events: List[Dict[str, Any]] = []
    backlog = True  # Events already waiting are news, but no reason to cut this wait short.
    while True:
        outcome = call_mod("get_events", {"since": since})
        if not outcome.get("ok"):
            return outcome
        arrived = outcome["result"]["events"]
        events += arrived
        since = outcome["result"]["next_since"]
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (not backlog and any(_needs_attention(event) for event in arrived)):
            break
        if outcome["result"].get("more"):
            continue
        backlog = False
        time.sleep(min(WAIT_POLL_SECONDS, remaining))
    return {
        "ok": True,
        "result": {
            "waited_seconds": round(seconds - max(deadline - time.monotonic(), 0.0), 1),
            "events": events,
            "next_since": since,
        },
    }


def call(tool: str, args: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Run one tool.  Always returns the Mod's response shape, never raises."""
    refused = _unknown_arguments(tool, args or {})
    if refused:
        return refused
    if tool == "wait":
        return wait(args or {})
    return call_mod(tool, args)


def call_mod(tool: str, args: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    api_file = bridge_dir() / "api.json"
    try:
        api = json.loads(api_file.read_text(encoding="utf-8"))
        port = int(api["port"])
        token = str(api["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return _failure(
            "game_unavailable",
            "No running game API was found.",
            "Start one with scripts/api_session.py start.",
        )

    body = json.dumps({"tool": tool, "args": dict(args or {})}, ensure_ascii=False).encode("utf-8")
    # http.client, not urllib: it cannot pick up the machine's proxy settings.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=CALL_TIMEOUT_SECONDS)
    try:
        connection.request(
            "POST",
            "/call",
            body=body,
            headers={"Content-Type": "application/json", "X-Oni-Token": token},
        )
        response = connection.getresponse()
        return json.loads(response.read().decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError, socket.timeout) as exc:
        return _failure(
            "game_unavailable",
            f"The game did not answer: {exc}",
            "The game may have exited; check get_lifecycle or the process.",
        )
    finally:
        connection.close()


def _mcp_result(request_id: Any, result: Mapping[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _mcp_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_mcp(message: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Answer one JSON-RPC message.  Notifications get no reply."""
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    if method == "initialize":
        params = message.get("params") or {}
        return _mcp_result(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion") or MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oni", "version": "1.0.0"},
            },
        )
    if method == "ping":
        return _mcp_result(request_id, {})
    if method == "tools/list":
        return _mcp_result(
            request_id,
            {
                "tools": [
                    {
                        "name": tool["name"],
                        "description": tool["description"],
                        "inputSchema": tool["parameters"],
                    }
                    for tool in load_tools()
                ]
            },
        )
    if method == "tools/call":
        params = message.get("params") or {}
        outcome = call(str(params.get("name")), params.get("arguments") or {})
        # A refused call is a result the player reads and acts on, not a protocol error.
        text = json.dumps(
            outcome["result"] if outcome.get("ok") else outcome["error"],
            ensure_ascii=False,
        )
        return _mcp_result(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": not outcome.get("ok")},
        )
    return _mcp_error(request_id, -32601, f"Unknown method: {method}")


def serve_mcp(stdin: TextIO, stdout: TextIO) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: Optional[Dict[str, Any]] = _mcp_error(None, -32700, "Parse error")
        else:
            reply = handle_mcp(message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: List[str]) -> int:
    # Tool results carry Chinese names; the Windows console default would corrupt them.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) == 2 and argv[1] == "mcp":
        serve_mcp(sys.stdin, sys.stdout)
        return 0
    if len(argv) in (3, 4) and argv[1] == "call":
        outcome = call(argv[2], json.loads(argv[3]) if len(argv) == 4 else {})
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 0 if outcome.get("ok") else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
