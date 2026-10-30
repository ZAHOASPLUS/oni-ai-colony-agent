"""Controller start with a latest_state.json left by an earlier run.

M1 round 1 (2026-10-30): the Bridge directory still held Phase 2's last
snapshot (another session), so ``start_controller`` refused with
``autonomous_control_state_identity_mismatch`` before its bootstrap could
publish this run's own observation.  Only the start path may set that
snapshot aside; every later check still sees it.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import oni_autonomous as A  # noqa: E402


class BoundInputsForeignStateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "action-policy.json").write_text(json.dumps({"schema": "oni.action-policy.v2"}), encoding="utf-8")
        self.config = {"bridge_dir": self.root, "session_id": "session-new"}
        self.seen = []
        patches = [
            mock.patch.object(A, "_new_control", return_value={}),
            mock.patch.object(A, "validate_autonomous_control", side_effect=lambda _c, policy, latest_state: self.seen.append(latest_state)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _latest(self, session: str) -> dict:
        latest = {"session_id": session, "snapshot_seq": 1}
        (self.root / "latest_state.json").write_text(json.dumps(latest), encoding="utf-8")
        return latest

    def test_the_start_path_sets_a_foreign_snapshot_aside(self):
        self._latest("session-old")
        _, latest, control = A._bound_inputs(self.config, ignore_foreign_latest=True)
        self.assertIsNone(latest)
        self.assertIsNone(control)
        self.assertEqual(self.seen, [None])

    def test_every_other_caller_still_checks_a_foreign_snapshot(self):
        foreign = self._latest("session-old")
        _, latest, _ = A._bound_inputs(self.config)
        self.assertEqual(latest, foreign)
        self.assertEqual(self.seen, [foreign])

    def test_this_runs_own_snapshot_is_always_checked(self):
        own = self._latest("session-new")
        _, latest, _ = A._bound_inputs(self.config, ignore_foreign_latest=True)
        self.assertEqual(latest, own)
        self.assertEqual(self.seen, [own])


class StartControllerBootstrapTests(unittest.TestCase):
    def test_a_foreign_snapshot_leads_to_the_bootstrap(self):
        calls = []

        def bound(config, *, ignore_foreign_latest=False):
            calls.append(("bound", ignore_foreign_latest))
            return {}, (None if len(calls) == 1 else {"fresh": True}), None

        stop = RuntimeError("stop after the start checks")
        with mock.patch.object(A, "_bound_inputs", side_effect=bound), \
                mock.patch.object(A, "_bootstrap_observation", side_effect=lambda _c: calls.append(("bootstrap",))), \
                mock.patch.object(A, "_observation_is_untrusted", side_effect=lambda latest: latest is None), \
                mock.patch.object(A, "_object", side_effect=stop):
            with self.assertRaises(RuntimeError):
                A.start_controller({"bridge_dir": pathlib.Path(".")})
        self.assertEqual(calls, [("bound", True), ("bootstrap",), ("bound", False)])


if __name__ == "__main__":
    unittest.main()
