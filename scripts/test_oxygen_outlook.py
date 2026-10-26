"""Regression: the model must see whether an oxygen producer can actually work.

The choice prompt used to assert that placing an oxygen producer addresses a
negative oxygen balance.  On this clone the diffuser requests Algae while the
world holds none, so the model kept placing producers that could never run
(2026-10-26 rounds 33-37).  ``oxygen_outlook`` publishes the facts instead.

Two traps this pins:

* the world scan is a rolling window, so its fields are ``estimated`` rather than
  ``observed``; accepting only ``observed`` silently dropped every value and once
  produced a wrong "the world has no water" reading;
* a measured zero must stay distinguishable from "not observed".
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.model_loop import build_choice_payload, oxygen_outlook  # noqa: E402


def quality(*paths: str) -> dict:
    return {
        path: {"completeness": "complete", "scope": "active_world", "status": "estimated", "source": "test"}
        for path in paths
    }


def state(**overrides):
    base = {
        "life_support_buildings": {
            "instances": [
                {
                    "prefab_id": "MineralDeoxidizer",
                    "requested_item_tag": "Algae",
                    "delivery_capacity_kg": 330,
                    "requested_item_available_kg": 0,
                    "inventory_valid": True,
                }
            ]
        },
        "materials": {
            "active_world_cells": {
                "algae_kg": 0,
                "clean_water_kg": 323.138428,
                "polluted_water_kg": 5204.005175,
                "scan_progress_ratio": 0.67,
            }
        },
    }
    base.update(overrides)
    base["quality"] = quality(
        "materials.active_world_cells.algae_kg",
        "materials.active_world_cells.clean_water_kg",
        "materials.active_world_cells.polluted_water_kg",
        "materials.active_world_cells.scan_progress_ratio",
    )
    return base


class OxygenOutlookTests(unittest.TestCase):
    def test_estimated_world_values_are_reported(self):
        outlook = oxygen_outlook(state())
        self.assertEqual(outlook["world_algae_kg"], 0)
        self.assertAlmostEqual(outlook["world_clean_water_kg"], 323.138428)
        self.assertAlmostEqual(outlook["world_polluted_water_kg"], 5204.005175)
        self.assertAlmostEqual(outlook["world_water_kg"], 5527.144, places=3)
        self.assertAlmostEqual(outlook["world_scan_progress"], 0.67)

    def test_measured_zero_is_not_confused_with_unobserved(self):
        outlook = oxygen_outlook(state())
        self.assertEqual(outlook["consumable_available_kg"], 0)
        self.assertIs(outlook["oxygen_buildings_can_operate"], False)
        # Water in the world is not enough: an Electrolyzer needs a liquid pipe
        # the action catalog cannot lay yet, so it cannot operate (B14).
        self.assertIs(outlook["electrolyzer_supply_offered"], False)
        self.assertIs(outlook["electrolyzer_can_operate"], False)

    def test_a_missing_value_reports_null(self):
        # The framework treats a present value as observed even without a quality
        # entry, so the unobserved case has to be a genuinely absent value.
        empty = state()
        empty["materials"] = {"active_world_cells": {}}
        outlook = oxygen_outlook(empty)
        self.assertIsNone(outlook["world_algae_kg"])
        self.assertIsNone(outlook["world_clean_water_kg"])
        self.assertIsNone(outlook["world_water_kg"])
        self.assertIsNone(outlook["electrolyzer_can_operate"])

    def test_an_invalid_inventory_does_not_claim_a_shortage(self):
        broken = state()
        broken["life_support_buildings"]["instances"][0]["inventory_valid"] = False
        outlook = oxygen_outlook(broken)
        self.assertIsNone(outlook["requested_consumable"])
        self.assertIsNone(outlook["oxygen_buildings_can_operate"])

    def test_the_rule_stops_claiming_producers_help(self):
        payload = build_choice_payload(state(), [], [], "goal", (), autonomous=True, notes=["oxygen_consumable_unavailable:Algae"])
        rules = [item for item in payload["rules"] if "oxygen_balance" in item]
        self.assertEqual(len(rules), 1)
        self.assertIn("none of the consumable", rules[0])
        # The rule once said "an electrolyser would work" because the world held
        # water, and the model built six that never ran (B14).
        self.assertNotIn("electrolyser would work", rules[0])
        self.assertIn("liquid pipe", rules[0])
        self.assertEqual(payload["candidate_notes"], ["oxygen_consumable_unavailable:Algae"])
        self.assertIs(payload["resource_outlook"]["oxygen_buildings_can_operate"], False)

    def test_a_supplied_producer_keeps_the_original_rule(self):
        supplied = state()
        supplied["life_support_buildings"]["instances"][0]["requested_item_available_kg"] = 500.0
        payload = build_choice_payload(supplied, [], [], "goal", (), autonomous=True)
        rules = [item for item in payload["rules"] if "oxygen_balance" in item]
        self.assertIn("addresses it before an alert fires", rules[0])
        self.assertIs(payload["resource_outlook"]["oxygen_buildings_can_operate"], True)


if __name__ == "__main__":
    unittest.main()
