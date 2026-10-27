"""The single-building blueprint allowlist beyond Ladder (2026-09-29).

MineralDeoxidizer (Oxygen Diffuser) and AlgaeHabitat (Algae Terrarium) are
offered to the model loop; every builder derives the Probe's per-building
placement field from the building id, and an id outside the allowlist is
refused before any state is read.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import advanced_actions  # noqa: E402
from agent.field_contracts import (  # noqa: E402
    BLUEPRINT_CATALOG_CONTRACT_ID,
    BLUEPRINT_CELL_CONTRACT_ID,
    contract_id_for_path,
)


class BlueprintAllowlistTests(unittest.TestCase):
    def test_placement_fields_follow_the_building_id(self):
        names = ("Ladder", "Wire", "MineralDeoxidizer", "AlgaeHabitat")
        self.assertEqual(
            [advanced_actions.blueprint_can_place_field(item) for item in names],
            ["can_place_ladder", "can_place_wire", "can_place_mineral_deoxidizer", "can_place_algae_habitat"],
        )

    def test_allowlisted_buildings_have_catalog_and_cell_contracts(self):
        for building in sorted(advanced_actions.BUILDING_BLUEPRINT_ALLOWLIST):
            self.assertEqual(
                contract_id_for_path(f"blueprints.catalog.{building}.available"), BLUEPRINT_CATALOG_CONTRACT_ID
            )
            field = advanced_actions.blueprint_can_place_field(building)
            self.assertEqual(contract_id_for_path(f"cells.by_id.cell_12.{field}"), BLUEPRINT_CELL_CONTRACT_ID)
        # A building outside the allowlist has no trusted contract at all.  The
        # Electrolyzer and then the Liquid Pump served as the example until they
        # were allowlisted (round 44, B15); the Battery is not.
        self.assertNotIn("Battery", advanced_actions.BUILDING_BLUEPRINT_ALLOWLIST)
        self.assertIsNone(contract_id_for_path("blueprints.catalog.Battery.available"))
        self.assertIsNone(contract_id_for_path("cells.by_id.cell_12.can_place_battery"))

    def test_a_building_outside_the_allowlist_is_refused_before_reading_state(self):
        with self.assertRaisesRegex(ValueError, "allowlist"):
            advanced_actions.build_place_building_blueprint_hint(
                {}, cells=[1], material_tags=["Cuprite"], building_def_id="Battery"
            )


if __name__ == "__main__":
    unittest.main()
