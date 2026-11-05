#!/usr/bin/env python3
"""A model plays ONI through the oni tools: ask, run its tool calls, repeat.

    python -m agent.player_loop --run-dir reports/play/<run> --cycles 10

This is the same player as Claude with the oni-player skill: the same tool
list, the same skill text, and no rules of its own.  The model has no file
access, so the knowledge files go into the system prompt and its notes are two
local tools.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))

import oni_tools  # noqa: E402

from agent import model_client  # noqa: E402

SKILL_DIR = REPO_ROOT / "skills" / "oni-player"
RESULT_LIMIT = 6000
HISTORY_LIMIT = 40
ACT_AFTER_LOOKS = 5
RETRY_SECONDS = (5.0, 15.0, 30.0, 60.0)
CONTINUE_PROMPT = "继续玩。用工具看现状，然后动手。"
ACT_PROMPT = "你已经连续查了 {count} 次，游戏一直在走。现在就下单，或者 wait 看结果，别再查了。"
READ_ONLY = ("get_", "inspect", "find", "list_", "read_notes")

NOTE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_notes",
            "description": "Your notes on this run. Read them when you lose track: older messages drop out of your history.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_notes",
            "description": "Replace your notes with this text: the current plan, the base layout with ids, to-dos and lessons.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
]

Ask = Callable[[List[Mapping[str, Any]], List[Mapping[str, Any]]], Dict[str, Any]]


def system_prompt() -> str:
    parts = [(SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")]
    for path in sorted((SKILL_DIR / "knowledge").glob("*.md")):
        parts.append(path.read_text(encoding="utf-8"))
    parts.append("你没有读写文件的能力：上面的知识已经全部给你，笔记用 read_notes 和 write_notes。")
    return "\n\n".join(parts)


def trimmed(messages: List[Dict[str, Any]], limit: int = HISTORY_LIMIT) -> List[Dict[str, Any]]:
    """The system prompt plus recent history, starting at a whole exchange."""
    if len(messages) <= limit + 1:
        return messages
    start = len(messages) - limit
    while start < len(messages) and messages[start]["role"] == "tool":
        start += 1  # A tool result must follow the call that asked for it.
    return [messages[0]] + messages[start:]


def run_tool(name: str, args: Mapping[str, Any], notes: pathlib.Path) -> str:
    if name == "read_notes":
        return notes.read_text(encoding="utf-8") if notes.exists() else "(no notes yet)"
    if name == "write_notes":
        notes.write_text(str(args.get("text", "")), encoding="utf-8")
        return "saved"
    outcome = oni_tools.call(name, args)
    text = json.dumps(outcome.get("result") if outcome.get("ok") else outcome.get("error"), ensure_ascii=False)
    if len(text) > RESULT_LIMIT:
        return text[:RESULT_LIMIT] + " ... (cut; ask for a smaller area)"
    return text


def tool_arguments(call: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(call["function"].get("arguments") or "{}")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def looks_since_last_act(messages: List[Mapping[str, Any]]) -> int:
    """How many read-only calls the model has made since it last did anything."""
    count = 0
    for message in reversed(messages):
        for call in reversed(message.get("tool_calls") or []):
            if not call["function"]["name"].startswith(READ_ONLY):
                return count
            count += 1
    return count


def play_turn(messages: List[Dict[str, Any]], ask: Ask, notes: pathlib.Path, log: Callable[[Mapping[str, Any]], None]) -> None:
    """One model reply and every tool call in it."""
    looks = looks_since_last_act(messages)
    if looks >= ACT_AFTER_LOOKS:
        messages.append({"role": "user", "content": ACT_PROMPT.format(count=looks)})
    tools = oni_tools.openai_tools() + NOTE_TOOLS
    reply = ask(trimmed(messages), tools)
    calls = reply.get("tool_calls") or []
    assistant: Dict[str, Any] = {"role": "assistant", "content": reply.get("content") or ""}
    if calls:
        assistant["tool_calls"] = calls
    messages.append(assistant)
    log({"assistant": assistant})
    if not calls:
        messages.append({"role": "user", "content": CONTINUE_PROMPT})
        return
    for call in calls:
        args = tool_arguments(call)
        if args is None:
            result = "The arguments were not a JSON object."
        else:
            result = run_tool(call["function"]["name"], args, notes)
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        log({"tool": call["function"]["name"], "args": args, "result": result[:2000]})


def chat_asker(config: Mapping[str, Any]) -> Ask:
    def ask(messages: List[Mapping[str, Any]], tools: List[Mapping[str, Any]]) -> Dict[str, Any]:
        base_url = str(config.get("base_url") or model_client.DEFAULT_BASE_URL).rstrip("/")
        request = urllib.request.Request(
            base_url + "/chat/completions",
            data=json.dumps(
                {"model": config.get("model") or model_client.DEFAULT_MODEL, "messages": messages, "tools": tools},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + str(config["api_key"])},
            method="POST",
        )
        timeout = model_client.effective_request_timeout(config)
        response = model_client.normalize_model_response(model_client.execute_request(request, timeout))
        failure = model_client.response_error_code(response)
        if failure:
            raise model_client.ModelRequestError(failure)
        return response["choices"][0]["message"]

    return ask


def patient(ask: Ask, sleep: Callable[[float], None] = time.sleep) -> Ask:
    """Retry a failed request; the game runs on meanwhile, so a pause is safe."""

    def retrying(messages: List[Mapping[str, Any]], tools: List[Mapping[str, Any]]) -> Dict[str, Any]:
        for delay in RETRY_SECONDS:
            try:
                return ask(messages, tools)
            except (model_client.ModelRequestError, OSError, KeyError, IndexError, ValueError) as exc:
                print(f"model request failed ({exc}); retrying in {delay:.0f} s", file=sys.stderr)
                sleep(delay)
        return ask(messages, tools)

    return retrying


def current_cycle() -> Optional[int]:
    outcome = oni_tools.call("get_progress", {})
    return outcome["result"]["now"]["cycle"] if outcome.get("ok") else None


def play(run_dir: pathlib.Path, cycles: int, max_turns: int, ask: Ask) -> Dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    notes = run_dir / "notes.md"
    start = current_cycle()
    if start is None:
        raise SystemExit("No running game API was found. Start one with scripts/api_session.py start.")
    goal = f"玩到第 {start + cycles} 周期，不减员。现在是第 {start} 周期。先读笔记。"
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": goal},
    ]
    with (run_dir / "transcript.jsonl").open("a", encoding="utf-8") as transcript:

        def log(entry: Mapping[str, Any]) -> None:
            transcript.write(json.dumps({"time": time.time(), **entry}, ensure_ascii=False) + "\n")
            transcript.flush()

        for turn in range(max_turns):
            play_turn(messages, ask, notes, log)
            cycle = current_cycle()
            if cycle is None or cycle >= start + cycles:
                return {"turns": turn + 1, "start_cycle": start, "end_cycle": cycle}
    return {"turns": max_turns, "start_cycle": start, "end_cycle": current_cycle()}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--max-turns", type=int, default=2000)
    parser.add_argument("--secrets-dir")
    args = parser.parse_args(argv)
    config = model_client.load_llm_config(pathlib.Path(args.secrets_dir) if args.secrets_dir else None)
    problem = (
        config.get("config_error")
        or ("no_api_key" if not config.get("api_key") else None)
        or model_client.base_url_error(str(config.get("base_url") or model_client.DEFAULT_BASE_URL))
    )
    if problem:
        print(f"The model endpoint cannot be used: {problem}", file=sys.stderr)
        return 1
    outcome = play(pathlib.Path(args.run_dir), args.cycles, args.max_turns, patient(chat_asker(config)))
    print(json.dumps(outcome, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
