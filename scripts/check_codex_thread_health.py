#!/usr/bin/env python3
"""Read-only, redacted health probe for a local Codex task rollout."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def age_seconds(now: datetime, value: datetime | None) -> float | None:
    return round(max(0.0, (now - value).total_seconds()), 1) if value else None


def locate_rollout(thread_id: str, codex_home: Path) -> Path:
    candidates: list[Path] = []
    for root in (codex_home / "sessions", codex_home / "archived_sessions"):
        if root.exists():
            candidates.extend(root.rglob(f"*{thread_id}.jsonl"))
    if not candidates:
        raise FileNotFoundError(f"No local rollout found for thread {thread_id}")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def scan_rollout(path: Path) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    parse_errors = 0
    line_count = 0
    compactions = 0
    last_event: datetime | None = None
    last_progress: datetime | None = None
    last_commentary: datetime | None = None
    last_reasoning: datetime | None = None
    last_tool: datetime | None = None
    last_error: datetime | None = None
    last_task_started: datetime | None = None
    last_terminal: datetime | None = None
    first_progress_after_start: datetime | None = None
    turn_context: dict[str, Any] = {}
    token_info: dict[str, Any] = {}
    goal: dict[str, Any] = {}

    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line_count += 1
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue

            timestamp = parse_timestamp(record.get("timestamp"))
            if timestamp and (last_event is None or timestamp > last_event):
                last_event = timestamp

            outer_type = str(record.get("type") or "")
            payload = record.get("payload") or {}
            payload_type = str(payload.get("type") or "")
            event_name = payload_type if outer_type == "event_msg" else outer_type
            counts[event_name] += 1

            if outer_type == "turn_context":
                turn_context = payload
            if outer_type == "compacted" or "compact" in payload_type.lower():
                compactions += 1

            if outer_type == "event_msg":
                if payload_type == "task_started":
                    last_task_started = timestamp
                    first_progress_after_start = None
                elif payload_type in {"task_complete", "turn_aborted", "task_failed"}:
                    last_terminal = timestamp
                elif payload_type == "token_count":
                    token_info = payload.get("info") or {}
                elif payload_type == "thread_goal_updated":
                    goal = payload.get("goal") or {}
                if any(word in payload_type.lower() for word in ("error", "retry", "fail")):
                    last_error = timestamp

            if outer_type != "response_item":
                continue

            is_progress = False
            if payload_type in {
                "reasoning",
                "custom_tool_call",
                "custom_tool_call_output",
                "function_call",
                "function_call_output",
            }:
                is_progress = True
                if payload_type == "reasoning":
                    last_reasoning = timestamp
                else:
                    last_tool = timestamp
            elif payload_type in {"message", "agent_message"}:
                role = payload.get("role")
                phase = payload.get("phase")
                if role == "assistant" or payload_type == "agent_message":
                    is_progress = True
                    if phase == "commentary":
                        last_commentary = timestamp

            if is_progress and timestamp:
                last_progress = timestamp
                if (
                    last_task_started
                    and timestamp >= last_task_started
                    and first_progress_after_start is None
                ):
                    first_progress_after_start = timestamp

    active_turn = bool(
        last_task_started and (last_terminal is None or last_task_started > last_terminal)
    )
    last_usage = token_info.get("last_token_usage") or {}
    context_window = token_info.get("model_context_window")
    input_tokens = last_usage.get("input_tokens")
    context_ratio = None
    if isinstance(input_tokens, int) and isinstance(context_window, int) and context_window:
        context_ratio = round(input_tokens / context_window, 4)

    return {
        "line_count": line_count,
        "parse_errors": parse_errors,
        "compaction_count": compactions,
        "event_counts": dict(counts),
        "last_event": last_event,
        "last_progress": last_progress,
        "last_commentary": last_commentary,
        "last_reasoning": last_reasoning,
        "last_tool": last_tool,
        "last_error": last_error,
        "last_task_started": last_task_started,
        "last_terminal": last_terminal,
        "first_progress_after_start": first_progress_after_start,
        "active_turn": active_turn,
        "model": turn_context.get("model"),
        "effort": turn_context.get("effort"),
        "turn_id": turn_context.get("turn_id"),
        "last_input_tokens": input_tokens,
        "model_context_window": context_window,
        "context_ratio": context_ratio,
        "goal_status": goal.get("status"),
        "goal_tokens_used": goal.get("tokensUsed"),
        "goal_time_used_seconds": goal.get("timeUsedSeconds"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument(
        "--codex-home", type=Path, default=Path.home() / ".codex"
    )
    parser.add_argument("--sample-seconds", type=float, default=0.0)
    parser.add_argument("--active-seconds", type=float, default=90.0)
    parser.add_argument("--stalled-seconds", type=float, default=180.0)
    args = parser.parse_args()

    if not THREAD_ID_RE.fullmatch(args.thread_id):
        parser.error("--thread-id must be a UUID-shaped Codex thread id")
    if not 0.0 <= args.sample_seconds <= 30.0:
        parser.error("--sample-seconds must be between 0 and 30")
    if not 0 < args.active_seconds < args.stalled_seconds:
        parser.error("thresholds must satisfy 0 < active < stalled")

    try:
        rollout = locate_rollout(args.thread_id, args.codex_home)
    except FileNotFoundError as exc:
        print(json.dumps({"thread_id": args.thread_id, "error": str(exc)}, indent=2))
        return 3

    before = rollout.stat()
    scan = scan_rollout(rollout)
    if args.sample_seconds:
        time.sleep(args.sample_seconds)
    after = rollout.stat()

    now = datetime.now(timezone.utc)
    last_activity = scan["last_progress"] or scan["last_event"]
    activity_age = age_seconds(now, last_activity)
    commentary_age = age_seconds(now, scan["last_commentary"])
    grew = after.st_size > before.st_size or after.st_mtime_ns > before.st_mtime_ns

    if not scan["active_turn"]:
        classification = "idle_or_terminal"
    elif grew or (activity_age is not None and activity_age <= args.active_seconds):
        classification = "active"
    elif activity_age is not None and activity_age <= args.stalled_seconds:
        classification = "quiet_reasoning_or_transport"
    elif scan["first_progress_after_start"] is None:
        classification = "startup_stall_candidate"
    else:
        classification = "runtime_stall_candidate"

    visibility_desync_risk = bool(
        classification == "active"
        and commentary_age is not None
        and commentary_age > 60
    )
    context_ratio = scan["context_ratio"]
    context_band = "unknown"
    if isinstance(context_ratio, float):
        context_band = "red" if context_ratio >= 0.60 else "yellow" if context_ratio >= 0.40 else "green"

    result = {
        "thread_id": args.thread_id,
        "classification": classification,
        "visibility_desync_risk": visibility_desync_risk,
        "recommendation": (
            "Do not interrupt or replay actions; execution activity is present."
            if classification == "active"
            else "Take another read-only snapshot before any retry."
            if classification == "quiet_reasoning_or_transport"
            else "Verify live process, queue, and postconditions before one compact retry."
            if classification.endswith("stall_candidate")
            else "No active turn detected."
        ),
        "rollout_path": str(rollout.resolve()),
        "file_size_bytes": after.st_size,
        "grew_during_sample": grew,
        "line_count": scan["line_count"],
        "parse_errors": scan["parse_errors"],
        "last_event_at": iso(scan["last_event"]),
        "last_progress_at": iso(scan["last_progress"]),
        "seconds_since_activity": activity_age,
        "last_commentary_at": iso(scan["last_commentary"]),
        "seconds_since_commentary": commentary_age,
        "last_error_signal_at": iso(scan["last_error"]),
        "active_turn": scan["active_turn"],
        "turn_id": scan["turn_id"],
        "model": scan["model"],
        "effort": scan["effort"],
        "last_input_tokens": scan["last_input_tokens"],
        "model_context_window": scan["model_context_window"],
        "context_ratio": context_ratio,
        "context_band": context_band,
        "compaction_count": scan["compaction_count"],
        "goal_status": scan["goal_status"],
        "goal_tokens_used": scan["goal_tokens_used"],
        "goal_time_used_seconds": scan["goal_time_used_seconds"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if classification.endswith("stall_candidate") else 0


if __name__ == "__main__":
    raise SystemExit(main())
