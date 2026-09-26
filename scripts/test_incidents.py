#!/usr/bin/env python3
"""Focused input-boundary and state-semantics tests for IncidentTracker."""

from __future__ import annotations

import copy
import datetime as dt
import math
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.incidents import IncidentTracker, IncidentValidationError  # noqa: E402


BASE_TIME = dt.datetime(2026, 9, 13, 0, 0, 0, tzinfo=dt.timezone.utc)


def snapshot(*, session_id: str = "session-a", seq: int = 1, seconds: float = 0.0) -> dict:
    timestamp = (BASE_TIME + dt.timedelta(seconds=seconds)).isoformat()
    return {
        "session_id": session_id,
        "seq": seq,
        "snapshot_seq": seq,
        "snapshot_id": f"{session_id}:{seq}",
        "ts": timestamp,
    }


def signal(incident_id: object = "oxygen", state: str = "active", **overrides: object) -> dict:
    value = {
        "incident_id": incident_id,
        "state": state,
        "title": "Oxygen",
        "risk": "high",
        "reason": "test",
        "action": "observe",
        "evidence": ["test"],
        "command": None,
    }
    value.update(overrides)
    return value


class IncidentTrackerInputBoundaryTests(unittest.TestCase):
    def test_valid_hysteresis_and_resolution_semantics_remain_unchanged(self) -> None:
        tracker = IncidentTracker()
        tracker.update(
            snapshot(seq=1),
            [signal(required_consecutive_samples=2, min_duration_seconds="0", cooldown_seconds=3)],
        )
        self.assertEqual(tracker.incidents["oxygen"]["status"], "pending")
        self.assertEqual(tracker.incidents["oxygen"]["consecutive_samples"], 1)

        tracker.update(
            snapshot(seq=2, seconds=1),
            [signal(required_consecutive_samples=2, min_duration_seconds=0.0, cooldown_seconds=3.0)],
        )
        self.assertEqual(tracker.incidents["oxygen"]["status"], "open")
        self.assertEqual([event["event"] for event in tracker.timeline], ["incident_opened"])

        tracker.update(snapshot(seq=3, seconds=2), [signal(state="inactive", cooldown_seconds=3)])
        incident = tracker.incidents["oxygen"]
        self.assertEqual(incident["status"], "resolved")
        self.assertIsNotNone(incident["resolved_at"])
        self.assertEqual([event["event"] for event in tracker.timeline], [
            "incident_opened",
            "incident_resolved",
        ])

    def test_valid_zero_values_keep_historical_normalization(self) -> None:
        tracker = IncidentTracker()
        tracker.update(
            snapshot(),
            [signal(required_consecutive_samples=0, min_duration_seconds=0, cooldown_seconds=0)],
        )
        self.assertEqual(tracker.incidents["oxygen"]["status"], "open")

    def test_malformed_batch_cannot_partially_update_or_reset_tracker(self) -> None:
        tracker = IncidentTracker()
        tracker.update(snapshot(), [signal()])
        before_session = tracker.session_id
        before_incidents = copy.deepcopy(tracker.incidents)
        before_timeline = copy.deepcopy(tracker.timeline)

        malformed = signal("later", required_consecutive_samples="not-a-number")
        with self.assertRaisesRegex(IncidentValidationError, "required_consecutive_samples"):
            tracker.update(snapshot(session_id="new-session", seq=2), [signal("next"), malformed])

        self.assertEqual(tracker.session_id, before_session)
        self.assertEqual(tracker.incidents, before_incidents)
        self.assertEqual(tracker.timeline, before_timeline)

    def test_non_mapping_signal_inputs_are_rejected_without_mutation(self) -> None:
        tracker = IncidentTracker()
        before = (tracker.session_id, copy.deepcopy(tracker.incidents), copy.deepcopy(tracker.timeline))
        for value in (None, [], "signal", 7):
            with self.subTest(value=value):
                with self.assertRaisesRegex(IncidentValidationError, "mapping"):
                    tracker.update(snapshot(), [value])
                self.assertEqual(tracker.session_id, before[0])
                self.assertEqual(tracker.incidents, before[1])
                self.assertEqual(tracker.timeline, before[2])

        with self.assertRaisesRegex(IncidentValidationError, "iterable of mappings"):
            tracker.update(snapshot(), {"incident_id": "oxygen"})
        self.assertEqual((tracker.session_id, tracker.incidents, tracker.timeline), before)

    def test_incident_id_must_be_a_non_empty_string(self) -> None:
        tracker = IncidentTracker()
        for bad_id in (None, "", "   ", 1, False, object()):
            with self.subTest(bad_id=repr(bad_id)):
                with self.assertRaisesRegex(IncidentValidationError, "incident_id"):
                    tracker.update(snapshot(), [signal(bad_id)])
                self.assertEqual(tracker.incidents, {})
                self.assertEqual(tracker.timeline, [])

    def test_numeric_policy_values_reject_bool_non_finite_unconvertible_and_negative(self) -> None:
        invalid_values = (True, False, math.nan, math.inf, -math.inf, "nan", "inf", "bad", -1)
        for key in (
            "required_consecutive_samples",
            "min_duration_seconds",
            "cooldown_seconds",
        ):
            for value in invalid_values:
                with self.subTest(key=key, value=repr(value)):
                    tracker = IncidentTracker()
                    with self.assertRaisesRegex(IncidentValidationError, key):
                        tracker.update(snapshot(), [signal(**{key: value})])
                    self.assertEqual(tracker.incidents, {})
                    self.assertEqual(tracker.timeline, [])

    def test_required_samples_rejects_fractional_values(self) -> None:
        tracker = IncidentTracker()
        with self.assertRaisesRegex(IncidentValidationError, "required_consecutive_samples"):
            tracker.update(snapshot(), [signal(required_consecutive_samples=1.5)])
        self.assertEqual(tracker.incidents, {})
        self.assertEqual(tracker.timeline, [])

    def test_direct_update_one_validates_before_mutating(self) -> None:
        tracker = IncidentTracker()
        now = BASE_TIME
        with self.assertRaisesRegex(IncidentValidationError, "incident_id"):
            tracker._update_one(snapshot(), signal(None), now)  # type: ignore[arg-type]
        self.assertEqual(tracker.incidents, {})
        self.assertEqual(tracker.timeline, [])


if __name__ == "__main__":
    unittest.main()
