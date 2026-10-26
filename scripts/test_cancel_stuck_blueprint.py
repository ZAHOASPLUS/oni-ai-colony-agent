"""Regression: a stuck blueprint must be cancellable, not just blocking.

``_pending_blueprint_count`` withholds new placements while any unbuilt blueprint
is queued, so the model cannot paper the colony with plans.  That guard alone
deadlocks the colony when a placement will never be built: there is no
constructive candidate at all, and the loop only toggles the pause while food and
oxygen fall (2026-10-25 round 20: one unbuildable MineralDeoxidizer held the
colony at ``done`` 9 while food dropped 13705 -> 7692 kcal).

The cancel candidate is therefore offered for a placement no duplicant has
started, which lets the model clear it and place one that will be built.  Active
construction is never a cancel target, because interrupting it wastes the work
already done.

The fixture is derived from a real recorded state so every field-quality contract
the candidate builders validate stays intact.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import candidates as C  # noqa: E402
from scripts.recorded_state import load_recorded_policy, load_recorded_state  # noqa: E402


def live_blueprints(base: dict, *, in_progress: bool) -> dict:
    """The recorded placements, with the in-progress flag forced.

    The cancel builder re-reads the placement's own cell fields, so the fixture
    must be the real recorded placement rather than a synthetic one.
    """

    instances = (base.get("blueprints") or {}).get("by_instance_id") or {}
    result = {}
    for key, entry in instances.items():
        if not isinstance(entry, dict):
            continue
        copy_entry = dict(entry)
        copy_entry["in_progress"] = in_progress
        result[key] = copy_entry
    return result


def load_base_state() -> dict:
    return load_recorded_state()


def with_state(base: dict, *, paused: bool, blueprints: dict) -> dict:
    state = copy.deepcopy(base)
    state.setdefault("game", {})["paused"] = paused
    state.setdefault("blueprints", {})["by_instance_id"] = blueprints
    return state


def candidate_ids(base: dict, *, paused: bool, blueprints: dict, allowed: list[str]) -> list[str]:
    state = with_state(base, paused=paused, blueprints=blueprints)
    return [
        item.get("candidate_id")
        for item in C.generate_candidates(state, allowed_actions=allowed, autonomous=True)["items"]
    ]


class StuckBlueprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = load_base_state()
        if not self.base:
            self.skipTest("no recorded live state available to derive the fixture from")
        self.allowed = load_recorded_policy().get("allowed_actions") or []
        if "cancel_building_blueprint" not in self.allowed:
            self.skipTest("the recorded policy does not allow cancel_building_blueprint")
        self.placed = live_blueprints(self.base, in_progress=False)
        self.underway = live_blueprints(self.base, in_progress=True)
        # Pending but with real progress: the case that legitimately suppresses
        # further placements (never started is a different case now).
        self.partial = {
            key: dict(entry, progress_percent=10, worker_instance_id=7)
            for key, entry in self.placed.items()
        }
        if not self.placed:
            self.skipTest("the recorded state holds no placed blueprint to cancel")
        self.cell = sorted(
            int(entry["cell"]) for entry in self.placed.values() if isinstance(entry.get("cell"), int)
        )[0]
        self.cancel_id = f"cancel_blueprint_{self.cell}"

    def test_a_placed_blueprint_offers_a_cancel(self):
        ids = candidate_ids(self.base, paused=True, blueprints=self.placed, allowed=self.allowed)
        self.assertIn(self.cancel_id, ids)

    def test_a_never_started_placement_no_longer_withholds_new_placements(self):
        # ``self.placed`` has progress 0, no worker and nothing delivered: it is
        # provably not being worked on.  While it suppressed every building
        # candidate, the colony's only food and oxygen buildings stayed
        # unofferable for many cycles (2026-10-26 round 44).
        ids = candidate_ids(self.base, paused=True, blueprints=self.placed, allowed=self.allowed)
        self.assertTrue(
            any(cid.startswith(("oxygen_diffuser", "algae_terrarium", "ladder", "microbe_musher")) for cid in ids),
            ids,
        )
        # ... and the cancel is still offered, ahead of the placements.
        self.assertIn(self.cancel_id, ids)

    def test_an_active_placement_still_withholds_new_placements(self):
        # A placement with real progress is being worked on, so piling another one
        # on top would split the duplicants.  Only a never-started placement is
        # ignored (2026-10-26 round 44).
        ids = candidate_ids(self.base, paused=True, blueprints=self.partial, allowed=self.allowed)
        self.assertFalse(
            any(cid.startswith(("oxygen_diffuser", "algae_terrarium", "ladder", "microbe_musher")) for cid in ids),
            ids,
        )

    def test_active_construction_is_never_a_cancel_target(self):
        ids = candidate_ids(self.base, paused=True, blueprints=self.underway, allowed=self.allowed)
        self.assertFalse(any(cid.startswith("cancel_blueprint_") for cid in ids), ids)

    def test_no_blueprints_offers_placements_and_no_cancel(self):
        ids = candidate_ids(self.base, paused=True, blueprints={}, allowed=self.allowed)
        self.assertFalse(any(cid.startswith("cancel_blueprint_") for cid in ids), ids)
        self.assertTrue(
            any(cid.startswith(("oxygen_diffuser", "algae_terrarium", "ladder")) for cid in ids),
            ids,
        )

    def test_the_cancel_binds_the_exact_placement(self):
        state = with_state(self.base, paused=True, blueprints=self.placed)
        candidate = C._cancel_blueprint_candidate(state)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["hint"]["action"], "cancel_building_blueprint")
        targets = candidate["hint"]["args"]["targets"]
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["cell"], self.cell)

    def test_the_loop_always_has_a_constructive_candidate_when_paused(self):
        ids = candidate_ids(self.base, paused=True, blueprints=self.placed, allowed=self.allowed)
        self.assertTrue([cid for cid in ids if cid != "wait"], ids)


class CancelSummaryDescribesTheTarget(unittest.TestCase):
    """The summary names the cancelled placement, not the last entry scanned.

    The builder once passed the scan loop's leftover ``entry`` to the summary, so
    with two placements the model could read one placement's progress and
    delivery next to another placement's cell.
    """

    def test_summary_uses_the_chosen_placement(self):
        snapshot = {
            "blueprints": {
                "by_instance_id": {
                    # Lowest cell: the one the builder picks.
                    "id_1": {
                        "prefab_id": "Electrolyzer", "cell": 100, "in_progress": False,
                        "progress_percent": 0,
                        "material_lineage": [
                            {"tag": "Cinnabar", "delivered_mass_kg": 0, "required_mass_kg": 200},
                        ],
                    },
                    # Scanned last; its delivery must not leak into the summary.
                    "id_2": {
                        "prefab_id": "MineralDeoxidizer", "cell": 900, "in_progress": False,
                        "progress_percent": 0,
                        "material_lineage": [
                            {"tag": "Dirt", "delivered_mass_kg": 37, "required_mass_kg": 100},
                        ],
                    },
                }
            }
        }
        original = C.build_cancel_building_blueprint_hint
        C.build_cancel_building_blueprint_hint = lambda state, cells: {
            "action": "cancel_building_blueprint", "args": {"cells": list(cells)},
        }
        try:
            candidate = C._cancel_blueprint_candidate(snapshot)
        finally:
            C.build_cancel_building_blueprint_hint = original
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["candidate_id"], "cancel_blueprint_100")
        summary = candidate["summary"]
        self.assertIn("Electrolyzer", summary)
        self.assertIn("0 of 200 kg Cinnabar", summary)
        self.assertNotIn("Dirt", summary)


if __name__ == "__main__":
    unittest.main()
