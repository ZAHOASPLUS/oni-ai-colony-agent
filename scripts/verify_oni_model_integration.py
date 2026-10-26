"""Live smoke test: the real ONI model loop driven by the cline-pass DeepSeek model.

This is an acceptance check for the model integration, not a live-game action:
it builds the same bridge fixture the regression suite uses (no game process, no
save mutation), lets the real model loop submit one real choice request, and
reports the decision the model produced.

Usage:
    python -B scripts/verify_oni_model_integration.py [--secrets-dir DIR] [--timeout 90]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.llm_planner import config_health, resolve_llm_config  # noqa: E402
from agent.model_loop import ModelLoop  # noqa: E402
from bridge import oni_bridge  # noqa: E402
from scripts import test_model_loop as model_fixtures  # noqa: E402
from scripts import verify_bridge_reliability as fixtures  # noqa: E402

SESSION = "session-model-integration"
REPORT_PATH = ROOT / "reports" / "model-integration" / "live-integration-smoke.json"


def build_bridge(bridge: pathlib.Path) -> tuple[dict, dict]:
    """Write a real bridge fixture: policy, startup binding, three snapshots.

    The snapshots carry the same field-quality contracts the regression suite
    uses, because the controller refuses to plan from untrusted state.
    """

    expected = fixtures.startup_expectations()
    binding = dict(
        active_path_id=expected["loader_path_id_upper"],
        save_id_hash=expected["loader_save_sha256"],
    )
    policy = {
        "schema": "oni.action-policy.v1",
        "actions_enabled": True,
        "suggestions_enabled": True,
        "allowed_actions": [
            "noop",
            "focus_note",
            "set_paused",
            "set_research",
            "set_duplicant_chore_priority",
            "mark_dig_cells",
            "place_building_blueprint",
        ],
        "allowed_save_hashes": [expected["loader_save_sha256"]],
        "command_ttl_seconds": 120,
        "max_pending_commands": 20,
    }
    (bridge / "action-policy.json").write_text(json.dumps(policy), encoding="utf-8")
    records = [
        fixtures.startup_event(
            expected, SESSION, 1, "session_started",
            reason="save_loader_load_complete", **binding,
        )
    ]
    for index in range(3):
        snapshot = fixtures.startup_snapshot(
            expected, SESSION, index + 2, index + 1, paused=True, **binding
        )
        model_fixtures.enrich(snapshot)
        snapshot["ts"] = fixtures.iso_now()
        records.append(snapshot)
    with (bridge / "state.jsonl").open("wb") as stream:
        for record in records:
            stream.write(fixtures.json_line(record) + b"\n")
    runtime, _ = oni_bridge.replay_initial_records(
        bridge / "state.jsonl", bridge,
        startup_expectations=expected, require_startup_binding=True,
    )
    return runtime, expected


def refresh_snapshot(bridge: pathlib.Path, expected: dict, runtime: object, *, sequence: int) -> None:
    """Append one fresh trusted snapshot, as a live Probe would keep doing."""

    binding = dict(
        active_path_id=expected["loader_path_id_upper"],
        save_id_hash=expected["loader_save_sha256"],
    )
    snapshot = fixtures.startup_snapshot(
        expected, SESSION, sequence, sequence - 1, paused=True, **binding
    )
    model_fixtures.enrich(snapshot)
    snapshot["ts"] = fixtures.iso_now()
    with (bridge / "state.jsonl").open("ab") as stream:
        stream.write(fixtures.json_line(snapshot) + b"\n")
    runtime.process(snapshot)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secrets-dir", type=pathlib.Path, default=None)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--deadline", type=float, default=75.0)
    args = parser.parse_args()

    config = resolve_llm_config(args.secrets_dir)
    health = config_health(config)
    report: dict = {
        "schema": "oni.model-integration-smoke.v1",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config": health,
        "config_source": config.get("config_source"),
    }
    if config.get("config_error") or not config.get("api_key"):
        report["status"] = "config_unavailable"
        report["detail"] = str(config.get("config_error") or "missing_api_key")
        write_report(report)
        return 2

    with tempfile.TemporaryDirectory(prefix="oni-model-integration-") as temp:
        bridge = pathlib.Path(temp) / "bridge"
        bridge.mkdir()
        runtime, expected = build_bridge(bridge)
        loop = ModelLoop(
            secrets_dir=args.secrets_dir,
            config_override=config,
            min_interval_seconds=0,
            deadline_seconds=args.deadline,
        )
        oni_bridge.write_outputs(bridge, runtime, model_loop=loop)
        first = read_json(bridge / "model-loop.json")
        report["phase_after_first_tick"] = first.get("phase")
        report["candidate_ids"] = [item["candidate_id"] for item in first.get("candidates", [])]

        submitted = first.get("phase") == "awaiting_model"
        report["request_submitted"] = submitted
        if not submitted:
            report["status"] = "request_not_submitted"
            report["blockers"] = first.get("blockers")
            write_report(report)
            return 3

        started = time.monotonic()
        answered = loop.join_request(args.timeout)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["join_request"] = answered

        # A real Probe keeps publishing; refresh the fixture snapshot so the
        # model's validated choice is published in the same run instead of
        # waiting on a snapshot that the freshness gate now considers stale.
        refresh_snapshot(bridge, expected, runtime, sequence=5)
        oni_bridge.write_outputs(bridge, runtime, model_loop=loop)
        second = read_json(bridge / "model-loop.json")
        report["phase_after_answer"] = second.get("phase")

        choice_event = last_event(second, "model_choice")
        failure_event = last_event(second, "request_failed")
        pending = sorted((bridge / "commands" / "pending").glob("*.json"))
        report["pending_commands"] = [path.name for path in pending]
        report["last_event"] = second.get("last_event")
        report["last_choice"] = second.get("last_choice")
        if pending:
            command = read_json(pending[0])
            report["command"] = {
                "id": command.get("id"),
                "mode": command.get("mode"),
                "action": command.get("action"),
                "args": command.get("args"),
                "status": command.get("status"),
                "reason": command.get("reason"),
                "source": command.get("source"),
            }
            source = command.get("source") if isinstance(command.get("source"), dict) else {}
            report["chosen_candidate_id"] = source.get("candidate_id")
            report["model_reason"] = command.get("reason")
        if choice_event:
            report["status"] = "ok"
            report["chosen_candidate_id"] = choice_event.get("candidate_id") or report.get("chosen_candidate_id")
            report["model_reason"] = choice_event.get("reason") or report.get("model_reason")
            report["usage"] = choice_event.get("usage")
            report["cost_estimate_usd"] = choice_event.get("cost_estimate_usd")
            report["structured_output"] = choice_event.get("structured_output")
            report["elapsed_seconds_reported"] = choice_event.get("elapsed_seconds")
            report["response_hash"] = choice_event.get("response_hash")
        elif pending:
            # The command is the durable proof that the model answered and the
            # loop validated its choice; the event feed may have been trimmed.
            report["status"] = "ok"
            report["usage"] = None
        else:
            report["status"] = "model_failed"
            if failure_event:
                report["failure"] = {
                    key: failure_event.get(key)
                    for key in ("status", "request_id", "retry_after_seconds")
                }
        report["blockers"] = second.get("blockers")

    write_report(report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("status") == "ok" else 4


def read_json(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_events(status: dict) -> list[dict]:
    """Read the loop's published event history from one status document."""

    events = status.get("events") if isinstance(status, dict) else None
    if isinstance(events, list):
        return [event for event in events if isinstance(event, dict)]
    last = status.get("last_event") if isinstance(status, dict) else None
    return [last] if isinstance(last, dict) else []


def last_event(status: dict, kind: str) -> dict:
    for event in reversed(read_events(status)):
        if event.get("kind") == kind or event.get("event") == kind:
            return event
    return {}


def write_report(report: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
