"""Regression: informational suggestions do not churn once per snapshot.

Live run m1-0927c (2026-09-27): while the game ran, every snapshot superseded
the source binding of the pending idle_dupes focus_note, so it was rejected as
pending_source_snapshot_stale and re-issued on the next generation (29
rejected records in five minutes).  A non-mutating suggestion for the same
incident is now re-issued at most once per NON_MUTATING_RESUGGEST_SECONDS
after such a rejection; a mutating survival suggestion keeps its
per-generation refresh, and other incidents are not throttled.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import command_queue  # noqa: E402
from agent.field_contracts import (  # noqa: E402
    DUPLICANTS_IDLE_CONTRACT_ID,
    build_condition,
    contract_quality_fixture,
)
from scripts import verify_command_safety as fixtures  # noqa: E402


def note_advice(snapshot: dict, incident_id: str = "idle_duplicants") -> dict:
    message = "2 duplicant(s) are idle."
    return {
        "incident_id": incident_id,
        "incident_opened_at": "2026-09-27T10:28:19+08:00",
        "risk": "medium",
        "title": "Duplicants are idle",
        "reason": message,
        "evidence": ["duplicants.idle"],
        "source_snapshot_id": snapshot["snapshot_id"],
        "command": {
            "action": "focus_note",
            "args": {"tag": incident_id, "message": message},
            "preconditions": [
                build_condition("duplicants.idle", "gt", 0, DUPLICANTS_IDLE_CONTRACT_ID),
            ],
            "expected_effects": [],
        },
    }


def pause_advice(snapshot: dict) -> dict:
    item = fixtures.advice()
    item["source_snapshot_id"] = snapshot["snapshot_id"]
    return item


class ResuggestThrottleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-resuggest-")
        self.addCleanup(self.temp.cleanup)
        self.bridge = pathlib.Path(self.temp.name) / "bridge"
        fixtures.write_policy(self.bridge, enabled=False)

    def snapshot(self, seq: int) -> dict:
        record = fixtures.state()
        record.update({"seq": seq, "snapshot_seq": seq, "snapshot_id": f"{record['session_id']}:{seq}"})
        record["duplicants"] = {"idle": 2}
        record["quality"]["duplicants.idle"] = contract_quality_fixture(DUPLICANTS_IDLE_CONTRACT_ID)
        return record

    def generation(self, seq: int, *builders) -> list:
        """One Bridge generation: a new snapshot, its authorizing frame, then sync."""

        snapshot = self.snapshot(seq)
        frame = fixtures.write_authorizing_frame(self.bridge, snapshot)
        items = [build(snapshot) for build in builders]
        return command_queue.sync_pending_commands(self.bridge, snapshot, items, proposal_frame=frame)

    def stage(self, name: str) -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8-sig"))
            for path in sorted((self.bridge / "commands" / name).glob("*.json"))
        ]

    def test_a_stale_focus_note_is_not_reissued_within_the_window(self):
        self.assertEqual([c["action"] for c in self.generation(8, note_advice)], ["focus_note"])
        self.assertEqual(self.generation(9, note_advice), [])
        self.assertEqual(
            [r["rejection_reason"] for r in self.stage("rejected")], ["pending_source_snapshot_stale"]
        )
        self.assertEqual(self.stage("pending"), [])
        # Later snapshots inside the window still create nothing.
        self.assertEqual(self.generation(10, note_advice), [])
        self.assertEqual(len(self.stage("rejected")), 1)

    def test_the_focus_note_is_reissued_once_the_window_has_passed(self):
        self.generation(8, note_advice)
        self.generation(9, note_advice)
        path = next((self.bridge / "commands" / "rejected").glob("*.json"))
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        record["rejected_at"] = (
            command_queue.now() - dt.timedelta(seconds=command_queue.NON_MUTATING_RESUGGEST_SECONDS + 1)
        ).isoformat(timespec="seconds")
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual([c["action"] for c in self.generation(10, note_advice)], ["focus_note"])

    def test_other_incidents_and_other_rejection_reasons_are_not_throttled(self):
        self.generation(8, note_advice)
        self.generation(9, note_advice)  # idle_duplicants rejected stale
        other = lambda snapshot: note_advice(snapshot, incident_id="low_food")  # noqa: E731
        self.assertEqual(
            [c["source_incident_id"] for c in self.generation(10, note_advice, other)], ["low_food"]
        )
        # A rejection for a closed incident does not start a window.
        self.assertEqual(self.generation(11), [])
        reasons = sorted(r["rejection_reason"] for r in self.stage("rejected"))
        self.assertEqual(reasons, ["pending_source_snapshot_stale", "source_incident_not_open"])
        self.assertEqual(
            [c["source_incident_id"] for c in self.generation(12, other)], ["low_food"]
        )

    def test_a_mutating_suggestion_keeps_its_per_generation_refresh(self):
        fixtures.write_policy(self.bridge, enabled=True)
        self.assertEqual([c["action"] for c in self.generation(8, pause_advice)], ["set_paused"])
        self.assertEqual([c["action"] for c in self.generation(9, pause_advice)], ["set_paused"])
        self.assertEqual(
            [(r["action"], r["rejection_reason"]) for r in self.stage("rejected")],
            [("set_paused", "pending_source_snapshot_stale")],
        )


if __name__ == "__main__":
    unittest.main()
