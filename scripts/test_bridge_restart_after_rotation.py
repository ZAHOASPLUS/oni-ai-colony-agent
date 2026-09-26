"""Regression: a restarted Bridge re-verifies a session after log rotation (review 2026-09-26, L6).

Startup replay read only the active state.jsonl.  After a rotation the
session's seq=1 start lives in state.jsonl.N or (a few rotations later) only
in the session archive, so a restarted Bridge ignored every record as
pre-session noise and never trusted the session again.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge import oni_bridge  # noqa: E402
from scripts import verify_bridge_reliability as fixtures  # noqa: E402

SESSION = "session-rotation-restart"


class RestartAfterRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-rotation-restart-")
        self.addCleanup(self.temp.cleanup)
        self.bridge = pathlib.Path(self.temp.name) / "bridge"
        self.bridge.mkdir()
        self.expected = fixtures.startup_expectations()
        binding = dict(active_path_id=self.expected["loader_path_id_upper"],
                       save_id_hash=self.expected["loader_save_sha256"])
        ts = fixtures.iso_now()
        self.records = [fixtures.startup_event(self.expected, SESSION, 1, "session_started",
                                               reason="save_loader_load_complete", **binding)]
        for seq in range(2, 11):
            self.records.append(fixtures.startup_snapshot(self.expected, SESSION, seq, seq - 1, **binding))
        for record in self.records:
            record["ts"] = ts

    def prepare(self, *, archived: range, active: range) -> None:
        # What the previous Bridge run archived, and what the active log still
        # holds after its last rotation.
        oni_bridge.archive_session_records(
            self.bridge, [self.records[seq - 1] for seq in archived], rebuild=False
        )
        with (self.bridge / "state.jsonl").open("wb") as stream:
            for seq in active:
                stream.write(fixtures.json_line(self.records[seq - 1]) + b"\n")

    def restart(self) -> dict:
        oni_bridge.run_once(self.bridge, startup_expectations=self.expected)
        return json.loads((self.bridge / "health.json").read_text(encoding="utf-8"))

    def test_session_start_from_the_archive_restores_trust(self):
        self.prepare(archived=range(1, 8), active=range(6, 11))
        health = self.restart()
        self.assertEqual((health["status"], health["trust_ready"]), ("ok", True), health.get("trust_blockers"))
        self.assertEqual(health["latest_snapshot_id"], f"{SESSION}:10")
        archive = self.bridge / "sessions" / SESSION / "state.jsonl"
        sequences = [json.loads(line)["seq"] for line in archive.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sequences, list(range(1, 11)))  # nothing replayed twice

    def test_archive_gap_stays_untrusted(self):
        self.prepare(archived=range(1, 4), active=range(6, 11))
        health = self.restart()
        self.assertFalse(health["trust_ready"])

    def test_explicit_input_replay_does_not_read_the_archive(self):
        self.prepare(archived=range(1, 8), active=range(6, 11))
        runtime, _ = oni_bridge.replay_initial_records(
            self.bridge / "state.jsonl", self.bridge,
            startup_expectations=self.expected, require_startup_binding=True,
        )
        self.assertIsNone(runtime.startup_verified_session_id)


if __name__ == "__main__":
    unittest.main()
