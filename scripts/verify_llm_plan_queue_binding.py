#!/usr/bin/env python3
"""Verify the isolated LLM-ranked-plan to one planned queue leaf contract.

This verifier deliberately uses a temporary Bridge directory.  It exercises
the existing projection and approval-gated controller APIs without wiring the
production watch loop to a second queue owner.
"""
from __future__ import annotations

import copy
import argparse
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.advanced_actions import build_duplicant_priority_hint
from agent.colony_plan import create_colony_plan, load_active_plan
from agent.command_queue import ensure_command_dirs
from agent.controller_bridge import ColonyPlanControllerAdapter, IDENTITY_FIELDS
from agent.controller_loop import ControllerCore, DurableIntentStore, EffectStatus
from agent.llm_planner import colony_plan_definition_from_plan
from agent.state_quality import canonical_state_hash
from scripts.test_controller_loop import snapshot
from scripts.verify_colony_plan import only_command, write_live_state


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    started = datetime.now(timezone.utc).isoformat()
    report: dict[str, object] = {
        "schema": "oni.llm-plan-queue-binding-verification.v1",
        "status": "failed",
        "live_admission": False,
        "game_launched": False,
        "production_watch_wired": False,
        "scope": "isolated temporary Bridge directory; no external model or runtime",
        "started_at": started,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="oni-llm-plan-queue-") as temp:
            bridge = pathlib.Path(temp) / "bridge"
            bridge.mkdir()
            ensure_command_dirs(bridge)
            initial = snapshot()
            write_live_state(bridge, initial, suggestions_enabled=True)
            state = json.loads((bridge / "latest_state.json").read_text(encoding="utf-8-sig"))

            working = copy.deepcopy(state)
            advice: list[dict[str, object]] = []
            for incident_id, priority in (("incident-a", 5), ("incident-b", 4)):
                command = build_duplicant_priority_hint(
                    working, instance_id=123, chore_group="Build", priority=priority
                )
                advice.append({
                    "incident_id": incident_id,
                    "reason": f"ranked fixture {incident_id}",
                    "command": command,
                })

            state_hash = canonical_state_hash(state)
            planner = {
                "schema": "oni.plan.v2",
                "mode": "llm_ranked",
                "goal": "verify one planned queue owner",
                "prompt_hash": "sha256:" + "a" * 64,
                "incident_fingerprint": "sha256:" + "b" * 64,
                "source": {
                    "session_id": state["session_id"],
                    "save_id_hash": state["save_id_hash"],
                    "world_id": state["world_id"],
                    "snapshot_id": state["snapshot_id"],
                    "state_hash": state_hash,
                },
                # Deliberately reverse the local advice order.
                "plan": [{"incident_id": "incident-b"}, {"incident_id": "incident-a"}],
                "llm": {
                    "status": "ok",
                    "model": "cpa-oni/synthetic",
                    "response_hash": "sha256:" + "c" * 64,
                },
            }
            definition = colony_plan_definition_from_plan(planner, advice)
            plan = create_colony_plan(bridge, definition)
            _require(
                [item["reason"] for item in plan["steps"]]
                == ["ranked fixture incident-b", "ranked fixture incident-a"],
                "LLM ranking order was not retained in the durable plan",
            )
            _require(plan["decision_provenance"]["mode"] == "llm_ranked", "plan provenance mode missing")

            identity = {key: state[key] for key in IDENTITY_FIELDS}
            adapter = ColonyPlanControllerAdapter(
                bridge,
                plan_id=plan["plan_id"],
                step_id=plan["steps"][0]["id"],
                expected_identity=identity,
                allow_pending_commands=True,
            )
            intents = pathlib.Path(temp) / "intents"
            intents.mkdir()
            cycle = ControllerCore(
                adapter,
                adapter.policy(),
                intent_store=DurableIntentStore(intents),
            ).step()
            _require(cycle.effect is not None and cycle.effect.status is EffectStatus.PENDING,
                     "first planned leaf did not reach pending approval")
            _path, command = only_command(bridge, "pending")
            _require(command.get("mode") == "planned", "pending command is not planned mode")
            command_source = command.get("source") if isinstance(command.get("source"), dict) else {}
            command_evidence = command.get("evidence") if isinstance(command.get("evidence"), dict) else {}
            _require(command_source.get("plan_id") == plan["plan_id"], "pending command plan_id drifted")
            _require(command_source.get("step_id") == plan["steps"][0]["id"], "pending command step_id drifted")
            _require(command_evidence.get("plan_id") == plan["plan_id"], "pending evidence plan_id drifted")
            _require(command_evidence.get("step_id") == plan["steps"][0]["id"], "pending evidence step_id drifted")
            _require(isinstance(command.get("source_frame_id"), str), "pending command source frame missing")
            frame = json.loads((bridge / "decision-frame.json").read_text(encoding="utf-8-sig"))
            _require(command.get("source_frame_id") == frame.get("frame_id"), "pending source frame drifted")
            _require(not list((bridge / "commands" / "approved").glob("*.json")),
                     "command reached approved without approval")
            _require(not list((bridge / "commands" / "suggested").glob("*.json")),
                     "suggested queue owner was created")
            current = load_active_plan(bridge)
            _require(current["steps"][0]["status"] == "awaiting_approval", "first leaf status not durable")
            _require(current["steps"][1]["status"] == "waiting", "successor advanced unexpectedly")
            report.update({
                "status": "passed",
                "plan_id": plan["plan_id"],
                "step_order": [item["id"] for item in plan["steps"]],
                "pending_command": {
                    "mode": command.get("mode"),
                    "plan_id": command_source.get("plan_id"),
                    "step_id": command_source.get("step_id"),
                    "source_frame_id": command.get("source_frame_id"),
                },
                "pending_count": len(list((bridge / "commands" / "pending").glob("*.json"))),
                "suggested_count": len(list((bridge / "commands" / "suggested").glob("*.json"))),
                "successor_status": current["steps"][1]["status"],
            })
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    if args.report is not None:
        target = args.report.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
