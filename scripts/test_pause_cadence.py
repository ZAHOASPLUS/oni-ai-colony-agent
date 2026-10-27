"""Roadmap B7: pause and resume are rule decisions in autonomous mode.

Phase 2 published 32 commands, 19 of them ``set_paused`` (59 %); each one cost
a model call and a command round trip.  ``ModelLoop._pause_cadence`` takes
``pause_game`` and ``resume_game`` away from the model and decides them itself,
through the same publication and authorization path as a model choice.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import autonomous_authorization  # noqa: E402
from agent import model_loop as M  # noqa: E402
from agent.candidates import generate_candidates  # noqa: E402
from scripts.recorded_state import load_recorded_policy, load_recorded_state  # noqa: E402

IDENTITY = {
    "run_id": "r", "controller_id": "c", "session_id": "s", "world_id": 0,
    "save_id_hash": "sha256:" + "0" * 64, "startup_expectations": {},
}


class PauseCadenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = load_recorded_state()
        self.policy = load_recorded_policy()
        if not self.base or not self.policy:
            self.skipTest("no recorded live state available to derive the fixture from")
        self.loop = M.ModelLoop(autonomous=True, autonomous_identity=IDENTITY)

    def state(self, *, paused: bool, cycles: float = 7.0, alerts=None, blueprints=None) -> dict:
        # The recorded alerts are onset alerts only, which allow a resume.
        state = copy.deepcopy(self.base)
        state["game"]["paused"] = paused
        state["game_time_cycles"] = cycles
        if alerts is not None:
            state["alerts"] = list(alerts)
            state["alerts_by_world"][0]["alerts"] = list(alerts)
        if blueprints is not None:
            state["blueprints"]["by_instance_id"] = blueprints
        return state

    def cadence(self, state: dict):
        candidate_set = generate_candidates(state, allowed_actions=self.policy["allowed_actions"], autonomous=True)
        decision = self.loop._pause_cadence(state, candidate_set, 0.0)
        return candidate_set, decision

    @staticmethod
    def ids(items) -> list:
        return [item["candidate_id"] for item in items]

    # -- running -------------------------------------------------------------

    def test_running_hides_the_pause_until_the_window_has_run(self) -> None:
        candidate_set, decision = self.cadence(self.state(paused=False, cycles=7.0))
        self.assertIsNone(decision)
        self.assertNotIn("pause_game", self.ids(candidate_set["items"]))
        self.assertIn("pause_game", self.ids(candidate_set["catalog_items"]))
        _, decision = self.cadence(self.state(paused=False, cycles=7.0 + M.RUN_WINDOW_CYCLES / 2))
        self.assertIsNone(decision)
        _, decision = self.cadence(self.state(paused=False, cycles=7.0 + M.RUN_WINDOW_CYCLES))
        self.assertEqual(decision["candidate"]["candidate_id"], "pause_game")
        self.assertEqual(decision["model"], M.PAUSE_CADENCE_RULE)

    def test_running_without_game_time_leaves_the_pause_to_the_model(self) -> None:
        state = self.state(paused=False)
        del state["game_time_cycles"]
        candidate_set, decision = self.cadence(state)
        self.assertIsNone(decision)
        self.assertIn("pause_game", self.ids(candidate_set["items"]))

    def test_active_construction_still_withholds_the_pause(self) -> None:
        underway = {"id_1": {"prefab_id": "MineralDeoxidizer", "in_progress": True, "progress_percent": 40}}
        self.cadence(self.state(paused=False, cycles=7.0, blueprints=underway))
        _, decision = self.cadence(self.state(paused=False, cycles=8.0, blueprints=underway))
        self.assertIsNone(decision)

    # -- paused --------------------------------------------------------------

    def test_paused_hides_the_resume_and_tells_the_model_what_wait_means(self) -> None:
        candidate_set, decision = self.cadence(self.state(paused=True))
        self.assertIsNone(decision)
        self.assertNotIn("resume_game", self.ids(candidate_set["items"]))
        self.assertIn("resume_game", self.ids(candidate_set["catalog_items"]))
        self.assertIn("pause_cadence:choosing_wait_resumes_the_game", candidate_set["notes"])

    def test_a_wait_answer_while_paused_resumes(self) -> None:
        self.cadence(self.state(paused=True))
        request = M._Request(
            request_id="req-1", candidate_set_hash="h", candidates={}, prompt_hash="p", model="m",
            submitted_at="t", started=0.0, deadline=1.0, paused=True,
        )
        self.loop._record_result(request, {"status": "ok", "choice": {"candidate_id": "wait", "reason": "done"}}, 0.0)
        _, decision = self.cadence(self.state(paused=True))
        self.assertEqual(decision["candidate"]["candidate_id"], "resume_game")
        self.assertIn("chose wait", decision["reason"])

    def test_the_paused_action_budget_resumes(self) -> None:
        self.cadence(self.state(paused=True))
        self.loop._paused_actions = M.MAX_PAUSED_ACTIONS
        _, decision = self.cadence(self.state(paused=True))
        self.assertEqual(decision["candidate"]["candidate_id"], "resume_game")

    def test_nothing_left_to_change_resumes_without_asking(self) -> None:
        state = self.state(paused=True)
        candidate_set = generate_candidates(state, allowed_actions=["set_paused"], autonomous=True)
        decision = self.loop._pause_cadence(state, candidate_set, 0.0)
        self.assertEqual(self.ids(candidate_set["items"]), ["wait"])
        self.assertEqual(decision["candidate"]["candidate_id"], "resume_game")

    def test_an_emergency_keeps_the_game_paused(self) -> None:
        self.loop._paused_wait = True
        self.loop._cadence_session = self.base.get("session_id")
        candidate_set, decision = self.cadence(self.state(paused=True, alerts=["suffocation"]))
        self.assertIsNone(decision)
        self.assertNotIn("resume_game", self.ids(candidate_set["catalog_items"]))

    def test_running_resets_the_paused_counters(self) -> None:
        self.cadence(self.state(paused=True))
        self.loop._paused_actions = 2
        self.loop._paused_wait = True
        self.cadence(self.state(paused=False))
        self.assertEqual((self.loop._paused_actions, self.loop._paused_wait), (0, False))

    def test_a_new_session_restarts_the_run_window(self) -> None:
        self.cadence(self.state(paused=False, cycles=7.0))
        state = self.state(paused=False, cycles=7.0 + M.RUN_WINDOW_CYCLES)
        state["session_id"] = "session-other"
        _, decision = self.cadence(state)
        self.assertIsNone(decision)

    def test_human_mode_is_unchanged(self) -> None:
        loop = M.ModelLoop()
        state = self.state(paused=True)
        candidate_set = generate_candidates(state, allowed_actions=self.policy["allowed_actions"], autonomous=True)
        before = self.ids(candidate_set["items"])
        self.assertIsNone(loop._pause_cadence(state, candidate_set, 0.0))
        self.assertEqual(self.ids(candidate_set["items"]), before)

    def test_the_prompt_rules_follow_the_cadence(self) -> None:
        payload = {"candidates": []}
        cadence = M.build_choice_prompt_text(payload, pause_cadence=True)
        model = M.build_choice_prompt_text(payload)
        self.assertIn("The controller pauses and resumes the game itself", cadence)
        self.assertIn(f"up to {M.MAX_PAUSED_ACTIONS} changes per pause", cadence)
        self.assertNotIn("choose pause_game", cadence)
        self.assertIn("choose pause_game", model)
        for text in (cadence, model):
            rules = text.split("\n\n", 1)[0]
            # Instruction, heading, six rules and the end marker; no blank line.
            self.assertEqual(rules.count("\n"), 8)

    # -- provenance ----------------------------------------------------------

    def test_a_rule_decision_passes_the_autonomous_provenance_check(self) -> None:
        state = self.state(paused=True)
        policy = dict(self.policy)
        self.cadence(state)
        self.loop._paused_wait = True
        _, decision = self.cadence(state)
        frame = {"frame_id": "frame-1"}
        command = M.build_model_command(state, frame, policy, decision["candidate"], decision, autonomous=True)
        autonomous_authorization._validate_provenance(command, policy, frame, state)
        self.assertEqual(command["evidence"]["model"], M.PAUSE_CADENCE_RULE)
        self.assertEqual(command["args"], {"paused": False})


if __name__ == "__main__":
    unittest.main()
