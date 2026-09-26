"""Regression tests for the explicit Bridge planned-queue owner seam."""

from __future__ import annotations

import copy
import json
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.command_queue import ensure_command_dirs
from agent.advanced_actions import build_duplicant_priority_hint
from bridge import oni_bridge
from scripts.test_controller_loop import snapshot
from scripts.verify_colony_plan import write_live_state


class PlannedQueueOwnerTests(unittest.TestCase):
    def test_inventory_covers_all_protected_command_and_validation_queues(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-owner-queues-") as directory:
            root = pathlib.Path(directory)
            ensure_command_dirs(root)
            for queue_name in oni_bridge._PLANNED_OWNER_QUEUE_NAMES:
                queue = root / queue_name
                queue.mkdir(parents=True, exist_ok=True)
                path = queue / f"fixture-{queue_name.replace('/', '-')}.json"
                path.write_text(json.dumps({"id": path.stem, "mode": "planned"}), encoding="utf-8")

            inventory = oni_bridge._planned_owner_pending_inventory(root)
            self.assertEqual(len(inventory), len(oni_bridge._PLANNED_OWNER_QUEUE_NAMES))
            self.assertTrue(all(mode == "planned" for mode in inventory.values()))

    def test_owner_rejects_nonempty_protected_queue_before_plan_creation(self) -> None:
        for queue_name in oni_bridge._PLANNED_OWNER_QUEUE_NAMES:
            with self.subTest(queue=queue_name), tempfile.TemporaryDirectory(prefix="oni-owner-block-") as directory:
                root = pathlib.Path(directory)
                ensure_command_dirs(root)
                queue = root / queue_name
                queue.mkdir(parents=True, exist_ok=True)
                (queue / "fixture.json").write_text(
                    json.dumps({"id": "fixture", "mode": "planned"}), encoding="utf-8"
                )
                owner = oni_bridge.build_planned_queue_owner()
                with self.assertRaisesRegex(RuntimeError, "protected queue|protected queue entry"):
                    owner(root, {}, [], {"frame_id": "sha256:" + "a" * 64})
                self.assertFalse((root / "colony-plans" / "active.json").exists())

    def test_post_sync_failure_aborts_plan_and_quarantines_new_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-owner-cleanup-") as directory:
            root = pathlib.Path(directory)
            ensure_command_dirs(root)
            (root / "validation-controls" / "approved").mkdir(parents=True)
            (root / "validation-controls" / "inflight").mkdir(parents=True)
            state = snapshot(priority=3, seq=2, snapshot_seq=1)
            write_live_state(root, state, suggestions_enabled=True)
            baseline_frame = json.loads((root / "decision-frame.json").read_text(encoding="utf-8-sig"))
            runtime = oni_bridge.ReplayState()
            runtime.process(
                {
                    "schema": "oni.event.v2",
                    "event": "session_started",
                    "session_id": state["session_id"],
                    "seq": 1,
                    "snapshot_seq": 0,
                    "reason": "save_loader_load_complete",
                    "ts": state["ts"],
                }
            )
            current_state = json.loads((root / "latest_state.json").read_text(encoding="utf-8-sig"))
            runtime.process(current_state)
            advice = [{
                "incident_id": "incident-planned",
                "reason": "cleanup fixture",
                "command": build_duplicant_priority_hint(
                    current_state, instance_id=123, chore_group="Build", priority=4
                ),
            }]
            generated_plan = {
                "schema": "oni.plan.v2",
                "mode": "rule_only",
                "prompt_hash": "sha256:" + "b" * 64,
                "incident_fingerprint": "sha256:" + "c" * 64,
                "source": {
                    "session_id": current_state["session_id"],
                    "save_id_hash": current_state["save_id_hash"],
                    "world_id": current_state["world_id"],
                    "snapshot_id": current_state["snapshot_id"],
                    "state_hash": oni_bridge.canonical_state_hash(current_state),
                },
                "goal": "fixture",
                "plan": [{"incident_id": "incident-planned"}],
                "llm": {"enabled": False, "called": False, "status": "disabled"},
            }
            owner = oni_bridge.build_planned_queue_owner()
            original_gate = oni_bridge.command_pipeline_readiness

            def fail_post_sync(*args: object, **kwargs: object) -> object:
                if kwargs.get("proposal_frame") is not None:
                    raise RuntimeError("synthetic post-sync gate failure")
                return original_gate(*args, **kwargs)

            for _attempt in range(2):
                with self.subTest(attempt=_attempt), \
                        mock.patch.object(oni_bridge, "generate_plan", return_value=copy.deepcopy(generated_plan)), \
                        mock.patch.object(oni_bridge.agent_rules, "build_arbitrated_advice", return_value=copy.deepcopy(advice)), \
                        mock.patch.object(oni_bridge, "build_decision_frame", return_value=copy.deepcopy(baseline_frame)), \
                        mock.patch.object(oni_bridge, "command_pipeline_readiness", side_effect=fail_post_sync):
                    with self.assertRaisesRegex(RuntimeError, "post-sync gate failure"):
                        oni_bridge.write_outputs(root, runtime, planned_queue_owner=owner)

                active = oni_bridge.load_active_plan(root)
                self.assertEqual(active["status"], "aborted")
                self.assertIsNone(active.get("open_command_id"))
                self.assertIsNone(active.get("current_step"))
                for queue_name in oni_bridge._PLANNED_OWNER_QUEUE_NAMES:
                    queue = root / queue_name
                    self.assertFalse(list(queue.iterdir()), queue_name)
                self.assertTrue(list((root / "commands" / "quarantine").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
