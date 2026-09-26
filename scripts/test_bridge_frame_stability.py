"""Regression: publications without new records keep one frame (review 2026-09-26, L1).

The frame projected health.snapshot_age_seconds, and frame_id is a digest of
the frame, so every poll produced a new frame and the Bridge rejected and
re-created each pending suggestion as ``pending_source_frame_stale``.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge import oni_bridge  # noqa: E402
from scripts import verify_bridge_reliability as fixtures  # noqa: E402


class FrameStabilityTests(unittest.TestCase):
    def test_polls_without_records_keep_frame_and_pending(self):
        expected = fixtures.startup_expectations()
        binding = dict(
            active_path_id=expected["loader_path_id_upper"],
            save_id_hash=expected["loader_save_sha256"],
        )
        ts = fixtures.iso_now()
        records = [fixtures.startup_event(
            expected, "session-frames", 1, "session_started",
            reason="save_loader_load_complete", **binding,
        )]
        for seq in range(2, 5):
            records.append(fixtures.startup_snapshot(expected, "session-frames", seq, seq - 1, **binding))
        for record in records:
            record["ts"] = ts

        real_advice = oni_bridge.agent_rules.build_arbitrated_advice

        def advice_with_noop(latest, open_incidents):
            items = real_advice(latest, open_incidents)
            if not isinstance(latest, dict) or not latest.get("bridge", {}).get("trust_ready"):
                return items
            return items + [{
                "incident_id": "test.noop", "incident_opened_at": ts, "risk": "low",
                "title": "noop", "reason": "frame stability", "evidence": [],
                "source_snapshot_id": latest.get("snapshot_id"),
                "command": {"action": "noop", "args": {}, "preconditions": [], "expected_effects": []},
            }]

        with tempfile.TemporaryDirectory(prefix="oni-frames-") as temp:
            bridge = pathlib.Path(temp) / "bridge"
            bridge.mkdir()
            (bridge / "action-policy.json").write_text(json.dumps({
                "schema": "oni.action-policy.v1", "actions_enabled": False, "suggestions_enabled": True,
                "allowed_actions": ["noop", "focus_note", "set_paused"],
                "allowed_save_hashes": [expected["loader_save_sha256"]],
                "command_ttl_seconds": 30, "max_pending_commands": 20,
            }), encoding="utf-8")
            with (bridge / "state.jsonl").open("wb") as stream:
                for record in records:
                    stream.write(fixtures.json_line(record) + b"\n")
            runtime, _ = oni_bridge.replay_initial_records(
                bridge / "state.jsonl", bridge,
                startup_expectations=expected, require_startup_binding=True,
            )

            def observe():
                frame = json.loads((bridge / "decision-frame.json").read_text(encoding="utf-8"))
                pending = sorted(p.name for p in (bridge / "commands" / "pending").glob("*.json"))
                rejected = len(list((bridge / "commands" / "rejected").glob("*.json")))
                return frame["frame_id"], pending, rejected

            with mock.patch.object(oni_bridge.agent_rules, "build_arbitrated_advice", advice_with_noop):
                # The first suggestion changes the pipeline counts once; the
                # publication then reaches a steady state.
                for _ in range(2):
                    oni_bridge.write_outputs(bridge, runtime)
                steady = observe()
                self.assertEqual(len(steady[1]), 1)
                for _ in range(3):
                    time.sleep(0.05)  # snapshot age advances between polls
                    oni_bridge.write_outputs(bridge, runtime)
                    self.assertEqual(observe(), steady)


if __name__ == "__main__":
    unittest.main()
