#!/usr/bin/env python3
"""Verify the explicit single-owner planned queue seam in Bridge outputs."""
from __future__ import annotations

import copy
import argparse
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.advanced_actions import build_duplicant_priority_hint
from agent.colony_plan import (
    load_active_plan,
)
from agent.command_queue import ensure_command_dirs
from bridge import oni_bridge
from scripts.test_controller_loop import snapshot
from scripts.verify_colony_plan import only_command, write_live_state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    report: dict[str, object] = {
        "schema": "oni.planned-queue-owner-seam-verification.v1",
        "status": "failed",
        "live_admission": False,
        "game_launched": False,
        "scope": "temporary Bridge directory; explicit callback only; no real runtime",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="oni-planned-owner-seam-") as temp:
            bridge = pathlib.Path(temp) / "bridge"
            bridge.mkdir()
            ensure_command_dirs(bridge)
            (bridge / "validation-controls" / "approved").mkdir(parents=True)
            (bridge / "validation-controls" / "inflight").mkdir(parents=True)
            initial = snapshot(priority=3, seq=2, snapshot_seq=1)
            write_live_state(bridge, initial, suggestions_enabled=True)
            baseline_frame = json.loads((bridge / "decision-frame.json").read_text(encoding="utf-8-sig"))
            state = json.loads((bridge / "latest_state.json").read_text(encoding="utf-8-sig"))
            advice = [{
                "incident_id": "incident-planned",
                "reason": "explicit planned owner fixture",
                "command": build_duplicant_priority_hint(
                    state, instance_id=123, chore_group="Build", priority=4
                ),
            }]
            runtime = oni_bridge.ReplayState()
            runtime.process({
                "schema": "oni.event.v2",
                "event": "session_started",
                "session_id": state["session_id"],
                "seq": 1,
                "snapshot_seq": 0,
                "reason": "save_loader_load_complete",
                "ts": state["ts"],
            })
            runtime.process(state)
            # A real named clone on this machine: the launch guard refuses an
            # autosave clone (review P0-1, a659015), so the 2026-09-13 autosave
            # fixture no longer passes; m1-0927c is a non-autosave clone in its
            # own colony folder with the manifest that created it.
            clone_save = pathlib.Path(r"C:\Users\user\Documents\Klei\OxygenNotIncluded\cloud_save_files\76561190000000000\oni-m1-0927c\oni-m1-0927c.sav")
            clone_preview = clone_save.with_suffix(".png")
            clone_manifest = ROOT / "reports" / "m1" / "c" / "m1-0927c" / "test-clone-manifest.json"
            for label, path in (("clone save", clone_save), ("clone preview", clone_preview), ("clone manifest", clone_manifest)):
                if not path.is_file():
                    raise RuntimeError(f"seam fixture {label} is missing on this machine: {path}")
            owner_config = pathlib.Path(temp) / "planned-owner.json"
            owner_config.write_text(json.dumps({
                "schema": oni_bridge.PLANNED_OWNER_CONFIG_SCHEMA,
                "bridge_dir": str(bridge),
                "clone_manifest": str(clone_manifest),
                "clone_save": str(clone_save),
                "clone_preview": str(clone_preview),
            }), encoding="utf-8")
            owner = oni_bridge.build_planned_queue_owner_from_config(bridge, owner_config)

            with mock.patch.object(oni_bridge, "build_decision_frame", return_value=copy.deepcopy(baseline_frame)), \
                    mock.patch.object(oni_bridge.agent_rules, "build_arbitrated_advice", return_value=copy.deepcopy(advice)), \
                    mock.patch.object(oni_bridge, "sync_pending_commands") as sync:
                oni_bridge.write_outputs(bridge, runtime, planned_queue_owner=owner)
                sync.assert_not_called()

            suggested_path = bridge / "commands" / "pending" / "fixture-suggested.json"
            suggested_path.write_text(json.dumps({"id": "fixture-suggested", "mode": "suggested"}), encoding="utf-8")
            try:
                owner(bridge, copy.deepcopy(json.loads((bridge / "plan.json").read_text(encoding="utf-8-sig"))),
                      copy.deepcopy(advice), copy.deepcopy(baseline_frame))
            except RuntimeError as exc:
                suggested_blocked = "suggested" in str(exc)
            else:
                suggested_blocked = False
            suggested_path.unlink()
            if not suggested_blocked:
                raise RuntimeError("pre-existing suggested command was not fail-closed")

            first_active = load_active_plan(bridge)
            first_plan_id = first_active.get("plan_id")
            with mock.patch.object(oni_bridge, "build_decision_frame", return_value=copy.deepcopy(baseline_frame)), \
                    mock.patch.object(oni_bridge.agent_rules, "build_arbitrated_advice", return_value=copy.deepcopy(advice)), \
                    mock.patch.object(oni_bridge, "sync_pending_commands") as sync:
                second_result = owner(
                    bridge,
                    copy.deepcopy(json.loads((bridge / "plan.json").read_text(encoding="utf-8-sig"))),
                    copy.deepcopy(advice),
                    copy.deepcopy(baseline_frame),
                )
                sync.assert_not_called()

            pending_commands = [json.loads(path.read_text(encoding="utf-8-sig"))
                                for path in (bridge / "commands" / "pending").glob("*.json")]
            planned = [item for item in pending_commands if item.get("mode") == "planned"]
            suggested = [item for item in pending_commands if item.get("mode") == "suggested"]
            if len(planned) != 1 or suggested:
                raise RuntimeError(f"unexpected queue modes planned={len(planned)} suggested={len(suggested)}")
            command = planned[0]
            active = load_active_plan(bridge)
            source = command.get("source") if isinstance(command.get("source"), dict) else {}
            report.update({
                "status": "passed",
                "owner_calls": 1,
                "idempotent_second_result": second_result,
                "plan_id_stable": first_plan_id == active.get("plan_id"),
                "preexisting_suggested_blocked": suggested_blocked,
                "planned_command": {
                    "mode": command.get("mode"),
                    "plan_id": source.get("plan_id"),
                    "step_id": source.get("step_id"),
                    "source_frame_id": command.get("source_frame_id"),
                },
                "pending_count": len(pending_commands),
                "suggested_count": len(suggested),
                "active_plan_status": active.get("status"),
                "active_step_status": active["steps"][0].get("status"),
                "successor_count": len(active.get("steps") or []),
            })
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
