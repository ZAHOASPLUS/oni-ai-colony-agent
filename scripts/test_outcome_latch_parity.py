"""Parity: C# OutcomeLatch agrees with the Python pipeline gate (review 2026-09-26, P0-2).

The executor now rebuilds its durable latch from failed/ records at session
start instead of forgetting it on every reload.  Both sides must agree on
which failed records latch; for records the C# streaming reader cannot prove
(duplicate keys, malformed JSON, trailing data) it must latch, never be more
permissive than the Python gate.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.command_queue import (  # noqa: E402
    OPERATOR_RESOLUTION_SCHEMA,
    _is_unresolved_outcome_unknown,
)

HARNESS = ROOT / "tools" / "OutcomeLatchHarness"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)
BIG_STATE = {"cells": [{"cell": index, "mass_kg": 1.5, "element": "Oxygen"} for index in range(40000)]}


def record(command_id: str, **fields) -> dict:
    value = {"schema": "oni.command.v3", "id": command_id, "action": "set_paused", "status": "failed"}
    value.update(fields)
    return value


def result(classification=None) -> dict:
    value = {"message": "fixture", "before_state": BIG_STATE, "after_state": BIG_STATE}
    if classification is not None:
        value["classification"] = classification
    return value


def resolution(command_id: str, **changes) -> dict:
    value = {
        "schema": OPERATOR_RESOLUTION_SCHEMA,
        "command_id": command_id,
        "resolved_by": "local_human",
        "resolved_at": "2026-09-26T12:00:00+08:00",
        "observed_outcome": "effect_absent",
        "reason": "Checked in game.",
    }
    value.update(changes)
    return value


WELL_FORMED = {
    "unknown_top": record("unknown_top", classification="outcome_unknown"),
    "unknown_result": record("unknown_result", result=result("outcome_unknown")),
    "behavior_fail": record("behavior_fail", result=result("behavior_fail")),
    "rejected": record("rejected", result=result("rejected")),
    "runtime_fail": record("runtime_fail", result=result("runtime_fail")),
    "raw_claim": record("raw_claim", status="executing", execution_claim={"dispatch": {"state": "returned"}}),
    "noop_unclassified": record("noop_unclassified", action="noop"),
    "focus_note_unknown": record("focus_note_unknown", action="focus_note", result=result("outcome_unknown")),
    "resolved": record("resolved", result=result("outcome_unknown"), operator_resolution=resolution("resolved")),
    "resolution_wrong_id": record(
        "resolution_wrong_id", result=result("outcome_unknown"), operator_resolution=resolution("other")
    ),
    "resolution_bad_outcome": record(
        "resolution_bad_outcome",
        result=result("outcome_unknown"),
        operator_resolution=resolution("resolution_bad_outcome", observed_outcome="maybe"),
    ),
    "resolution_blank_reason": record(
        "resolution_blank_reason",
        result=result("outcome_unknown"),
        operator_resolution=resolution("resolution_blank_reason", reason="   "),
    ),
    "unknown_classification": record("unknown_classification", result=result("weird")),
    "definite_pair": record("definite_pair", classification="behavior_fail", result=result("rejected")),
    "mixed_unknown": record("mixed_unknown", classification="behavior_fail", result=result("outcome_unknown")),
}
UNPROVABLE = {
    "duplicate_keys": '{"id":"duplicate_keys","action":"set_paused","classification":"outcome_unknown",'
                      '"classification":"behavior_fail"}',
    "malformed": '{"id": "malformed", "action": ',
    "trailing": '{"id":"trailing","action":"set_paused","result":{"classification":"behavior_fail"}} {}',
}


@unittest.skipUnless(shutil.which("dotnet") and (MANAGED / "Newtonsoft.Json.dll").is_file(),
                     "dotnet SDK and the game's Newtonsoft.Json.dll are required")
class OutcomeLatchParityTests(unittest.TestCase):
    def run_harness(self, directory: pathlib.Path) -> dict:
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        dll = HARNESS / "bin" / "Release" / "net8.0" / "OutcomeLatchHarness.dll"
        run = subprocess.run(
            ["dotnet", str(dll), str(directory)], capture_output=True, text=True, errors="replace"
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_csharp_latch_matches_python_gate(self):
        with tempfile.TemporaryDirectory(prefix="oni-latch-parity-") as temp:
            failed = pathlib.Path(temp)
            for name, value in WELL_FORMED.items():
                (failed / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
            for name, text in UNPROVABLE.items():
                (failed / f"{name}.json").write_text(text, encoding="utf-8")
            report = self.run_harness(failed)

        files = report["files"]
        self.assertEqual(report["signature_lines"], len(WELL_FORMED) + len(UNPROVABLE))
        for name, value in WELL_FORMED.items():
            with self.subTest(name=name):
                self.assertTrue(files[f"{name}.json"]["readable"])
                self.assertEqual(files[f"{name}.json"]["latches"], _is_unresolved_outcome_unknown(value))
        for name in UNPROVABLE:
            with self.subTest(name=name):
                self.assertFalse(files[f"{name}.json"]["readable"])
                self.assertTrue(files[f"{name}.json"]["latches"])
        self.assertIsNotNone(report["first_unresolved"])

    def test_resolved_directory_releases_latch(self):
        with tempfile.TemporaryDirectory(prefix="oni-latch-parity-") as temp:
            failed = pathlib.Path(temp)
            for name in ("resolved", "behavior_fail", "noop_unclassified"):
                (failed / f"{name}.json").write_text(json.dumps(WELL_FORMED[name]), encoding="utf-8")
            report = self.run_harness(failed)
        self.assertIsNone(report["first_unresolved"])


if __name__ == "__main__":
    unittest.main()
