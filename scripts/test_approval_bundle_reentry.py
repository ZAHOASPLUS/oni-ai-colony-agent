"""Regression (B8): re-entry for a done command must not need its source frame.

A confirmed command's approval bundle freezes its source decision frame and
policy before the command enters ``done``.  Once it is done, no nonterminal
command references that frame, so archive pruning may remove it.  The outcome
commit nevertheless rebuilt the bundle from the frame on every idempotent
re-entry and raised "confirmed command source frame could not be frozen before
done", which took the Bridge down (Phase 2 rounds 28 and 51).

The bundle validator and frame loader are replaced so the test pins only the
control flow: an existing bundle that freezes exactly this final command is
accepted without touching the archive; a conflicting one still fails; with no
bundle the frame is still required.
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

from agent import action_outcomes as A  # noqa: E402
from agent.decision_frame_archive import DecisionFrameArchiveError  # noqa: E402

ACTION = sorted(A._APPROVAL_BUNDLE_ACTIONS)[0]


def final_command(command_id="cmd-model-0001", **extra):
    command = {
        "id": command_id,
        "action": ACTION,
        "status": "confirmed",
        "source_frame_id": "sha256:" + "a" * 64,
    }
    command.update(extra)
    return command


def write_bundle(bridge, command):
    path = A.confirmed_command_approval_bundle_path(bridge, command["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    bundle = {
        "schema": A.CONFIRMED_COMMAND_APPROVAL_BUNDLE_SCHEMA,
        "command_id": command["id"],
        "final_command": dict(command),
        "final_command_hash": A._canonical_hash(command),
    }
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return path


def missing_frame(*_args, **_kwargs):
    raise DecisionFrameArchiveError("archived decision frame is missing")


class ApprovalBundleReentryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bridge = pathlib.Path(self._tmp.name)
        patches = (
            mock.patch.object(A, "validate_confirmed_command_approval_bundle", lambda bundle: None),
            mock.patch.object(A, "load_archived_decision_frame", side_effect=missing_frame),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_existing_bundle_is_accepted_without_the_pruned_frame(self):
        command = final_command()
        path = write_bundle(self.bridge, command)
        bundle, bundle_path = A.persist_confirmed_command_approval_bundle(self.bridge, command)
        self.assertEqual(bundle_path, path)
        self.assertEqual(bundle["final_command"], command)
        A.load_archived_decision_frame.assert_not_called()

    def test_a_bundle_for_a_different_final_command_still_conflicts(self):
        write_bundle(self.bridge, final_command(status="confirmed", note="other"))
        with self.assertRaisesRegex(A.ActionOutcomeError, "conflicts"):
            A.persist_confirmed_command_approval_bundle(self.bridge, final_command())

    def test_an_unreadable_bundle_still_fails_closed(self):
        path = A.confirmed_command_approval_bundle_path(self.bridge, "cmd-model-0001")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(A.ActionOutcomeError):
            A.persist_confirmed_command_approval_bundle(self.bridge, final_command())

    def test_without_a_bundle_the_frame_is_still_required(self):
        with self.assertRaisesRegex(A.ActionOutcomeError, "could not be frozen before done"):
            A.persist_confirmed_command_approval_bundle(self.bridge, final_command())
        self.assertFalse(A.confirmed_command_approval_bundle_path(self.bridge, "cmd-model-0001").exists())


if __name__ == "__main__":
    unittest.main()
