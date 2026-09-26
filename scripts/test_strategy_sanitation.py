#!/usr/bin/env python3
"""Offline tests: sanitation counts flush toilets as well as Outhouses.

Review 2026-09-26 (M7 / P2 "hygiene target only counts Outhouse"): a colony
that uses only flush toilets was reported as a sanitation emergency, and the
live Probe's estimated building counts left sanitation unknown even with a
trusted functional instance view.  No game, Bridge or live directory is used.
"""

from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.field_contracts import (
    FUNCTIONAL_BUILDINGS_CONTRACT_ID,
    FUNCTIONAL_BUILDINGS_SCHEMA,
    contract_quality_fixture,
)
from scripts.verify_strategy import base_state, evaluate_fixture, objective, observed


def functional(state: dict, toilets: list, *, prefab_ids: list | None = None) -> dict:
    payload = {
        "schema": FUNCTIONAL_BUILDINGS_SCHEMA,
        "captured_at": state["ts"],
        "world_id": 0,
        "complete": True,
        "instances": [
            {"instance_id": 300 + index, "world_id": 0, "prefab_id": prefab, "operational": value}
            for index, (prefab, value) in enumerate(toilets)
        ],
    }
    if prefab_ids is not None:
        payload["prefab_ids"] = prefab_ids
    state["functional_buildings"] = payload
    state["quality"]["functional_buildings"] = contract_quality_fixture(FUNCTIONAL_BUILDINGS_CONTRACT_ID)
    return state


def live_counts(state: dict) -> dict:
    """The live Probe marks its completed-prefab counts estimated."""

    state["quality"]["buildings.outhouse"] = {"status": "estimated", "source": "typed_active_world"}
    return state


def sanitation(state: dict) -> dict:
    return objective(evaluate_fixture(state), "sanitation")


class SanitationTests(unittest.TestCase):
    def test_flush_toilets_cover_a_colony_without_outhouses(self) -> None:
        state = base_state()
        state["buildings"]["outhouse"] = 0
        state["buildings"]["flush_toilet"] = 1
        state["quality"]["buildings.flush_toilet"] = observed("buildings.flush_toilet")
        self.assertEqual(sanitation(state)["status"], "satisfied")

    def test_too_few_outhouses_without_flush_telemetry_is_not_an_emergency(self) -> None:
        state = base_state()
        state["buildings"]["outhouse"] = 0
        result = sanitation(state)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("flush toilets are not observed", result["reason"])

    def test_too_few_toilets_with_flush_telemetry_is_a_risk(self) -> None:
        state = base_state()
        state["duplicants"]["total"] = 9
        state["buildings"]["flush_toilet"] = 1
        state["quality"]["buildings.flush_toilet"] = observed("buildings.flush_toilet")
        result = sanitation(state)
        self.assertEqual(result["status"], "at_risk")
        self.assertIn("1 outhouse(s) and 1 flush toilet(s)", result["reason"])

    def test_live_shape_counts_trusted_functional_outhouses(self) -> None:
        state = functional(live_counts(base_state()), [("Outhouse", True)] * 7)
        self.assertEqual(sanitation(state)["status"], "satisfied")

    def test_functional_flush_toilets_count(self) -> None:
        state = functional(live_counts(base_state()), [("FlushToilet", True)])
        self.assertEqual(sanitation(state)["status"], "satisfied")
        broken = functional(live_counts(base_state()), [("FlushToilet", False), ("Outhouse", True)])
        self.assertEqual(sanitation(broken)["status"], "at_risk")

    def test_no_toilet_is_a_risk_only_when_flush_toilets_are_tracked(self) -> None:
        untracked = functional(live_counts(base_state()), [])
        self.assertEqual(sanitation(untracked)["status"], "unknown")
        tracked = functional(
            live_counts(base_state()),
            [],
            prefab_ids=["FlushToilet", "ManualGenerator", "MineralDeoxidizer", "Outhouse", "OxygenDiffuser"],
        )
        self.assertEqual(sanitation(tracked)["status"], "at_risk")

    def test_invalid_prefab_declaration_fails_closed(self) -> None:
        for declared in (["Outhouse", "FlushToilet"], ["Toilet"], [], "FlushToilet", [["Outhouse"]]):
            with self.subTest(declared=declared):
                state = functional(live_counts(base_state()), [("Outhouse", True)], prefab_ids=declared)
                self.assertEqual(sanitation(state)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
