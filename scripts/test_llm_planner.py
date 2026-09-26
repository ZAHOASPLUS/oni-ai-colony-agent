#!/usr/bin/env python3
"""Focused regression tests for :mod:`agent.llm_planner`."""

from __future__ import annotations

import errno
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import llm_planner


def winerror_5() -> OSError:
    error = OSError(errno.EINVAL, "synthetic destination lock")
    error.winerror = 5
    return error


class AtomicWriteJsonTests(unittest.TestCase):
    def test_retries_transient_destination_lock_then_replaces(self) -> None:
        original_replace = pathlib.Path.replace
        replace_calls = 0

        def flaky_replace(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls <= 2:
                raise winerror_5()
            return original_replace(source, target)

        with tempfile.TemporaryDirectory(prefix="oni-llm-atomic-") as root:
            target = pathlib.Path(root) / "plan.json"
            target.write_text('{"status": "old"}\n', encoding="utf-8")
            with (
                mock.patch.object(pathlib.Path, "replace", new=flaky_replace),
                mock.patch.object(llm_planner.time, "sleep") as sleep,
            ):
                llm_planner.atomic_write_json(target, {"status": "ok"})

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"status": "ok"})
            self.assertEqual(replace_calls, 3)
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list],
                [
                    llm_planner._ATOMIC_REPLACE_INITIAL_DELAY_SECONDS,
                    llm_planner._ATOMIC_REPLACE_INITIAL_DELAY_SECONDS * 2,
                ],
            )
            self.assertFalse(list(pathlib.Path(root).glob("*.tmp")))

    def test_retry_budget_is_bounded_and_temp_file_is_removed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-llm-atomic-") as root:
            target = pathlib.Path(root) / "plan.json"
            target.write_text('{"status": "old"}\n', encoding="utf-8")
            with (
                mock.patch.object(pathlib.Path, "replace", side_effect=PermissionError(errno.EACCES, "synthetic destination lock")) as replace,
                mock.patch.object(llm_planner.time, "sleep") as sleep,
            ):
                with self.assertRaises(PermissionError):
                    llm_planner.atomic_write_json(target, {"status": "blocked"})
            self.assertEqual(replace.call_count, llm_planner._ATOMIC_REPLACE_ATTEMPTS)
            expected_delays = []
            delay = llm_planner._ATOMIC_REPLACE_INITIAL_DELAY_SECONDS
            for _ in range(llm_planner._ATOMIC_REPLACE_ATTEMPTS - 1):
                expected_delays.append(delay)
                delay = min(delay * 2, llm_planner._ATOMIC_REPLACE_MAX_DELAY_SECONDS)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], expected_delays)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"status": "old"})
            self.assertFalse(list(pathlib.Path(root).glob("*.tmp")))

    def test_non_retryable_replace_error_fails_immediately(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-llm-atomic-") as root:
            target = pathlib.Path(root) / "plan.json"
            with (
                mock.patch.object(pathlib.Path, "replace", side_effect=FileNotFoundError(errno.ENOENT, "synthetic missing source")) as replace,
                mock.patch.object(llm_planner.time, "sleep") as sleep,
            ):
                with self.assertRaises(FileNotFoundError):
                    llm_planner.atomic_write_json(target, {"status": "missing"})
            self.assertEqual(replace.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse(list(pathlib.Path(root).glob("*.tmp")))


class ColonyPlanProjectionTests(unittest.TestCase):
    def _inputs(self):
        advice = [
            {
                "incident_id": "inc-1",
                "reason": "keep oxygen stable",
                "command": {
                    "action": "focus_note",
                    "args": {"note": "inspect oxygen"},
                    "preconditions": [{"path": "game.paused", "op": "eq", "value": True}],
                    "expected_effects": [{"path": "game.paused", "op": "eq", "value": True}],
                },
            }
        ]
        plan = {
            "schema": "oni.plan.v2",
            "mode": "llm_ranked",
            "goal": "preserve colony",
            "prompt_hash": "sha256:" + "a" * 64,
            "incident_fingerprint": "sha256:" + "b" * 64,
            "plan": [{"incident_id": "inc-1"}],
            "llm": {
                "status": "ok",
                "model": "synthetic-model",
                "response_hash": "sha256:" + "c" * 64,
            },
        }
        return plan, advice

    def test_projection_copies_only_local_command_contract(self) -> None:
        plan, advice = self._inputs()
        definition = llm_planner.colony_plan_definition_from_plan(plan, advice)
        self.assertEqual(definition["schema"], "oni.colony-plan-definition.v1")
        self.assertEqual(definition["steps"][0]["action"], "focus_note")
        self.assertEqual(definition["steps"][0]["args"], {"note": "inspect oxygen"})
        self.assertEqual(definition["decision_provenance"]["response_hash"], "sha256:" + "c" * 64)

    def test_projection_rejects_missing_local_contract(self) -> None:
        plan, advice = self._inputs()
        advice[0]["command"].pop("expected_effects")
        with self.assertRaises(ValueError):
            llm_planner.colony_plan_definition_from_plan(plan, advice)

    def test_projection_rejects_unconfirmed_llm_mode(self) -> None:
        plan, advice = self._inputs()
        plan["llm"]["status"] = "timeout"
        with self.assertRaises(ValueError):
            llm_planner.colony_plan_definition_from_plan(plan, advice)

if __name__ == "__main__":
    unittest.main(verbosity=2)
