"""The read-only live review replays the generator over a recorded session."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import review_live_candidates as R  # noqa: E402
from scripts.recorded_state import load_recorded_policy  # noqa: E402
from scripts.test_space_heater import TECH, heater_state  # noqa: E402


class ReviewLiveCandidatesTests(unittest.TestCase):
    def review(self, states, stride=1):
        with tempfile.TemporaryDirectory() as temp:
            archive = pathlib.Path(temp) / "state.jsonl"
            lines = [json.dumps({"event": "session_started", "session_id": "s"})]
            for index, state in enumerate(states):
                state["event"] = "state_snapshot"
                state["snapshot_seq"] = index + 1
                state["read_metrics_ms"] = {"blueprints": 10.0 + index}
                lines.append(json.dumps(state))
            archive.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return R.review(archive, load_recorded_policy()["allowed_actions"], stride)

    def test_heater_notes_and_candidates_are_summarized(self) -> None:
        summary = self.review([heater_state(unlock=(False, TECH, True)), heater_state(unlock=(False, TECH, False))])
        self.assertEqual(summary["snapshots"], 2)
        self.assertEqual(summary["candidate_families"]["research"], 1)
        self.assertEqual(summary["first_seen"]["candidate:research"]["snapshot_seq"], 1)
        self.assertEqual(summary["notes"][f"unlock_research_blocked:SpaceHeater:{TECH}"], 1)
        self.assertIn("note:temperature_alert:hypothermia", summary["first_seen"])
        self.assertEqual(summary["probe_fields_present"]["space_heater_catalog_unlock"], "2/2")
        self.assertEqual(summary["read_metrics_ms_blueprints"], {"count": 2, "median": 10.5, "max": 11.0})
        self.assertEqual(summary["final_buildings"]["SpaceHeater"]["built"], 0)
        self.assertEqual(summary["final_alerts"], ["hypothermia", "urgent_toilet"])

    def test_stride_samples_but_metrics_cover_every_snapshot(self) -> None:
        summary = self.review([heater_state(), heater_state(), heater_state()], stride=2)
        self.assertEqual(summary["sampled"], 2)
        self.assertEqual(summary["read_metrics_ms_blueprints"]["count"], 3)

    def test_an_archive_without_snapshots_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.review([])


if __name__ == "__main__":
    unittest.main()
