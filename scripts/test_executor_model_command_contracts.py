"""Cross-language check: the executor accepts Python-approved model choices (review 2026-09-26, M1).

Builds each candidate kind through the real Bridge generation and model loop,
approves it with the real Python receipt issuer, and runs the executor's own
ValidateConditionShapes and ValidateApprovalReceipt (compiled from the Mod
sources) on the result.  Only the game-state parts of validation (live
preconditions, target lookup) remain for a clone run.
"""

from __future__ import annotations

import copy
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

from agent import approval_receipt  # noqa: E402
from agent.command_queue import load_policy  # noqa: E402
from scripts import test_model_loop  # noqa: E402
from scripts import verify_bridge_reliability as fixtures  # noqa: E402

HARNESS = ROOT / "tools" / "ActionExecutorHarness"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


def approved_choice(candidate_id: str, *, paused: bool = True):
    environment = test_model_loop.ModelLoopTests("test_choice_is_published_as_one_approvable_model_selected_command")
    environment.setUp()
    try:
        if not paused:
            environment.runtime.process_many([environment.snapshot(paused=False)])
        loop = environment.loop(test_model_loop.FakeModel(candidate_id))
        environment.tick(loop)
        loop.join_request(5)
        environment.tick(loop)
        [path] = environment.pending()
        command = json.loads(path.read_text(encoding="utf-8"))
        digest = command["source_frame_id"].split(":", 1)[1]
        frame = json.loads((environment.bridge / "decision-frames" / f"sha256-{digest}.json").read_text(encoding="utf-8"))
        policy = load_policy(environment.bridge)
        approved = approval_receipt.build_approved_command(
            command, policy, frame, approved_by="local_human", approved_at=fixtures.iso_now(),
        )
        return approved, policy
    finally:
        environment.doCleanups()


@unittest.skipUnless(shutil.which("dotnet") and (MANAGED / "Assembly-CSharp.dll").is_file(),
                     "dotnet SDK and the installed game assemblies are required")
class ExecutorModelCommandContractTests(unittest.TestCase):
    def run_harness(self, cases):
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        with tempfile.TemporaryDirectory(prefix="oni-executor-harness-") as temp:
            path = pathlib.Path(temp) / "cases.json"
            path.write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")
            run = subprocess.run(
                ["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "ActionExecutorHarness.dll"), str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])
        return {item["name"]: item for item in json.loads(run.stdout)["results"]}

    def test_every_candidate_kind_passes_the_executor_checks(self):
        cases = []
        for name, candidate_id, paused in (
            ("priority_non_ascii_name", "priority_102_Dig_2", True),
            ("dig_run", "dig_5000_3", True),
            ("resume", "resume_game", True),
            ("pause", "pause_game", False),
        ):
            command, policy = approved_choice(candidate_id, paused=paused)
            cases.append({"name": name, "command": command, "policy": policy})

        tampered = copy.deepcopy(cases[0])
        tampered["name"] = "tampered_after_approval"
        tampered["command"]["evidence"]["model_reason"] = "changed after approval"
        cases.append(tampered)
        unpaused = copy.deepcopy(cases[0])
        unpaused["name"] = "priority_without_paused_precondition"
        unpaused["command"]["preconditions"] = [
            item for item in unpaused["command"]["preconditions"] if item["path"] != "game.paused"
        ]
        cases.append(unpaused)

        results = self.run_harness(cases)
        for name in ("priority_non_ascii_name", "dig_run", "resume", "pause"):
            self.assertEqual((results[name]["shape_error"], results[name]["receipt_error"]), (None, None), name)
        self.assertIn("intent hash", results["tampered_after_approval"]["receipt_error"])
        self.assertIn("game.paused", results["priority_without_paused_precondition"]["shape_error"])


if __name__ == "__main__":
    unittest.main()
