"""Regression: which canonical alerts may block resuming the game.

Resuming was refused whenever any canonical alert was open, which deadlocked the
autonomous loop: a duplicant standing in a bad cell raised ``low_oxygen``, the
alert kept the game paused, the paused game never let the duplicant walk away,
and every resume was refused (2026-10-25 rounds 9-11).

Onset alerts (``low_oxygen``, ``urgent_toilet``, ``exhaustion``) are needs that
are just starting to be unmet; they clear by themselves once the game runs and
the duplicant walks to the toilet or into breathable air.  Genuine emergencies
must still refuse the resume.

The fixture is derived from a real recorded state so every field-quality
contract the gate validates stays intact; only the alert payload is varied.
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
from agent.field_contracts import CANONICAL_ALERT_IDS, ONSET_ALERT_IDS  # noqa: E402
from scripts.recorded_state import load_recorded_state  # noqa: E402



def load_base_state() -> dict:
    """A real paused state, or a minimal one when no run is available."""

    base = load_recorded_state()
    if base:
        base.setdefault("game", {})["paused"] = True
    return base


def state_with_alerts(base: dict, alerts: list[str]) -> dict:
    state = copy.deepcopy(base)
    state["alerts"] = list(alerts)
    state["alerts_by_world"] = [
        {"alerts": list(alerts), "duplicant_count": 4, "world_id": 0}
    ]
    return state


def resume_offered(base: dict, alerts: list[str]) -> bool:
    candidate = C._resume_candidate(state_with_alerts(base, alerts))
    return candidate is not None and candidate["candidate_id"] == "resume_game"


class OnsetAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = load_base_state()
        if not self.base:
            self.skipTest("no recorded live state available to derive the fixture from")
        observed = C._observed(self.base, "alerts", C.ALERTS_CANONICAL_CONTRACT_ID)
        if observed is None:
            self.skipTest("the recorded state does not observe the alerts contract")

    def test_the_onset_set_is_a_subset_of_the_canonical_catalog(self):
        self.assertTrue(set(ONSET_ALERT_IDS) <= set(CANONICAL_ALERT_IDS))

    def test_an_empty_alert_set_may_resume(self):
        self.assertTrue(resume_offered(self.base, []))

    def test_each_onset_alert_alone_may_resume(self):
        for alert in ONSET_ALERT_IDS:
            with self.subTest(alert=alert):
                self.assertTrue(resume_offered(self.base, [alert]))

    def test_every_onset_alert_combined_may_resume(self):
        self.assertTrue(resume_offered(self.base, list(ONSET_ALERT_IDS)))

    def test_a_real_emergency_refuses_the_resume(self):
        emergencies = [a for a in CANONICAL_ALERT_IDS if a not in ONSET_ALERT_IDS]
        self.assertTrue(emergencies, "the catalog must contain emergencies")
        for alert in emergencies:
            with self.subTest(alert=alert):
                self.assertFalse(resume_offered(self.base, [alert]))

    def test_an_emergency_mixed_with_an_onset_alert_refuses_the_resume(self):
        for alert in CANONICAL_ALERT_IDS:
            if alert in ONSET_ALERT_IDS:
                continue
            with self.subTest(alert=alert):
                self.assertFalse(resume_offered(self.base, ["low_oxygen", alert]))

    def test_the_precondition_binds_the_exact_observed_alert_set(self):
        alerts = ["low_oxygen", "urgent_toilet"]
        candidate = C._resume_candidate(state_with_alerts(self.base, alerts))
        self.assertIsNotNone(candidate)
        conditions = [
            c for c in candidate["hint"]["preconditions"] if c.get("path") == "alerts"
        ]
        self.assertEqual(len(conditions), 1)
        self.assertEqual(sorted(conditions[0]["value"]), sorted(alerts))

    def test_an_unobserved_alert_set_refuses_the_resume(self):
        state = state_with_alerts(self.base, [])
        state["quality"]["alerts"]["status"] = "unsupported"
        self.assertIsNone(C._resume_candidate(state))


if __name__ == "__main__":
    unittest.main()
