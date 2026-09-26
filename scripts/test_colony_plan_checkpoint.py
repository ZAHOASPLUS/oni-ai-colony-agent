#!/usr/bin/env python3
"""Offline tests: a finished dig or blueprint keeps its plan checkpoint.

Review 2026-09-26 (M3): the next plan step required the previous step's
expected effects to still hold, so once duplicants dug the marked cell or
built the blueprint (the marker disappears) the plan reported
checkpoint_state_mismatch forever.  No game, Bridge or live directory is used.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.advanced_actions import (
    blueprint_cell_path,
    build_mark_dig_hint,
    build_place_building_blueprint_hint,
    dig_cell_path,
)
from agent.colony_plan import ColonyPlanError, _checkpoint_holds, _validate_definition
from scripts.verify_advanced_actions import (
    DIG_CELL,
    LADDER_CELL,
    blueprint_fixture,
    dig_fixture,
)


def plan_after(hint: dict, *, completed_effects: list | None = None) -> tuple[dict, dict]:
    step = {
        "id": "step-1",
        "action": hint["action"],
        "args": hint["args"],
        "preconditions": hint["preconditions"],
        "expected_effects": hint["expected_effects"],
        "status": "completed",
    }
    if completed_effects is not None:
        step["completed_effects"] = completed_effects
    checkpoint = {"step_id": "step-1", "expected_effects": copy.deepcopy(hint["expected_effects"])}
    return {"steps": [step], "last_checkpoint": checkpoint}, checkpoint


def dug(state: dict) -> dict:
    cell = state["designations"]["dig"]["by_cell"][f"cell_{DIG_CELL}"]
    cell.update(
        {
            "solid": False,
            "element_id": "Oxygen",
            "mass_kg": 0.2,
            "designation_present": False,
            "designation_instance_id": 0,
            "designation_kind": "none",
            "safe_mark_candidate": False,
            "fingerprint": "sha256:" + "c" * 64,
        }
    )
    return state


def built_ladder(state: dict) -> dict:
    cell = state["cells"]["by_id"][f"cell_{LADDER_CELL}"]
    cell.update(
        {
            "blueprint_present": False,
            "blueprint_instance_id": 0,
            "blueprint_prefab_id": "none",
            "blueprint_orientation": "none",
            "blueprint_material_tags": [],
            "object_count": 1,
            "empty_for_blueprint": False,
            "can_place_ladder": False,
            "fingerprint": "sha256:" + "d" * 64,
        }
    )
    return state


class DigCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        hint = build_mark_dig_hint(dig_fixture(), cells=[DIG_CELL])
        self.plan, self.checkpoint = plan_after(hint)

    def test_marked_cell_holds(self) -> None:
        self.assertTrue(_checkpoint_holds(self.plan, self.checkpoint, dig_fixture(designated=True)))

    def test_dug_cell_holds(self) -> None:
        self.assertTrue(_checkpoint_holds(self.plan, self.checkpoint, dug(dig_fixture(designated=True))))

    def test_cancelled_marker_on_a_solid_cell_is_a_mismatch(self) -> None:
        self.assertFalse(_checkpoint_holds(self.plan, self.checkpoint, dig_fixture(designated=False)))

    def test_dug_state_needs_observed_telemetry(self) -> None:
        state = dug(dig_fixture(designated=True))
        state["quality"][f"{dig_cell_path(DIG_CELL)}.solid"]["status"] = "estimated"
        self.assertFalse(_checkpoint_holds(self.plan, self.checkpoint, state))

    def test_a_step_that_is_not_completed_gets_no_allowance(self) -> None:
        self.plan["steps"][0]["status"] = "awaiting_confirmation"
        self.assertFalse(_checkpoint_holds(self.plan, self.checkpoint, dug(dig_fixture(designated=True))))


class BlueprintCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        hint = build_place_building_blueprint_hint(blueprint_fixture(), cells=[LADDER_CELL], material_tags=["SandStone"])
        self.hint = hint
        self.plan, self.checkpoint = plan_after(hint)

    def test_placed_blueprint_holds(self) -> None:
        self.assertTrue(_checkpoint_holds(self.plan, self.checkpoint, blueprint_fixture(ladder_placed=True)))

    def test_built_ladder_holds(self) -> None:
        self.assertTrue(
            _checkpoint_holds(self.plan, self.checkpoint, built_ladder(blueprint_fixture(ladder_placed=True)))
        )

    def test_cancelled_blueprint_is_a_mismatch(self) -> None:
        self.assertFalse(_checkpoint_holds(self.plan, self.checkpoint, blueprint_fixture(ladder_placed=False)))

    def test_declared_completed_effects_are_accepted(self) -> None:
        prefix = blueprint_cell_path(LADDER_CELL)
        state = built_ladder(blueprint_fixture(ladder_placed=True))
        # A declared finished state that holds also satisfies the checkpoint,
        # even for an action without a built-in rule.
        plan, checkpoint = plan_after(self.hint, completed_effects=[
            item for item in self.hint["expected_effects"] if item["path"] == f"{prefix}.empty_fingerprint"
        ])
        plan["steps"][0]["action"] = "set_constructable_priority"
        self.assertTrue(_checkpoint_holds(plan, checkpoint, state))
        plan["steps"][0]["completed_effects"] = [
            item for item in self.hint["expected_effects"] if item["path"] == f"{prefix}.blueprint_present"
        ]
        self.assertFalse(_checkpoint_holds(plan, checkpoint, state))


class DefinitionTests(unittest.TestCase):
    def definition(self, **extra) -> dict:
        hint = build_mark_dig_hint(dig_fixture(), cells=[DIG_CELL])
        step = {
            "id": "dig",
            "action": hint["action"],
            "args": hint["args"],
            "preconditions": hint["preconditions"],
            "expected_effects": hint["expected_effects"],
        }
        step.update(extra)
        return {"schema": "oni.colony-plan-definition.v1", "title": "t", "goal": "g", "steps": [step]}

    def test_completed_effects_are_optional_and_validated(self) -> None:
        _title, _goal, steps = _validate_definition(self.definition())
        self.assertNotIn("completed_effects", steps[0])
        prefix = dig_cell_path(DIG_CELL)
        condition = next(item for item in self.definition()["steps"][0]["expected_effects"] if item["path"] == f"{prefix}.solid")
        _title, _goal, steps = _validate_definition(self.definition(completed_effects=[condition]))
        self.assertEqual(steps[0]["completed_effects"][0]["path"], f"{prefix}.solid")
        with self.assertRaises(ColonyPlanError):
            _validate_definition(self.definition(completed_effects="solid"))
        with self.assertRaises(ColonyPlanError):
            _validate_definition(self.definition(completed_effects=[{"path": "x", "op": "bad"}]))


if __name__ == "__main__":
    unittest.main()
