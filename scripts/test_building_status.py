"""Regression (B14): the controller must see the buildings it already has.

Phase 2 built six Electrolyzers that never ran.  The completed-prefab counts
covered only diffusers, outhouses and manual generators, so the controller could
not see the first Electrolyzer and kept offering another; and the outlook called
the Electrolyzer operable because the world held water, although it only takes
water through a liquid pipe the action catalog cannot lay
(docs/b2-blueprint-lifecycle-20261027.md).

The fixtures are self-contained: no live bridge state is read.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import candidates as C  # noqa: E402
from agent.field_contracts import (  # noqa: E402
    BUILDING_COMPLETED_COUNT_CONTRACT_ID,
    GAME_PAUSED_CONTRACT_ID,
    contract_quality_fixture,
)
from agent.model_loop import colony_summary  # noqa: E402

COUNT_KEYS = {
    "oxygen_diffuser": 2,
    "algae_terrarium": 0,
    "electrolyzer": 6,
    "microbe_musher": 0,
    "farm_tile": 0,
}


def life_support(*instances, status="observed"):
    return {
        "instances": list(instances),
        "tracked_prefab_ids": ["MineralDeoxidizer", "AlgaeHabitat", "Electrolyzer"],
        "complete": True,
    }, {"status": status, "completeness": "complete", "scope": "active_world", "source": "test"}


def instance(prefab_id, operational, status_item_ids=()):
    return {
        "prefab_id": prefab_id, "operational": operational, "functional": True, "inventory_valid": True,
        "status_item_ids": list(status_item_ids),
    }


# Placeholder ids: the real status item ids are only known from a live run.
ELECTROLYZER_STATUS = ("FixtureNoLiquidInput", "FixtureNoPower")


def phase2_state(*, paused=True, life_status="observed", diffuser_working=False):
    """The shape of the final Phase 2 state: two idle diffusers, six idle Electrolyzers."""

    support, support_quality = life_support(
        instance("MineralDeoxidizer", diffuser_working),
        instance("MineralDeoxidizer", False),
        *[instance("Electrolyzer", False, ELECTROLYZER_STATUS) for _ in range(6)],
        status=life_status,
    )
    quality = {f"buildings.{key}": contract_quality_fixture(BUILDING_COMPLETED_COUNT_CONTRACT_ID) for key in COUNT_KEYS}
    quality["game.paused"] = contract_quality_fixture(GAME_PAUSED_CONTRACT_ID)
    quality["life_support_buildings"] = support_quality
    return {
        "event": "state_snapshot",
        "ts": "2026-10-26T03:18:30+00:00",
        "game": {"paused": paused},
        "buildings": dict(COUNT_KEYS, captured_at="2026-10-26T03:18:29+00:00"),
        "life_support_buildings": support,
        "quality": quality,
    }


class BuildingStatusTests(unittest.TestCase):
    def test_counts_and_working_instances(self):
        status = C.building_status(phase2_state())
        self.assertEqual(
            status["Electrolyzer"],
            {"built": 6, "operational": 0, "idle_status": ["FixtureNoLiquidInput", "FixtureNoPower"]},
        )
        self.assertEqual(status["MineralDeoxidizer"], {"built": 2, "operational": 0, "idle_status": []})
        self.assertEqual(status["AlgaeHabitat"], {"built": 0, "operational": 0, "idle_status": []})
        # Not tracked by the life-support reader: working state unobserved.
        self.assertEqual(status["MicrobeMusher"], {"built": 0, "operational": None, "idle_status": None})

    def test_unobserved_life_support_reports_null_not_zero(self):
        status = C.building_status(phase2_state(life_status="estimated"))
        self.assertEqual(status["Electrolyzer"], {"built": 6, "operational": None, "idle_status": None})

    def test_a_count_without_quality_is_not_used(self):
        state = phase2_state()
        del state["quality"]["buildings.electrolyzer"]
        self.assertIsNone(C.building_status(state)["Electrolyzer"]["built"])

    def test_idle_buildings_need_an_observed_zero(self):
        self.assertEqual(
            C._idle_built_buildings(C.building_status(phase2_state())),
            {"Electrolyzer": 6, "MineralDeoxidizer": 2},
        )
        self.assertEqual(C._idle_built_buildings(C.building_status(phase2_state(life_status="estimated"))), {})
        working = C.building_status(phase2_state(diffuser_working=True))
        self.assertNotIn("MineralDeoxidizer", C._idle_built_buildings(working))

    def test_the_colony_summary_carries_the_building_status(self):
        self.assertEqual(colony_summary(phase2_state())["buildings"]["Electrolyzer"]["operational"], 0)
        self.assertEqual(
            colony_summary(phase2_state())["buildings"]["Electrolyzer"]["idle_status"],
            ["FixtureNoLiquidInput", "FixtureNoPower"],
        )

    def test_working_instances_do_not_contribute_idle_reasons(self):
        state = phase2_state(diffuser_working=True)
        state["life_support_buildings"]["instances"][0]["status_item_ids"] = ["FixtureWorking"]
        self.assertEqual(C.building_status(state)["MineralDeoxidizer"]["idle_status"], [])


class PlacementCandidateTests(unittest.TestCase):
    """Which buildings generate_candidates still offers for the Phase 2 state."""

    def offered(self, state):
        offered = []

        def fake_building(snapshot, building_def_id, prefix, label, note=""):
            offered.append(building_def_id)
            return None

        with mock.patch.object(C, "_building_candidate", side_effect=fake_building):
            result = C.generate_candidates(state, allowed_actions=("place_building_blueprint",), autonomous=True)
        return offered, result["notes"]

    def test_idle_buildings_and_the_unsuppliable_electrolyzer_are_withheld(self):
        offered, notes = self.offered(phase2_state())
        self.assertNotIn("Electrolyzer", offered)
        self.assertNotIn("MineralDeoxidizer", offered)
        self.assertIn("MicrobeMusher", offered)
        self.assertIn("built_not_operational:Electrolyzer:6", notes)
        self.assertIn("built_not_operational:MineralDeoxidizer:2", notes)
        self.assertIn("electrolyzer_needs_power_and_liquid_pipe", notes)

    def test_a_working_diffuser_keeps_its_candidate(self):
        offered, notes = self.offered(phase2_state(diffuser_working=True))
        self.assertIn("MineralDeoxidizer", offered)
        self.assertNotIn("built_not_operational:MineralDeoxidizer:2", notes)

    def test_the_electrolyzer_returns_once_a_pipe_can_be_laid(self):
        with mock.patch.object(C, "UTILITY_BLUEPRINT_ALLOWLIST", {"Wire", "LiquidConduit"}):
            self.assertTrue(C.electrolyzer_supply_offered())
            state = phase2_state()
            state["buildings"]["electrolyzer"] = 0
            state["life_support_buildings"]["instances"] = []
            offered, notes = self.offered(state)
        self.assertIn("Electrolyzer", offered)
        self.assertNotIn("electrolyzer_needs_power_and_liquid_pipe", notes)


if __name__ == "__main__":
    unittest.main()
