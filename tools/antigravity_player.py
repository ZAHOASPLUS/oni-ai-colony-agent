#!/usr/bin/env python3
"""Run the ONI tool set through Google Antigravity (direct session or Gemini API).

The Mod and ``tools/oni-tools.json`` remain the single source of truth for game
access.  This module adds the Antigravity side of the same boundary:

1. ``AntigravitySession`` (CLI subcommands ``status``, ``step``, ``wait``,
   ``call``, ``mcp-config``): lets an interactive Antigravity agent drive ONI
   directly while persisting ``cursor.json`` and ``steps.jsonl`` under
   ``reports/play/<run>/`` and capping each ``wait`` poll to <= 30 seconds.
2. ``AntigravityPlayer`` (CLI subcommand ``run``): dependency-free Gemini /
   Antigravity ``generateContent`` loop sending ``antigravity_tools()``
   function declarations, executing ``functionCall`` parts via
   ``oni_tools.call``, preserving ``thought`` / ``thoughtSignature`` parts, and
   returning ``functionResponse`` turns until a final assistant text response.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import pathlib
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from oni_tools import antigravity_tools, load_tools  # noqa: E402
from oni_tools import call as call_oni_tool  # noqa: E402


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-pro"
DEFAULT_MAX_ROUNDS = 8
DEFAULT_TIMEOUT_SECONDS = 80.0
MAX_SESSION_WAIT_SECONDS = 30.0
DEFAULT_WAIT_SECONDS = 15.0
MAX_ERROR_BODY_CHARS = 500

TRIVIAL_BUILD_DEFS = frozenset(
    {
        "Wire",
        "WireRefined",
        "WireBridge",
        "LogicWire",
        "LogicWireBridge",
        "GasConduit",
        "InsulatedGasConduit",
        "GasConduitBridge",
        "LiquidConduit",
        "InsulatedLiquidConduit",
        "LiquidConduitBridge",
        "Tile",
        "InsulationTile",
        "MeshTile",
        "AirflowTile",
        "Ladder",
        "LadderFast",
    }
)


class AntigravityPlayerError(RuntimeError):
    """A local adapter or provider failure with a stable machine code."""

    def __init__(self, code: str, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _error(code: str, message: str, hint: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if hint:
        result["error"]["hint"] = hint
    return result


def _tool_names() -> set[str]:
    return {str(item["name"]) for item in load_tools()}


def _redact_secret(text: str, secret: str) -> str:
    return text.replace(secret, "[REDACTED]") if secret else text


def _generate_content_url(base_url: str, model: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise AntigravityPlayerError("endpoint_unset", "Antigravity/Gemini base URL is empty.")
    clean_model = str(model or "").strip()
    if not clean_model:
        raise AntigravityPlayerError("model_unset", "Antigravity/Gemini model is empty.")
    if base.endswith(":generateContent"):
        return base
    if clean_model.startswith("models/"):
        model_path = "/".join(urllib.parse.quote(part, safe="-._") for part in clean_model.split("/"))
    else:
        model_path = "models/" + urllib.parse.quote(clean_model, safe="-._")
    return f"{base}/{model_path}:generateContent"


def _normalize_user_contents(
    contents: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in contents:
        entry = copy.deepcopy(dict(item))
        if "parts" in entry:
            entry.setdefault("role", "user")
            normalized.append(entry)
            continue
        role = str(entry.get("role") or "user")
        if role == "assistant":
            role = "model"
        text = str(entry.get("content") or "")
        normalized.append({"role": role, "parts": [{"text": text}]})
    return normalized


class AntigravityPlayer:
    """Dependency-free tool loop for Gemini / Antigravity ``generateContent``."""

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        tool_executor: Callable[[str, Optional[Mapping[str, Any]]], Dict[str, Any]] = call_oni_tool,
        request: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        resolved_model = (
            model
            if model is not None
            else (os.environ.get("ANTIGRAVITY_MODEL") or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL)
        )
        resolved_base = (
            base_url
            if base_url is not None
            else (os.environ.get("ANTIGRAVITY_BASE_URL") or os.environ.get("GEMINI_BASE_URL") or DEFAULT_BASE_URL)
        )
        resolved_key = (
            api_key
            if api_key is not None
            else (os.environ.get("ANTIGRAVITY_API_KEY") or os.environ.get("GEMINI_API_KEY") or "")
        )
        self.model = str(resolved_model).strip()
        self.base_url = str(resolved_base).strip()
        self.api_key = str(resolved_key).strip()
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 300.0))
        self.max_rounds = max(1, int(max_rounds))
        self.tool_executor = tool_executor
        self._request_override = request
        if not self.model:
            raise AntigravityPlayerError("model_unset", "Antigravity/Gemini model is empty.")
        if not self.base_url:
            raise AntigravityPlayerError("endpoint_unset", "Antigravity/Gemini base URL is empty.")

    def _http_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            raise AntigravityPlayerError(
                "missing_api_key",
                "ANTIGRAVITY_API_KEY or GEMINI_API_KEY is not set.",
                status=401,
            )
        url = _generate_content_url(self.base_url, self.model)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = _redact_secret(raw[:MAX_ERROR_BODY_CHARS], self.api_key)
            raise AntigravityPlayerError(
                f"http_{exc.code}",
                f"Antigravity/Gemini endpoint returned HTTP {exc.code}: {detail}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise AntigravityPlayerError(
                "transport_error",
                f"Antigravity/Gemini endpoint did not answer: {_redact_secret(str(exc), self.api_key)}",
            ) from exc
        try:
            response_payload = json.loads(raw)
        except ValueError as exc:
            raise AntigravityPlayerError("invalid_json_response", "Endpoint returned invalid JSON.") from exc
        if not isinstance(response_payload, dict):
            raise AntigravityPlayerError("invalid_response", "Endpoint returned a non-object JSON value.")
        if response_payload.get("error"):
            provider_error = response_payload["error"]
            if isinstance(provider_error, Mapping):
                message = str(provider_error.get("message") or provider_error.get("status") or "provider error")
            else:
                message = str(provider_error)
            raise AntigravityPlayerError("provider_error", _redact_secret(message, self.api_key))
        return response_payload

    def _request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._request_override is not None:
            response = self._request_override(payload)
            if not isinstance(response, dict):
                raise AntigravityPlayerError("invalid_response", "Injected transport returned a non-object.")
            return response
        return self._http_request(payload)

    def _execute_function_call(self, fn_call: Any) -> Dict[str, Any]:
        if not isinstance(fn_call, Mapping):
            outcome = _error("invalid_tool_call", "The model returned a malformed functionCall.")
            return {"functionResponse": {"name": "invalid_tool_call", "response": outcome}}
        name = str(fn_call.get("name") or "").strip()
        call_id = fn_call.get("id")
        if not name:
            outcome = _error("invalid_tool_call", "The model functionCall has no name.")
            resp: Dict[str, Any] = {"name": "invalid_tool_call", "response": outcome}
            if call_id:
                resp["id"] = str(call_id)
            return {"functionResponse": resp}

        raw_args = fn_call.get("args", {})
        if raw_args is None:
            raw_args = {}
        if isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args)
            except ValueError as exc:
                outcome = _error("invalid_tool_arguments", f"Arguments for {name} are not valid JSON: {exc}")
                resp = {"name": name, "response": outcome}
                if call_id:
                    resp["id"] = str(call_id)
                return {"functionResponse": resp}
        else:
            arguments = raw_args

        if name not in _tool_names():
            outcome = _error("unknown_tool", f"The ONI tool {name!r} does not exist.")
        elif not isinstance(arguments, Mapping):
            outcome = _error("invalid_tool_arguments", f"Arguments for {name} must be a JSON object.")
        else:
            try:
                outcome = self.tool_executor(name, arguments)
            except Exception as exc:  # pragma: no cover - defensive boundary for a live game process
                outcome = _error("tool_executor_error", f"The ONI tool {name} failed before returning a result: {exc}")
            if not isinstance(outcome, dict):
                outcome = _error("tool_executor_error", f"The ONI tool {name} returned a non-object result.")

        response_part: Dict[str, Any] = {"name": name, "response": outcome}
        if call_id:
            response_part["id"] = str(call_id)
        return {"functionResponse": response_part}

    def run(
        self,
        contents: Sequence[Mapping[str, Any]],
        *,
        system_instruction: Optional[str] = None,
        tool_mode: str = "AUTO",
        generation_config: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run until a final text answer or a bounded local failure."""

        if not contents:
            return {"ok": False, "error": {"code": "messages_empty", "message": "At least one message is required."}, "contents": []}
        history = _normalize_user_contents(contents)

        for round_index in range(1, self.max_rounds + 1):
            payload: Dict[str, Any] = {
                "contents": history,
                "tools": [{"functionDeclarations": copy.deepcopy(antigravity_tools())}],
                "toolConfig": {"functionCallingConfig": {"mode": str(tool_mode or "AUTO").upper()}},
            }
            if system_instruction:
                payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
            if generation_config:
                payload["generationConfig"] = copy.deepcopy(dict(generation_config))

            try:
                response = self._request(payload)
            except AntigravityPlayerError as exc:
                result = _error(exc.code, str(exc))
                result.update({"contents": history, "rounds": round_index})
                if exc.status is not None:
                    result["error"]["status"] = exc.status
                return result

            prompt_feedback = response.get("promptFeedback")
            if isinstance(prompt_feedback, Mapping) and prompt_feedback.get("blockReason"):
                reason = str(prompt_feedback["blockReason"])
                result = _error("content_filtered", f"Prompt blocked by safety filter: {reason}")
                result.update({"contents": history, "rounds": round_index, "response": response})
                return result

            candidates = response.get("candidates")
            if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], Mapping):
                result = _error("invalid_response", "Antigravity/Gemini response has no candidates[0].")
                result.update({"contents": history, "rounds": round_index, "response": response})
                return result

            candidate = candidates[0]
            finish_reason = str(candidate.get("finishReason") or "")
            content_obj = candidate.get("content")
            if not isinstance(content_obj, Mapping):
                if finish_reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
                    result = _error("content_filtered", f"Model response stopped by filter ({finish_reason}).")
                    result.update({"contents": history, "rounds": round_index, "response": response})
                    return result
                result = _error("invalid_response", "Candidate has no content object.")
                result.update({"contents": history, "rounds": round_index, "response": response})
                return result

            raw_parts = content_obj.get("parts")
            if not isinstance(raw_parts, list) or not raw_parts:
                result = _error("invalid_response", "Candidate content has no parts.")
                result.update({"contents": history, "rounds": round_index, "response": response})
                return result

            # Preserve all model parts (including thought / thoughtSignature) verbatim.
            model_turn = {
                "role": str(content_obj.get("role") or "model"),
                "parts": copy.deepcopy(raw_parts),
            }
            history.append(model_turn)

            fn_calls = [part["functionCall"] for part in raw_parts if isinstance(part, Mapping) and "functionCall" in part]
            if not fn_calls:
                if finish_reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
                    result = _error("content_filtered", f"Model response stopped by filter ({finish_reason}).")
                    result.update({"contents": history, "rounds": round_index})
                    return result
                if finish_reason == "MAX_TOKENS":
                    result = _error("response_incomplete", "The model stopped because it reached MAX_TOKENS.")
                    result.update({"contents": history, "rounds": round_index})
                    return result
                text_chunks = [
                    str(part["text"])
                    for part in raw_parts
                    if isinstance(part, Mapping) and "text" in part and not part.get("thought")
                ]
                if not text_chunks:
                    result = _error("invalid_response", "Model turn has neither visible text nor functionCall parts.")
                    result.update({"contents": history, "rounds": round_index})
                    return result
                text = "".join(text_chunks)
                return {
                    "ok": True,
                    "content": text,
                    "message": model_turn,
                    "contents": history,
                    "rounds": round_index,
                    "usage": response.get("usageMetadata"),
                }

            response_parts = [self._execute_function_call(fn_call) for fn_call in fn_calls]
            history.append({"role": "user", "parts": response_parts})

            if round_index == self.max_rounds:
                result = _error(
                    "tool_loop_limit",
                    f"The model requested tools after the {self.max_rounds}-round limit.",
                    "Increase max_rounds only after inspecting the tool-call trace.",
                )
                result.update({"contents": history, "rounds": round_index})
                return result

        return {"ok": False, "error": {"code": "tool_loop_limit", "message": "The tool loop stopped."}, "contents": history}


