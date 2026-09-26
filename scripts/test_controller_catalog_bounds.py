"""Regression: controller accepts every catalogued action shape (review 2026-09-26, M2).

The controller spelled "not equal" as "neq" while field contracts, Colony
Plan, the command queue and the C# executor use "ne", so every blueprint
placement (whose effects contain a quality-contract "ne" fingerprint
condition) was rejected.  A 32-condition cap also limited Ladder to 2 cells,
Wire to 1 cell and dig to 4 cells, far below the catalog.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.advanced_actions import (  # noqa: E402
    BLUEPRINT_CELL_FIELDS,
    MAX_BUILDING_BLUEPRINT_CELLS,
    MAX_DIG_CELLS,
    MAX_UTILITY_BLUEPRINT_CELLS,
    blueprint_cell_path,
    build_mark_dig_hint,
    build_place_building_blueprint_hint,
    build_place_utility_blueprint_path_hint,
    dig_cell_path,
)
from agent.controller_loop import ActionPolicy, Condition, _policy_blockers  # noqa: E402
from agent.field_contracts import (  # noqa: E402
    BLUEPRINT_CELL_CONTRACT_ID,
    DIG_CELL_CONTRACT_ID,
    contract_quality_fixture,
)
from scripts import verify_advanced_actions as fixtures  # noqa: E402


def blueprint_state(cells: list[int]) -> dict:
    """blueprint_fixture() extended with empty, horizontally adjacent cells."""

    state = fixtures.blueprint_fixture()
    template = state["cells"]["by_id"][f"cell_{fixtures.WIRE_CELLS[0]}"]
    for index, cell in enumerate(cells):
        details = copy.deepcopy(template)
        empty = "sha256:" + f"{cell:064x}"[-64:]
        details.update(cell=cell, x=100 + index, y=10, empty_fingerprint=empty, fingerprint=empty)
        state["cells"]["by_id"][f"cell_{cell}"] = details
        for field in BLUEPRINT_CELL_FIELDS:
            state["quality"][f"{blueprint_cell_path(cell)}.{field}"] = contract_quality_fixture(
                BLUEPRINT_CELL_CONTRACT_ID
            )
    return state


def dig_state(cells: list[int]) -> dict:
    """dig_fixture() extended with distinct safe dig candidates."""

    state = fixtures.dig_fixture()
    by_cell = state["designations"]["dig"]["by_cell"]
    template = by_cell[f"cell_{fixtures.DIG_CELL}"]
    for index, cell in enumerate(cells):
        details = copy.deepcopy(template)
        details.update(cell=cell, x=200 + index, fingerprint="sha256:" + f"{cell:064x}"[-64:])
        by_cell[f"cell_{cell}"] = details
        for field in fixtures.DIG_FIELDS:
            state["quality"][f"{dig_cell_path(cell)}.{field}"] = contract_quality_fixture(DIG_CELL_CONTRACT_ID)
    state["designations"]["dig"]["candidate_cells"] = sorted(set(cells) | {fixtures.DIG_CELL})
    return state


class CatalogBoundsTests(unittest.TestCase):
    def assert_controller_accepts(self, hint: dict) -> None:
        blockers = _policy_blockers(ActionPolicy.from_hint(hint))
        self.assertEqual(blockers, [], f"{hint['action']}: {blockers[:5]}")

    def test_largest_ladder_placement_fits(self):
        cells = list(range(1000, 1000 + MAX_BUILDING_BLUEPRINT_CELLS))
        hint = build_place_building_blueprint_hint(blueprint_state(cells), cells=cells, material_tags=["SandStone"])
        self.assertTrue(any(item["op"] == "ne" for item in hint["expected_effects"]))
        self.assert_controller_accepts(hint)

    def test_largest_wire_path_fits(self):
        cells = list(range(2000, 2000 + MAX_UTILITY_BLUEPRINT_CELLS))
        hint = build_place_utility_blueprint_path_hint(
            blueprint_state(cells), path_id="wirepathmax", cells=cells, material_tags=["Copper"]
        )
        self.assert_controller_accepts(hint)

    def test_largest_dig_batch_fits(self):
        cells = list(range(3000, 3000 + MAX_DIG_CELLS))
        self.assert_controller_accepts(build_mark_dig_hint(dig_state(cells), cells=cells))

    def test_not_equal_spellings_share_one_canonical_operator(self):
        for spelling in ("ne", "neq", "!="):
            condition = Condition("value", spelling, 3)
            self.assertEqual(condition.op, "ne")
            self.assertEqual(condition.as_dict()["op"], "ne")
            self.assertEqual(condition.blockers_for({"value": 4}), [])
            self.assertEqual(condition.blockers_for({"value": 3}), ["condition_not_met:value"])


if __name__ == "__main__":
    unittest.main()
