"""Regression: the frame's propose gate must not be blocked by the loop's own command.

The decision frame marks ``readiness.propose`` not ready whenever the command
pipeline carries a mutation blocker, and the command the loop has just published
to ``pending/`` is itself such a blocker.  The authorization path already tolerates
exactly that case (``validate_autonomous_pending_custody`` permits
``mutation_pending_model_selection:<this command>``), so refusing earlier only
burned the command's TTL: every such command was rejected with
``expired_before_approval`` (2026-10-25 round 22).

The reconciliation is deliberately narrow: only the loop's own single pending
command, and only the two tolerated prefixes.  An ``_invalid`` envelope, another
mutation, or more than one pending command still refuses.
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

from agent.model_loop import ModelLoop  # noqa: E402

PROPOSE_BLOCKED = {"ready": False, "blockers": ["COMMAND_PIPELINE_MUTATION_BLOCKED"]}
PROPOSE_READY = {"ready": True, "blockers": []}
OWN = "cmd-model-0123456789abcdef0123456789abcdef"


class ProposalOwnPendingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = ModelLoop()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "commands" / "pending").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, pipeline_blockers: list[str], pending_ids: list[str]) -> None:
        (self.root / "health.json").write_text(
            json.dumps({"command_pipeline": {"blockers": pipeline_blockers}}), encoding="utf-8"
        )
        for command_id in pending_ids:
            (self.root / "commands" / "pending" / f"{command_id}.json").write_text("{}", encoding="utf-8")

    def accepted(self) -> bool:
        return self.loop._proposal_blocked_only_by_own_pending(self.root, PROPOSE_BLOCKED)

    def test_a_ready_frame_needs_no_reconciliation(self):
        self.write([], [])
        self.assertFalse(self.loop._proposal_blocked_only_by_own_pending(self.root, PROPOSE_READY))

    def test_the_loops_own_single_pending_command_is_accepted(self):
        self.write([f"mutation_pending_model_selection:{OWN}"], [OWN])
        self.assertTrue(self.accepted())

    def test_the_legacy_selection_prefix_is_accepted(self):
        self.write([f"mutation_pending_selection:{OWN}"], [OWN])
        self.assertTrue(self.accepted())

    def test_an_invalid_envelope_still_refuses(self):
        self.write([f"mutation_pending_model_selection_invalid:{OWN}"], [OWN])
        self.assertFalse(self.accepted())

    def test_another_commands_blocker_refuses(self):
        other = "cmd-model-ffffffffffffffffffffffffffffffff"
        self.write([f"mutation_pending_model_selection:{other}"], [OWN])
        self.assertFalse(self.accepted())

    def test_two_pending_commands_refuse(self):
        other = "cmd-model-ffffffffffffffffffffffffffffffff"
        self.write([f"mutation_pending_model_selection:{OWN}"], [OWN, other])
        self.assertFalse(self.accepted())

    def test_a_non_pending_blocker_refuses(self):
        self.write(["ambiguous_outcome:" + OWN], [OWN])
        self.assertFalse(self.accepted())

    def test_an_unreadable_pipeline_refuses(self):
        (self.root / "health.json").write_text("not json", encoding="utf-8")
        self.assertFalse(self.accepted())


if __name__ == "__main__":
    unittest.main()
