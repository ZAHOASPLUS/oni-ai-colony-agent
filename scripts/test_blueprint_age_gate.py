"""Regression (B3): a fresh placement is neither "stuck" nor cancellable.

Phase 2 cancelled an Electrolyzer 66 s after placing it, because any placement
no duplicant had started yet counted as stuck; the six other Electrolyzers were
built 1.5-4.5 minutes after placement (docs/b2-blueprint-lifecycle-20261027.md).
The stuck logic now needs ``STUCK_BLUEPRINT_MIN_AGE_CYCLES`` of game time, which
the model loop measures from the first snapshot that showed the placement.

Self-contained fixtures; no live bridge state is read.
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
from agent.model_loop import ModelLoop  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from test_building_status import phase2_state  # noqa: E402

KEY = "id_16394"


def with_placement(state=None, *, key=KEY, prefab="Electrolyzer", cell=147028):
    state = dict(state or phase2_state())
    state["blueprints"] = {
        "by_instance_id": {
            key: {
                "prefab_id": prefab, "cell": cell, "in_progress": False,
                "progress_percent": 0, "worker_instance_id": 0,
                "material_lineage": [{"tag": "Cinnabar", "delivered_mass_kg": 0, "required_mass_kg": 200}],
            }
        }
    }
    return state


def fake_cancel_hint(state, cells):
    return {"action": "cancel_building_blueprint", "args": {"cells": list(cells)}}


class CandidateGateTests(unittest.TestCase):
    def test_a_young_placement_is_not_stuck(self):
        state = with_placement()
        self.assertEqual(C._stuck_blueprint_count(state, {KEY: 0.1}), 0)
        self.assertEqual(C._stuck_blueprint_count(state, {}), 0)
        self.assertEqual(C._stuck_blueprint_count(state, {KEY: C.STUCK_BLUEPRINT_MIN_AGE_CYCLES}), 1)

    def test_untracked_ages_keep_the_old_behaviour(self):
        self.assertEqual(C._stuck_blueprint_count(with_placement(), None), 1)

    def test_bad_ages_count_as_young(self):
        state = with_placement()
        for age in (float("nan"), float("inf"), True, "2"):
            self.assertEqual(C._stuck_blueprint_count(state, {KEY: age}), 0, age)

    def test_the_cancel_waits_for_the_age(self):
        state = with_placement()
        with mock.patch.object(C, "build_cancel_building_blueprint_hint", side_effect=fake_cancel_hint):
            self.assertIsNone(C._cancel_blueprint_candidate(state, {KEY: 0.1}))
            self.assertIsNotNone(C._cancel_blueprint_candidate(state, {KEY: 1.5}))

    def test_generate_candidates_holds_new_placements_behind_a_young_one(self):
        offered = []

        def fake_building(snapshot, building_def_id, prefix, label, note=""):
            offered.append(building_def_id)
            return None

        allowed = ("place_building_blueprint", "cancel_building_blueprint")
        with mock.patch.object(C, "_building_candidate", side_effect=fake_building), \
                mock.patch.object(C, "build_cancel_building_blueprint_hint", side_effect=fake_cancel_hint):
            young = C.generate_candidates(with_placement(), allowed_actions=allowed, blueprint_ages={KEY: 0.2})
            self.assertEqual(offered, [])
            self.assertFalse(any(i["candidate_id"].startswith("cancel_blueprint_") for i in young["items"]))
            old = C.generate_candidates(with_placement(), allowed_actions=allowed, blueprint_ages={KEY: 2.0})
            self.assertTrue(any(i["candidate_id"].startswith("cancel_blueprint_") for i in old["items"]))
            self.assertIn("stuck_placement_ignored:1", old["notes"])


def snapshot(game_time, keys, *, session="session-a", observed=True):
    return {
        "session_id": session,
        "game_time_cycles": game_time,
        "quality": {"game_time_cycles": {"status": "observed" if observed else "estimated"}},
        "blueprints": {"by_instance_id": {key: {"prefab_id": "Electrolyzer"} for key in keys}},
    }


class AgeTrackingTests(unittest.TestCase):
    def test_ages_follow_game_time_from_first_sight(self):
        loop = ModelLoop()
        self.assertEqual(loop._blueprint_ages(snapshot(6.4, ["a"])), {"a": 0.0})
        self.assertAlmostEqual(loop._blueprint_ages(snapshot(7.0, ["a"]))["a"], 0.6)
        ages = loop._blueprint_ages(snapshot(7.5, ["a", "b"]))
        self.assertAlmostEqual(ages["a"], 1.1)
        self.assertEqual(ages["b"], 0.0)

    def test_a_vanished_placement_is_forgotten(self):
        loop = ModelLoop()
        loop._blueprint_ages(snapshot(6.0, ["a"]))
        loop._blueprint_ages(snapshot(8.0, []))
        self.assertEqual(loop._blueprint_ages(snapshot(8.5, ["a"])), {"a": 0.0})

    def test_a_new_session_restarts_every_clock(self):
        loop = ModelLoop()
        loop._blueprint_ages(snapshot(6.0, ["a"]))
        self.assertEqual(loop._blueprint_ages(snapshot(9.0, ["a"], session="session-b")), {"a": 0.0})

    def test_unobserved_game_time_never_ages_a_placement(self):
        loop = ModelLoop()
        loop._blueprint_ages(snapshot(6.0, ["a"], observed=False))
        self.assertEqual(loop._blueprint_ages(snapshot(9.0, ["a"], observed=False)), {"a": 0.0})
        # The clock starts at the first observed time, not at an unobserved one.
        self.assertEqual(loop._blueprint_ages(snapshot(9.0, ["a"])), {"a": 0.0})
        self.assertAlmostEqual(loop._blueprint_ages(snapshot(10.25, ["a"]))["a"], 1.25)

    def test_game_time_running_backwards_does_not_go_negative(self):
        loop = ModelLoop()
        loop._blueprint_ages(snapshot(9.0, ["a"]))
        self.assertEqual(loop._blueprint_ages(snapshot(6.0, ["a"])), {"a": 0.0})


if __name__ == "__main__":
    unittest.main()
