"""Regression tests for ReplayState's semantic incident-signal cache."""

from __future__ import annotations

import copy
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import oni_bridge
from scripts.test_controller_loop import snapshot


class BridgeSignalCacheTests(unittest.TestCase):
    def test_identical_semantics_reuse_and_telemetry_change_invalidates(self) -> None:
        runtime = oni_bridge.ReplayState(allow_untrusted_snapshot_baseline=True)
        state = snapshot(seq=2, snapshot_seq=1)
        runtime.process({
            "schema": "oni.event.v2",
            "event": "session_started",
            "session_id": state["session_id"],
            "seq": 1,
            "snapshot_seq": 0,
            "reason": "save_loader_load_complete",
            "ts": state["ts"],
        })
        changed = copy.deepcopy(state)
        changed["seq"] = 3
        changed["snapshot_seq"] = 2
        changed["snapshot_id"] = "offline-m28:11"
        original = oni_bridge.agent_rules.evaluate_signals
        with mock.patch.object(oni_bridge.agent_rules, "evaluate_signals", wraps=original) as evaluate:
            runtime.process(state)
            runtime.process(changed)
            self.assertEqual(evaluate.call_count, 1)
            changed["resources"]["food_kcal"] += 1
            changed["seq"] = 4
            changed["snapshot_seq"] = 3
            changed["snapshot_id"] = "offline-m28:12"
            runtime.process(changed)
            self.assertEqual(evaluate.call_count, 2)
            changed["ts"] = "2026-09-10T00:00:01+00:00"
            changed["seq"] = 5
            changed["snapshot_seq"] = 4
            changed["snapshot_id"] = "offline-m28:13"
            runtime.process(changed)
            self.assertEqual(evaluate.call_count, 3)


if __name__ == "__main__":
    unittest.main()
