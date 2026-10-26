"""Regression: the pause is withheld only during active construction.

The pause must be withheld while a duplicant is actively constructing, because a
paused game builds nothing and the colony stalled with unbuilt blueprints
(2026-10-25 rounds 9-12).

It must NOT be withheld merely because a placement exists.  After a build
completes, the game keeps reporting the placement with ``in_progress`` false and
progress 0, so treating any placement as "keep running" suppressed the pause
forever, left ``wait`` as the only candidate, and stalled the loop for minutes
(round 14: one completed diffuser plus one lingering placement).

Separately, new blueprints are not offered while unbuilt placements are queued,
so the model cannot paper the colony with plans instead of letting it work.
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


PLACED = {"id_1": {"prefab_id": "MineralDeoxidizer", "in_progress": False, "progress_percent": 0}}
UNDERWAY = {"id_1": {"prefab_id": "MineralDeoxidizer", "in_progress": True, "progress_percent": 40}}


def load_base_state() -> dict:
    return load_recorded_state()


def with_state(base: dict, *, paused: bool, blueprints: dict) -> dict:
    state = copy.deepcopy(base)
    state.setdefault("game", {})["paused"] = paused
    state.setdefault("blueprints", {})["by_instance_id"] = blueprints
    return state


def candidate_ids(base: dict, *, paused: bool, blueprints: dict, allowed: list[str]) -> list[str]:
    state = with_state(base, paused=paused, blueprints=blueprints)
    return [item.get("candidate_id") for item in C.generate_candidates(
        state, allowed_actions=allowed, autonomous=True
    )["items"]]


class ConstructionPauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = load_base_state()
        if not self.base:
            self.skipTest("no recorded live state available to derive the fixture from")
        self.allowed = load_recorded_policy().get("allowed_actions") or []
        if not self.allowed:
            self.skipTest("no recorded action policy available")

    def test_counts_distinguish_placed_from_active(self):
        placed = with_state(self.base, paused=False, blueprints=PLACED)
        underway = with_state(self.base, paused=False, blueprints=UNDERWAY)
        empty = with_state(self.base, paused=False, blueprints={})
        self.assertEqual(C._pending_blueprint_count(placed), 1)
        self.assertEqual(C._active_construction_count(placed), 0)
        self.assertEqual(C._pending_blueprint_count(underway), 0)
        self.assertEqual(C._active_construction_count(underway), 1)
        self.assertEqual(C._active_construction_count(empty), 0)

    def test_the_pause_is_withheld_during_active_construction(self):
        ids = candidate_ids(self.base, paused=False, blueprints=UNDERWAY, allowed=self.allowed)
        self.assertNotIn("pause_game", ids)

    def test_the_pause_is_available_when_only_a_placement_lingers(self):
        ids = candidate_ids(self.base, paused=False, blueprints=PLACED, allowed=self.allowed)
        self.assertIn("pause_game", ids)

    def test_the_pause_is_available_with_no_blueprints(self):
        ids = candidate_ids(self.base, paused=False, blueprints={}, allowed=self.allowed)
        self.assertIn("pause_game", ids)

    def test_new_blueprints_are_withheld_while_a_placement_is_under_construction(self):
        # A placement with real progress is being worked on, so piling another one
        # on top would split the duplicants.  A never-started placement is
        # different: it must not suppress the colony's food and oxygen buildings
        # (2026-10-26 round 44).
        partial = {
            key: dict(entry, progress_percent=10, worker_instance_id=7)
            for key, entry in PLACED.items()
        }
        ids = candidate_ids(self.base, paused=True, blueprints=partial, allowed=self.allowed)
        self.assertFalse(
            any(cid.startswith(("oxygen_diffuser", "algae_terrarium", "ladder")) for cid in ids),
            ids,
        )

    def test_a_never_started_placement_no_longer_suppresses_new_blueprints(self):
        ids = candidate_ids(self.base, paused=True, blueprints=PLACED, allowed=self.allowed)
        self.assertTrue(
            any(cid.startswith(("oxygen_diffuser", "algae_terrarium", "ladder", "microbe_musher")) for cid in ids),
            ids,
        )

    def test_the_loop_has_a_non_wait_candidate_when_nothing_is_under_construction(self):
        # With no active construction the pause must stay available, so the loop
        # can still reach the paused-only actions instead of waiting forever.
        for label, blueprints in (("empty", {}), ("placed", PLACED)):
            with self.subTest(blueprints=label):
                ids = candidate_ids(self.base, paused=False, blueprints=blueprints, allowed=self.allowed)
                self.assertTrue([cid for cid in ids if cid != "wait"], ids)

    def test_active_construction_withholds_the_pause_even_if_that_leaves_wait(self):
        # Keeping the game running is the point: a paused game builds nothing.
        # The exact remaining set depends on the recorded state (research may add
        # candidates), so assert the intent rather than a literal list.
        ids = candidate_ids(self.base, paused=False, blueprints=UNDERWAY, allowed=self.allowed)
        self.assertNotIn("pause_game", ids)
        self.assertTrue(all(cid in ("wait",) or str(cid).startswith("research_") for cid in ids), ids)


if __name__ == "__main__":
    unittest.main()
