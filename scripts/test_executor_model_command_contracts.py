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
import os
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
from agent.advanced_actions import build_cancel_building_blueprint_hint, blueprint_can_place_field  # noqa: E402
from agent.command_queue import load_policy  # noqa: E402
from agent.field_contracts import BLUEPRINT_INSTANCE_CONTRACT_ID, contract_quality_fixture  # noqa: E402
from scripts import test_model_loop  # noqa: E402
from scripts import verify_bridge_reliability as fixtures  # noqa: E402

HARNESS = ROOT / "tools" / "ActionExecutorHarness"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


def placed_oxygen_state(def_id, cell, material, *, instance_id=73101):
    """Synthetic unstarted blueprint only; this is not an executed game action."""
    state = test_model_loop.CandidateTests().state(paused=True)
    values = state["cells"]["by_id"][f"cell_{cell}"]
    values.update({
        "object_count": 1, "empty_for_blueprint": False,
        blueprint_can_place_field(def_id): False,
        "blueprint_present": True, "blueprint_instance_id": instance_id,
        "blueprint_prefab_id": def_id, "blueprint_orientation": "Neutral",
        "blueprint_material_tags": [material], "blueprint_in_progress": False,
        "blueprint_is_utility": False, "fingerprint": "sha256:" + "c" * 64,
    })
    instance = {"instance_id": instance_id, "cell": cell, "world_id": 0,
                "prefab_id": def_id, "orientation": "Neutral", "material_tags": [material],
                "in_progress": False, "is_utility": False, "fingerprint": values["fingerprint"]}
    state["blueprints"]["by_instance_id"][f"id_{instance_id}"] = instance
    for field in instance:
        state["quality"][f"blueprints.by_instance_id.id_{instance_id}.{field}"] = contract_quality_fixture(
            BLUEPRINT_INSTANCE_CONTRACT_ID)
    return state


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
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q",
             "/p:UseSharedCompilation=false"],
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
        evidence_dir = os.environ.get("ONI_EXECUTOR_TEST_EVIDENCE_DIR")
        if evidence_dir:
            output = pathlib.Path(evidence_dir).resolve()
            output.mkdir(parents=True, exist_ok=True)
            # Create-only raw fixture inputs/results for the current test method.
            with (output / (self._testMethodName + "-input.json")).open("x", encoding="utf-8") as stream:
                stream.write(json.dumps({"cases": cases}, ensure_ascii=False, indent=2) + "\n")
            with (output / (self._testMethodName + "-output.json")).open("x", encoding="utf-8") as stream:
                stream.write(run.stdout)
        results = json.loads(run.stdout)["results"]
        expected_names = [item["name"] for item in cases]
        self.assertEqual(len(set(expected_names)), len(expected_names), "Fixture case names must be unique")
        self.assertCountEqual([item["name"] for item in results], expected_names)
        return {item["name"]: item for item in results}

    def test_every_candidate_kind_passes_the_executor_checks(self):
        cases = []
        for name, candidate_id, paused in (
            ("priority_non_ascii_name", "priority_102_Dig_2", True),
            ("dig_run", "dig_5000_3", True),
            ("resume", "resume_game", True),
            ("pause", "pause_game", False),
            ("research_while_running", "research_ImprovedOxygen", False),
            ("ladder", "ladder_9000", True),
            ("oxygen_diffuser", "oxygen_diffuser_9100", True),
            ("algae_terrarium", "algae_terrarium_9200", True),
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
        for name in ("priority_non_ascii_name", "dig_run", "resume", "pause", "research_while_running"):
            self.assertEqual((results[name]["shape_error"], results[name]["receipt_error"]), (None, None), name)
            self.assertIs(results[name]["full_shape_validated"], True, name)
            # A check that needed live game state would be reported as an exception, not as a pass.
            self.assertEqual(
                {key: value for key, value in results[name].items() if key.endswith("_exception")}, {}, name
            )
        # The blueprint shape check resolves the allowlisted BuildingDef through
        # Assets.GetBuildingDef, which only exists inside the running game, so the
        # harness proves receipts and pure cell contracts, never the native
        # BuildingDef/footprint check for any of these three buildings.
        for name in ("ladder", "oxygen_diffuser", "algae_terrarium"):
            result = results[name]
            self.assertEqual(result["receipt_error"], None)
            self.assertNotIn("receipt_exception", result)
            self.assertIn("NullReferenceException", result.get("shape_exception", ""))
            self.assertIs(result["full_shape_validated"], False)
            self.assertEqual(len(result["cell_contracts"]), 1)
            self.assertIsNone(result["cell_contracts"][0]["error"])
            self.assertNotIn("exception", result["cell_contracts"][0])
        self.assertIn("intent hash", results["tampered_after_approval"]["receipt_error"])
        self.assertIn("game.paused", results["priority_without_paused_precondition"]["shape_error"])
        self.assertIs(results["priority_without_paused_precondition"]["full_shape_validated"], False)

    def test_oxygen_placement_contract_and_receipt_reject_changed_fields(self):
        cases = []
        for candidate_id, def_id, cell in (("oxygen_diffuser_9100", "MineralDeoxidizer", 9100),
                                         ("algae_terrarium_9200", "AlgaeHabitat", 9200)):
            command, policy = approved_choice(candidate_id)
            prefix = f"cells.by_id.cell_{cell}."
            for label, section, field, replacement in (
                ("wrong_prefab", "expected_effects", "blueprint_prefab_id", "Ladder"),
                ("construction_started", "expected_effects", "blueprint_in_progress", True),
                ("physical_mass_changed", "expected_effects", "mass_kg", 42.0),
                ("placement_not_allowed", "preconditions", blueprint_can_place_field(def_id), False),
            ):
                changed = copy.deepcopy(command)
                condition = next(item for item in changed[section] if item["path"] == prefix + field)
                condition["value"] = replacement
                cases.append({"name": def_id + "_" + label, "command": changed, "policy": policy})
        for name, result in self.run_harness(cases).items():
            self.assertTrue(result["cell_contracts"][0]["error"], name)
            self.assertNotIn("exception", result["cell_contracts"][0], name)
            # The pure cell error cannot stand in for the top-level shape result:
            # offline native BuildingDef lookup must remain explicitly unverified.
            self.assertIs(result["full_shape_validated"], False, name)
            self.assertIn("NullReferenceException", result.get("shape_exception", ""), name)
            self.assertIn("intent hash", result["receipt_error"], name)

    def test_unstarted_oxygen_blueprint_cancel_contract_requires_exact_restoration(self):
        cases = []
        for def_id, cell, material in (("MineralDeoxidizer", 9100, "Cuprite"),
                                      ("AlgaeHabitat", 9200, "Dirt")):
            state = placed_oxygen_state(def_id, cell, material)
            hint = build_cancel_building_blueprint_hint(state, cells=[cell])
            cases.append({"name": def_id, "command": hint, "policy": {}})
            changed = copy.deepcopy(hint)
            restored = next(item for item in changed["expected_effects"]
                            if item["path"] == f"cells.by_id.cell_{cell}.fingerprint")
            restored["value"] = "sha256:" + "d" * 64
            cases.append({"name": def_id + "_wrong_restoration", "command": changed, "policy": {}})
            started = copy.deepcopy(state)
            started["cells"]["by_id"][f"cell_{cell}"]["blueprint_in_progress"] = True
            with self.assertRaises(ValueError):
                build_cancel_building_blueprint_hint(started, cells=[cell])
        for name, result in self.run_harness(cases).items():
            contract = result["cell_contracts"][0]
            self.assertNotIn("exception", contract, name)
            if name.endswith("wrong_restoration"):
                self.assertTrue(contract["error"], name)
                self.assertIs(result["full_shape_validated"], False, name)
            else:
                self.assertIsNone(contract["error"], name)
                self.assertIs(result["full_shape_validated"], True, name)
            # These are unsigned hints, not executed or approved cancellations.
            self.assertTrue(result["receipt_error"], name)

    def test_building_cancel_requires_one_prefab_across_targets(self):
        cases = []
        pairs = (("MineralDeoxidizer", "MineralDeoxidizer"),
                 ("AlgaeHabitat", "AlgaeHabitat"),
                 ("MineralDeoxidizer", "AlgaeHabitat"),
                 ("AlgaeHabitat", "MineralDeoxidizer"),
                 ("Ladder", "MineralDeoxidizer"))
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                state = placed_oxygen_state(first, 9100, "Cuprite")
                next_state = placed_oxygen_state(second, 9200, "Dirt", instance_id=73102)
                state["cells"]["by_id"]["cell_9200"] = next_state["cells"]["by_id"]["cell_9200"]
                state["blueprints"]["by_instance_id"].update(next_state["blueprints"]["by_instance_id"])
                state["quality"].update(next_state["quality"])
                if first != second:
                    with self.assertRaisesRegex(ValueError, "one.*blueprint_prefab_id"):
                        build_cancel_building_blueprint_hint(state, cells=[9100, 9200])
                    continue
                hint = build_cancel_building_blueprint_hint(state, cells=[9100, 9200])
                self.assertEqual(hint["args"]["targets"], [
                    {"cell": 9100, "blueprint_instance_id": 73101},
                    {"cell": 9200, "blueprint_instance_id": 73102},
                ])
                cases.append({"name": first + "_two_targets", "command": hint, "policy": {}})
        for name, result in self.run_harness(cases).items():
            self.assertIs(result["full_shape_validated"], True, name)
            self.assertEqual(len(result["cell_contracts"]), 2, name)
            for cell in result["cell_contracts"]:
                self.assertIsNone(cell["error"], name)
                self.assertNotIn("exception", cell, name)
            self.assertTrue(result["receipt_error"], name)  # Unsigned fixture only.


if __name__ == "__main__":
    unittest.main()