def compact_observation(tool_name: str, outcome: Mapping[str, Any]) -> Dict[str, Any]:
    """Summarize verbose game responses for interactive agent context efficiency."""

    if not outcome.get("ok"):
        return dict(outcome)
    raw = copy.deepcopy(outcome["result"])
    if tool_name == "get_status" and isinstance(raw, dict):
        dupes = [
            {
                key: dupe.get(key)
                for key in ("id", "name", "x", "y", "chore", "health", "breath_pct", "kcal", "stress_pct", "morale", "temperature_c")
                if key in dupe
            }
            for dupe in raw.get("duplicants", [])
        ]
        return {
            "ok": True,
            "result": {
                "time": raw.get("time"),
                "duplicants": dupes,
                "colony": raw.get("colony"),
                "alerts": raw.get("alerts"),
                "research": raw.get("research"),
                "work": raw.get("work"),
                "printables_waiting": raw.get("printables_waiting"),
            },
        }
    if tool_name == "inspect" and isinstance(raw, dict):
        for building in raw.get("buildings", []):
            if isinstance(building, dict) and isinstance(building.get("recipes"), list):
                building["recipes"] = [item for item in building["recipes"] if isinstance(item, Mapping) and item.get("queued")]
        return {"ok": True, "result": raw}
    if tool_name == "get_research" and isinstance(raw, dict):
        return {"ok": True, "result": {k: v for k, v in raw.items() if k != "techs"}}
    if tool_name in ("wait", "get_events") and isinstance(raw, dict):
        counts: Dict[str, int] = {}
        visible: List[Any] = []
        for event in raw.get("events", []):
            if (
                isinstance(event, Mapping)
                and event.get("type") in ("building_completed", "building_removed")
                and event.get("def") in TRIVIAL_BUILD_DEFS
            ):
                label = f"{event['type']}:{event['def']}"
                counts[label] = counts.get(label, 0) + 1
            else:
                visible.append(event)
        summarized = dict(raw)
        summarized["events"] = visible
        if counts:
            summarized["construction_counts"] = counts
        return {"ok": True, "result": summarized}
    return {"ok": True, "result": raw}


