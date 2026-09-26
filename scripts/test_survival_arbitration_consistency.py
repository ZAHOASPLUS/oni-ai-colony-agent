"""Regression: survival arbitration stays live and consistent (review 2026-09-26, L8/L9).

L8: the Bridge skips incident updates for untrusted snapshots, but the tracker
kept counting the episode, so the witness failed the arbiter's
``last - first + 1 == consecutive`` check until the alert cleared, blocking
even a new suffocation pause.

L9: the frame certificate passed every incident record to the reducer while
advice and candidate admission passed only open ones.  An active alert still
in hysteresis (pending/cooldown) therefore blocked the certificate and the
pause selected by advice was rejected at approval.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import rules  # noqa: E402
from agent.incidents import IncidentTracker  # noqa: E402
from scripts.verify_survival_certificate import (  # noqa: E402
    make_state,
    projected_incident,
    source_for,
    witness,
)

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
ALERTS = ["low_oxygen", "suffocation"]


def state_at(snapshot_seq: int) -> dict:
    return make_state(
        ALERTS,
        seq=snapshot_seq,
        snapshot_seq=snapshot_seq,
        timestamp=(BASE + dt.timedelta(seconds=5 * snapshot_seq)).isoformat(),
    )


def open_records(tracker: IncidentTracker) -> list:
    return [dict(item) for item in tracker.incidents.values() if item.get("status") == "open"]


class SkippedSnapshotTests(unittest.TestCase):
    def test_skipped_snapshot_restarts_episode_instead_of_poisoning_it(self):
        tracker = IncidentTracker()
        for seq in (1, 2, 3):
            state = state_at(seq)
            tracker.update(state, rules.evaluate_signals(state))
        # Snapshot 4 was untrusted: the Bridge never called update() for it.
        state = state_at(5)
        tracker.update(state, rules.evaluate_signals(state))
        suffocation = tracker.incidents["suffocation"]
        self.assertEqual(suffocation["first_snapshot_seq"], 5)
        self.assertEqual(suffocation["consecutive_samples"], 1)
        result = rules.arbitrate_survival_emergency(state, open_records(tracker))
        self.assertEqual(result["status"], "selected", result["blockers"])
        self.assertEqual(result["selected_incident_id"], "suffocation")

        for seq in range(6, 13):  # low oxygen re-accumulates 7 samples / 30 s
            state = state_at(seq)
            tracker.update(state, rules.evaluate_signals(state))
        low_oxygen = tracker.incidents["low_oxygen"]
        self.assertEqual(low_oxygen["status"], "open")
        self.assertEqual(
            low_oxygen["last_snapshot_seq"] - low_oxygen["first_snapshot_seq"] + 1,
            low_oxygen["consecutive_samples"],
        )
        result = rules.arbitrate_survival_emergency(state, open_records(tracker))
        self.assertEqual((result["status"], result["blockers"]), ("selected", []))


class HysteresisConsistencyTests(unittest.TestCase):
    def assert_paths_agree(self, records: list, expected_status: str, expected_selected=None):
        state = make_state(ALERTS)
        certificate = rules.build_survival_arbitration_certificate(state, records)
        advice_view = rules.arbitrate_survival_emergency(
            state, [item for item in records if item["status"] == "open"]
        )
        self.assertEqual(certificate["status"], expected_status, certificate["blockers"])
        self.assertEqual(certificate["selected_incident_id"], expected_selected)
        self.assertEqual(advice_view["status"], expected_status)
        self.assertEqual(advice_view.get("selected_incident_id"), expected_selected)
        blockers = rules.survival_certificate_blockers(
            certificate,
            source_for(state, include_snapshot_seq=True),
            [projected_incident(item) for item in records],
        )
        self.assertEqual(blockers, [])
        return certificate

    def test_pending_lower_priority_alert_does_not_block_suffocation(self):
        state = make_state(ALERTS)
        records = [
            witness(state, "suffocation"),
            witness(state, "low_oxygen", status="pending", samples=3, duration=10.0),
        ]
        self.assert_paths_agree(records, "selected", "suffocation")

    def test_cooldown_lower_priority_alert_does_not_block_suffocation(self):
        state = make_state(ALERTS)
        records = [
            witness(state, "suffocation"),
            witness(state, "low_oxygen", status="cooldown"),
        ]
        self.assert_paths_agree(records, "selected", "suffocation")

    def test_lone_pending_alert_is_pending_hysteresis(self):
        state = make_state(["low_oxygen"])
        record = witness(state, "low_oxygen", status="pending", samples=3, duration=10.0)
        certificate = rules.build_survival_arbitration_certificate(state, [record])
        self.assertEqual((certificate["status"], certificate["blockers"]), ("none", ["pending_hysteresis"]))

    def test_contradictory_resolved_active_witness_still_blocks(self):
        state = make_state(ALERTS)
        records = [
            witness(state, "suffocation"),
            witness(state, "low_oxygen", status="resolved"),
        ]
        certificate = rules.build_survival_arbitration_certificate(state, records)
        self.assertEqual(certificate["status"], "blocked")
        self.assertIn("open_incident_status_invalid", certificate["blockers"])


if __name__ == "__main__":
    unittest.main()
