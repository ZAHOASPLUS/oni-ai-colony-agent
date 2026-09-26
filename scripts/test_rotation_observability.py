"""Exercise the real watch/rotate/tail path against deterministic temp logs."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge import oni_bridge as bridge
from scripts.verify_bridge_reliability import make_state


class StopWatch(Exception):
    pass


class RotationObservabilityTests(unittest.TestCase):
    def exercise(self, *, append_tail: bool, missing: bool = False):
        evidence = ROOT / "reports" / "l19" / "l20" / "main"
        with tempfile.TemporaryDirectory(prefix="rotation-metrics-", dir=evidence) as tmp:
            root = pathlib.Path(tmp)
            log = root / "state.jsonl"
            session = "offline-rotation-observability"
            records = [{"schema": "oni.event.v2", "event": "session_started",
                        "session_id": session, "seq": 1}]
            for n in range(1, 8):
                record = make_state(session, n + 1, snapshot_seq=n)
                record["fixture_padding"] = "x" * (3 * 1024 * 1024)
                records.append(record)
            with log.open("w", encoding="utf-8", newline="\n") as stream:
                for record in records:
                    stream.write(json.dumps(record, separators=(",", ":")) + "\n")
            original_identity = (log.stat().st_dev, log.stat().st_ino)
            self.assertGreaterEqual(log.stat().st_size, bridge.MAX_LOG_BYTES)
            runtime = bridge.ReplayState()
            appended = False

            def output(_root, current, **kwargs):
                nonlocal appended
                # Append after watch sampled the active file, before its real rename.
                if append_tail and not appended and current.last_seq == 8:
                    next_seq = 10 if missing else 9
                    next_snapshot = 9 if missing else 8
                    with log.open("a", encoding="utf-8", newline="\n") as stream:
                        stream.write(json.dumps(make_state(session, next_seq,
                                                           snapshot_seq=next_snapshot)) + "\n")
                    appended = True

            with mock.patch.object(bridge, "replay_initial_records", return_value=(runtime, 0)), \
                    mock.patch.object(bridge, "write_outputs", side_effect=output), \
                    mock.patch.object(bridge.time, "sleep", side_effect=StopWatch):
                with self.assertRaises(StopWatch):
                    bridge.watch(root, 0.1)
            rotated = pathlib.Path(str(log) + ".1")
            self.assertEqual((rotated.stat().st_dev, rotated.stat().st_ino), original_identity)
            self.assertEqual(runtime.rotation_count, 1)
            health = runtime.health(runtime.effective_latest(), {"actions_enabled": False})
            self.assertEqual(health["last_rotation_tail"]["session_id"], session)
            self.assertEqual(health["last_rotation_tail"]["event_seq_before"], 8)
            self.assertEqual(health["last_rotation_tail"]["snapshot_seq_before"], 7)
            self.assertEqual(health["last_rotation_tail"]["records"], int(append_tail))
            self.assertEqual(health["rotation_tail_records_total"], int(append_tail))
            self.assertEqual(health["last_rotation_tail"]["offset_after"], rotated.stat().st_size)
            return runtime, health

    def test_zero_tail_is_explicit(self):
        runtime, health = self.exercise(append_tail=False)
        tail = health["last_rotation_tail"]
        self.assertEqual(tail["offset_before"], tail["offset_after"])
        self.assertEqual((health["last_event_seq"], health["last_snapshot_seq"]), (8, 7))
        self.assertEqual((runtime.seq_gap_count, runtime.snapshot_seq_gap_count), (0, 0))

    def test_tail_count_and_distinct_sequence_domains(self):
        runtime, health = self.exercise(append_tail=True)
        tail = health["last_rotation_tail"]
        self.assertGreater(tail["offset_after"], tail["offset_before"])
        self.assertEqual((tail["event_seq_after"], tail["snapshot_seq_after"]), (9, 8))
        self.assertEqual((health["last_event_seq"], health["last_snapshot_seq"]), (9, 8))
        self.assertEqual((runtime.seq_gap_count, runtime.snapshot_seq_gap_count), (0, 0))

    def test_metrics_do_not_clear_a_real_gap(self):
        runtime, health = self.exercise(append_tail=True, missing=True)
        self.assertGreater(runtime.seq_gap_count, 0)
        self.assertGreater(runtime.snapshot_seq_gap_count, 0)
        self.assertFalse(health["trust_ready"])
        self.assertIn("sequence_gap", health["trust_blockers"])
        self.assertIn("snapshot_sequence_gap", health["trust_blockers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
