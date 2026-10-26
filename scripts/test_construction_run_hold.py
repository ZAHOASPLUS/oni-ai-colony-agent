"""Regression: the bounded hold that lets queued construction finish.

Blueprint actions — place and cancel alike — require a paused game, so the pause
is what the model reaches for whenever it wants to change anything.  Left alone it
paused as soon as it could, and the simulation advanced 0.2 cycles in nine minutes
with no queued blueprint ever completed (2026-10-25 round 20).

While a placement is queued and the game is running, the loop therefore withholds
``pause_game`` for ``CONSTRUCTION_RUN_HOLD_SECONDS``.  The hold is deliberately
bounded: a placement that can never be built must stay cancellable, and the cancel
is itself a paused-only action, so an unbounded hold would deadlock the colony.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.candidates import generate_candidates  # noqa: E402
from agent.model_loop import CONSTRUCTION_RUN_HOLD_SECONDS, ModelLoop  # noqa: E402
from scripts.recorded_state import load_recorded_state  # noqa: E402


ALLOWED = ["set_paused", "set_research", "place_building_blueprint", "cancel_building_blueprint"]
PLACED = {"id_1": {"prefab_id": "MineralDeoxidizer", "cell": 139386, "in_progress": False}}
UNDERWAY = {"id_1": {"prefab_id": "MineralDeoxidizer", "cell": 139386, "in_progress": True}}
# ``functional_buildings`` is part of the fixture too: a built building the game
# reports as not operational is also queued work that needs the game running.
IDLE_BUILDING = [{"instance_id": 1, "prefab_id": "MineralDeoxidizer", "operational": False}]
RUNNING_BUILDING = [{"instance_id": 1, "prefab_id": "MineralDeoxidizer", "operational": True}]


def load_base_state() -> dict:
    """A recorded state, so the ``game.paused`` contract is really observed."""

    return load_recorded_state()


def state(base: dict, *, paused: bool, blueprints: dict, buildings: list) -> dict:
    document = copy.deepcopy(base)
    document.setdefault("game", {})["paused"] = paused
    document.setdefault("blueprints", {})["by_instance_id"] = blueprints
    document.setdefault("functional_buildings", {})["instances"] = buildings
    return document


class ConstructionRunHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = load_base_state()
        if not self.base:
            self.skipTest('no recorded live state available to derive the fixture from')
        # Autonomous mode requires a bound controller identity and startup
        # anchors; the hold itself is independent of that mode.
        self.loop = ModelLoop()
        self.clock = [1000.0]
        self.loop._monotonic = lambda: self.clock[0]

    def step(self, latest: dict, at: float) -> dict:
        self.clock[0] = at
        candidate_set = generate_candidates(latest, allowed_actions=ALLOWED)
        self.loop._hold_construction_run(latest, candidate_set, at)
        return candidate_set

    def state(self, *, paused: bool, blueprints: dict, buildings: list = None) -> dict:
        return state(self.base, paused=paused, blueprints=blueprints,
                     buildings=RUNNING_BUILDING if buildings is None else buildings)

    def ids(self, candidate_set: dict) -> list[str]:
        return [item.get("candidate_id") for item in candidate_set["items"]]

    def test_the_pause_is_withheld_while_construction_is_queued(self):
        candidate_set = self.step(self.state(paused=False, blueprints=PLACED), 1000.0)
        self.assertNotIn("pause_game", self.ids(candidate_set))
        self.assertIn("construction_run_hold_remaining_seconds", candidate_set)

    def test_the_hold_expires_so_a_stuck_placement_stays_cancellable(self):
        self.step(self.state(paused=False, blueprints=PLACED), 1000.0)
        candidate_set = self.step(
            self.state(paused=False, blueprints=PLACED), 1000.0 + CONSTRUCTION_RUN_HOLD_SECONDS + 1
        )
        self.assertIn("pause_game", self.ids(candidate_set))
        self.assertTrue(candidate_set.get("construction_run_hold_expired"))

    def test_the_hold_restarts_after_the_game_is_paused(self):
        self.step(self.state(paused=False, blueprints=PLACED), 1000.0)
        # Paused: the streak is cleared and the hold does not touch the set (a
        # paused game never offers pause_game anyway).
        candidate_set = self.step(self.state(paused=True, blueprints=PLACED), 1100.0)
        self.assertNotIn("construction_run_hold_remaining_seconds", candidate_set)
        self.assertIsNone(self.loop._construction_run_started)
        # Running again: the hold starts over rather than resuming the old window.
        candidate_set = self.step(self.state(paused=False, blueprints=PLACED), 1200.0)
        self.assertNotIn("pause_game", self.ids(candidate_set))
        self.assertEqual(self.loop._construction_run_started, 1200.0)

    def test_no_queued_placement_leaves_the_pause_alone(self):
        candidate_set = self.step(self.state(paused=False, blueprints={}), 1000.0)
        self.assertIn("pause_game", self.ids(candidate_set))
        self.assertIsNone(self.loop._construction_run_started)

    def test_active_construction_is_not_counted_as_queued(self):
        # An in-progress placement is not "queued work"; the candidates module
        # withholds the pause for that case through _active_construction_count.
        candidate_set = self.step(self.state(paused=False, blueprints=UNDERWAY), 1000.0)
        self.assertIsNone(self.loop._construction_run_started)

    def test_the_hold_is_bounded_not_permanent(self):
        self.assertGreater(CONSTRUCTION_RUN_HOLD_SECONDS, 0.0)
        self.assertLessEqual(CONSTRUCTION_RUN_HOLD_SECONDS, 600.0)

    def test_a_non_operational_building_also_holds_the_pause(self):
        # A built building the game reports as not operational is waiting on a
        # duplicant to supply or operate it, which needs the game running.
        candidate_set = self.step(
            self.state(paused=False, blueprints={}, buildings=IDLE_BUILDING), 1000.0
        )
        self.assertNotIn("pause_game", self.ids(candidate_set))
        self.assertEqual(candidate_set.get("construction_run_waiting"), 1)

    def test_the_hold_always_expires_so_the_loop_can_still_act(self):
        # Round 41 made the not-operational case unbounded.  The running candidate
        # set then collapsed to ``wait``, so the loop could neither cancel a stuck
        # placement nor place the colony's food and oxygen buildings; the hold must
        # stay bounded for both kinds of queued work (2026-10-26 round 43).
        snapshot = self.state(paused=False, blueprints={}, buildings=IDLE_BUILDING)
        early = self.step(snapshot, 0.0)
        self.assertNotIn("pause_game", self.ids(early))
        late = self.step(snapshot, 10000.0)
        self.assertIn("pause_game", self.ids(late))
        self.assertIs(late.get("construction_run_hold_expired"), True)

    def test_an_operational_building_does_not_hold_the_pause(self):
        candidate_set = self.step(
            self.state(paused=False, blueprints={}, buildings=RUNNING_BUILDING), 1000.0
        )
        self.assertIn("pause_game", self.ids(candidate_set))

    def test_the_waiting_count_sums_placements_and_idle_buildings(self):
        candidate_set = self.step(
            self.state(paused=False, blueprints=PLACED, buildings=IDLE_BUILDING), 1000.0
        )
        self.assertEqual(candidate_set.get("construction_run_waiting"), 2)


if __name__ == "__main__":
    unittest.main()
