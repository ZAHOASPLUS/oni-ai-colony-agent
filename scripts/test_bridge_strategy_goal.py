"""Regression: one strategy goal per Bridge runtime (code review 2026-09-26, L2).

scripts/run-bridge.ps1 always passes --goal with its own default text.  The
per-record observer used STRATEGY_GOAL while write_outputs used --goal, so the
same snapshot produced two different strategy payloads and the tracker reset
its evidence window on every publication.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge import oni_bridge  # noqa: E402
from scripts import verify_bridge_reliability as fixtures  # noqa: E402

LAUNCHER_GOAL = "Keep the colony alive with conservative, reversible actions."


class BridgeStrategyGoalTests(unittest.TestCase):
    def publish_session(self, root: pathlib.Path) -> pathlib.Path:
        expected = fixtures.startup_expectations()
        binding = dict(
            active_path_id=expected["loader_path_id_upper"],
            save_id_hash=expected["loader_save_sha256"],
        )
        now = dt.datetime.now(dt.timezone.utc).astimezone()
        started = fixtures.startup_event(
            expected, "session-goal", 1, "session_started",
            reason="save_loader_load_complete", **binding,
        )
        snapshot = fixtures.startup_snapshot(expected, "session-goal", 2, 1, **binding)
        started["ts"] = snapshot["ts"] = now.isoformat(timespec="milliseconds")
        bridge = root / "bridge"
        bridge.mkdir()
        with (bridge / "state.jsonl").open("wb") as stream:
            for record in (started, snapshot):
                stream.write(fixtures.json_line(record) + b"\n")
        self.expected = expected
        return bridge

    def accumulated_runtime(self, bridge: pathlib.Path, **kwargs):
        runtime, _ = oni_bridge.replay_initial_records(
            bridge / "state.jsonl",
            bridge,
            startup_expectations=self.expected,
            require_startup_binding=True,
            **kwargs,
        )
        tracker = runtime.strategy_tracker
        tracker.integrity_blockers = []
        tracker.consecutive_samples = 7  # a clean window had accumulated
        return runtime

    def test_launcher_goal_keeps_evidence_window(self):
        with tempfile.TemporaryDirectory(prefix="oni-goal-") as temp:
            bridge = self.publish_session(pathlib.Path(temp))
            runtime = self.accumulated_runtime(bridge, strategy_goal=LAUNCHER_GOAL)
            oni_bridge.write_outputs(bridge, runtime, goal=LAUNCHER_GOAL)
            tracker = runtime.strategy_tracker
            self.assertNotIn("strategy_duplicate_conflict", tracker.integrity_blockers)
            self.assertEqual(tracker.consecutive_samples, 7)

    def test_write_outputs_goal_cannot_fork_the_strategy_goal(self):
        with tempfile.TemporaryDirectory(prefix="oni-goal-") as temp:
            bridge = self.publish_session(pathlib.Path(temp))
            runtime = self.accumulated_runtime(bridge)
            oni_bridge.write_outputs(bridge, runtime, goal=LAUNCHER_GOAL)
            tracker = runtime.strategy_tracker
            self.assertNotIn("strategy_duplicate_conflict", tracker.integrity_blockers)
            self.assertEqual(tracker.consecutive_samples, 7)


if __name__ == "__main__":
    unittest.main()