class AntigravitySession:
    """Stateful direct bridge for an interactive Antigravity agent playing ONI."""

    def __init__(
        self,
        run_dir: pathlib.Path,
        *,
        tool_executor: Callable[[str, Optional[Mapping[str, Any]]], Dict[str, Any]] = call_oni_tool,
        max_wait_seconds: float = MAX_SESSION_WAIT_SECONDS,
    ) -> None:
        self.run_dir = pathlib.Path(run_dir)
        self.cursor_path = self.run_dir / "cursor.json"
        self.steps_path = self.run_dir / "steps.jsonl"
        self.tool_executor = tool_executor
        self.max_wait_seconds = max(0.0, min(float(max_wait_seconds), 60.0))

    def load_cursor(self) -> int:
        if not self.cursor_path.exists():
            return 0
        try:
            data = json.loads(self.cursor_path.read_text(encoding="utf-8"))
            value = int(data.get("since", 0))
            return max(0, value)
        except (OSError, ValueError, TypeError, AttributeError):
            return 0

    def save_cursor(self, next_since: int) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cursor_path.with_name("cursor.json.tmp")
        tmp.write_text(json.dumps({"since": int(next_since)}) + "\n", encoding="utf-8")
        os.replace(tmp, self.cursor_path)

    def call(self, tool_name: str, args: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        call_args = dict(args or {})
        if tool_name in ("get_events", "wait") and "since" not in call_args and self.cursor_path.exists():
            call_args["since"] = self.load_cursor()
        if tool_name == "wait":
            requested = call_args.get("seconds", DEFAULT_WAIT_SECONDS)
            if not isinstance(requested, bool) and isinstance(requested, (int, float)):
                call_args["seconds"] = min(float(requested), self.max_wait_seconds)

        if tool_name not in _tool_names():
            outcome = _error("unknown_tool", f"The ONI tool {tool_name!r} does not exist.")
        else:
            outcome = self.tool_executor(tool_name, call_args)

        self.run_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "tool": tool_name,
            "args": call_args,
            "out": outcome,
        }
        with self.steps_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        if tool_name in ("get_events", "wait") and outcome.get("ok"):
            next_since = outcome.get("result", {}).get("next_since")
            if isinstance(next_since, int) and not isinstance(next_since, bool):
                self.save_cursor(next_since)
        return outcome

    def run_batch(
        self,
        calls: Sequence[Sequence[Any]],
        *,
        compact: bool = True,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for entry in calls:
            if not isinstance(entry, (list, tuple)) or len(entry) not in (1, 2):
                results.append(
                    {
                        "tool": "<invalid>",
                        "outcome": _error("invalid_batch_entry", "Each batch item must be [tool] or [tool, args]."),
                    }
                )
                continue
            name = str(entry[0])
            raw_args = entry[1] if len(entry) == 2 else {}
            if not isinstance(raw_args, Mapping):
                results.append(
                    {
                        "tool": name,
                        "outcome": _error("invalid_tool_arguments", f"Arguments for {name} must be a JSON object."),
                    }
                )
                continue
            outcome = self.call(name, raw_args)
            results.append(
                {
                    "tool": name,
                    "outcome": compact_observation(name, outcome) if compact else outcome,
                }
            )
        return results

    def wait_and_check(self, seconds: float = DEFAULT_WAIT_SECONDS, *, compact: bool = True) -> List[Dict[str, Any]]:
        return self.run_batch(
            [
                ["wait", {"seconds": seconds}],
                ["get_status", {}],
            ],
            compact=compact,
        )


def mcp_config_payload(python_executable: str = "python") -> Dict[str, Any]:
    """Return the Antigravity ``mcp_config.json`` snippet for the ONI MCP server."""

    script_path = (REPO_ROOT / "tools" / "oni_tools.py").as_posix()
    return {
        "mcpServers": {
            "oni": {
                "command": python_executable,
                "args": [script_path, "mcp"],
            }
        }
    }


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    sys.stdin.reconfigure(encoding="utf-8-sig")
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="subcommand", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--run-dir",
        default=str(REPO_ROOT / "reports" / "play" / "current"),
        help="Session directory holding cursor.json and steps.jsonl.",
    )
    common.add_argument("--raw", action="store_true", help="Print uncompacted JSON tool outputs.")

    sub.add_parser("status", parents=[common], help="Read get_events (from cursor) and compact get_status.")

    wait_p = sub.add_parser("wait", parents=[common], help="Wait <= 30s from cursor, then read get_status.")
    wait_p.add_argument("seconds", nargs="?", type=float, default=DEFAULT_WAIT_SECONDS)

    call_p = sub.add_parser("call", parents=[common], help="Execute one ONI tool call with audit & cursor tracking.")
    call_p.add_argument("tool")
    call_p.add_argument("args_json", nargs="?", default="{}")

    step_p = sub.add_parser("step", parents=[common], help="Execute a JSON list of [tool, args] pairs in order.")
    step_p.add_argument("batch_json", help="JSON array of [tool, args] items, or '-' to read stdin.")

    mcp_p = sub.add_parser("mcp-config", help="Print the Antigravity mcp_config.json snippet for ONI.")
    mcp_p.add_argument("--python", default="python", help="Python command to place in mcpServers.oni.command.")

    run_p = sub.add_parser("run", help="Run the standalone Gemini / Antigravity generateContent tool loop.")
    run_p.add_argument("--message", required=True, help="Initial user prompt.")
    run_p.add_argument("--system", help="Optional system instruction.")
    run_p.add_argument("--system-file", help="UTF-8 file containing the system instruction.")
    run_p.add_argument("--model", help="Gemini/Antigravity model id (default: ANTIGRAVITY_MODEL / GEMINI_MODEL).")
    run_p.add_argument("--base-url", help="Endpoint base URL (default: generativelanguage.googleapis.com/v1beta).")
    run_p.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    run_p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)

    args = parser.parse_args(argv)

    if args.subcommand == "mcp-config":
        print(json.dumps(mcp_config_payload(args.python), ensure_ascii=False, indent=2))
        return 0

    if args.subcommand == "run":
        if args.system and args.system_file:
            parser.error("use either --system or --system-file, not both")
        system = args.system
        if args.system_file:
            try:
                system = pathlib.Path(args.system_file).read_text(encoding="utf-8")
            except OSError as exc:
                parser.error(f"cannot read --system-file: {exc}")
        player = AntigravityPlayer(
            model=args.model,
            base_url=args.base_url,
            max_rounds=args.max_rounds,
            timeout_seconds=args.timeout,
        )
        result = player.run([{"role": "user", "content": args.message}], system_instruction=system)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    session = AntigravitySession(pathlib.Path(args.run_dir))
    compact = not args.raw

    if args.subcommand == "status":
        items = session.run_batch([["get_events", {}], ["get_status", {}]], compact=compact)
        for item in items:
            print(item["tool"], json.dumps(item["outcome"], ensure_ascii=False))
        return 0 if all(item["outcome"].get("ok") for item in items) else 1

    if args.subcommand == "wait":
        items = session.wait_and_check(args.seconds, compact=compact)
        for item in items:
            print(item["tool"], json.dumps(item["outcome"], ensure_ascii=False))
        return 0 if all(item["outcome"].get("ok") for item in items) else 1

    if args.subcommand == "call":
        try:
            parsed_args = json.loads(args.args_json)
        except ValueError as exc:
            parser.error(f"invalid JSON in args_json: {exc}")
        if not isinstance(parsed_args, Mapping):
            parser.error("args_json must decode to a JSON object")
        outcome = session.call(args.tool, parsed_args)
        shown = compact_observation(args.tool, outcome) if compact else outcome
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return 0 if outcome.get("ok") else 1

    if args.subcommand == "step":
        raw_batch = sys.stdin.read() if args.batch_json == "-" else args.batch_json
        try:
            batch = json.loads(raw_batch)
        except ValueError as exc:
            parser.error(f"invalid JSON in batch_json: {exc}")
        if not isinstance(batch, list):
            parser.error("batch_json must decode to a JSON array")
        items = session.run_batch(batch, compact=compact)
        for item in items:
            print(item["tool"], json.dumps(item["outcome"], ensure_ascii=False))
        return 0 if all(item["outcome"].get("ok") for item in items) else 1

    return 2


if __name__ == "__main__":
    sys.exit(_cli())
