"""Regression: an approved command can reach a terminal disposition.

An approved command the executor never claimed expires with no disposition, and
the autonomous single-in-flight gate then counts it as an active intent forever:
the loop can neither publish a new command nor resume the game that command
needed (2026-10-25 round 4, 2026-10-26 round 34).

Disposing of it failed before reaching any of the custody guarantees:

    CommandCustodyConflict: pending disposition paths are outside the canonical queue

because the transition validated the source against the ``pending`` stage
unconditionally.  ``transition_pending_envelope`` now takes ``source_stage``, and
this test pins that the path check accepts ``approved`` while still rejecting a
source that is outside the canonical queue.
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

from agent.command_custody import (  # noqa: E402
    CommandCustodyConflict,
    CommandCustodyError,
    transition_pending_envelope,
)


class ApprovedDispositionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bridge = pathlib.Path(self._tmp.name)
        for stage in ("pending", "approving", "approved", "executing", "confirming", "done", "rejected", "quarantine"):
            (self.bridge / "commands" / stage).mkdir(parents=True, exist_ok=True)
        self.command_id = "cmd-test-approved-disposition"
        self.payload = {"id": self.command_id, "schema": "oni.command.v4"}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, stage: str) -> pathlib.Path:
        path = self.bridge / "commands" / stage / f"{self.command_id}.json"
        path.write_text(json.dumps(self.payload), encoding="utf-8")
        return path

    def call(self, source: pathlib.Path, *, source_stage: str):
        return transition_pending_envelope(
            self.bridge,
            source,
            self.bridge / "commands" / "rejected" / source.name,
            self.payload,
            operation="rejected",
            source_stage=source_stage,
        )

    def assert_paths_accepted(self, source: pathlib.Path, *, source_stage: str) -> None:
        """The call may fail on the envelope, but never on the source path.

        Round 35 fixed the path check but missed a second hardcoded ``pending``
        further in, which raised the misleading "present in multiple queue
        locations" conflict for a correctly-labelled approved source and faulted
        the whole bridge (round 38).  Both messages are refused here.
        """

        try:
            self.call(source, source_stage=source_stage)
        except CommandCustodyConflict as exc:
            self.assertNotIn("outside the canonical queue", str(exc))
            self.assertNotIn("multiple queue locations", str(exc))
        except (CommandCustodyError, ValueError, KeyError, TypeError):
            pass

    def test_an_approved_source_is_accepted_by_the_path_check(self):
        self.assert_paths_accepted(self.write("approved"), source_stage="approved")

    def test_a_pending_source_is_still_accepted(self):
        self.assert_paths_accepted(self.write("pending"), source_stage="pending")

    def test_the_wrong_stage_label_is_refused(self):
        # The source really is approved, but the caller claims pending.  The
        # refusal may come from the stage/status check or from the path check;
        # either way the mismatched label must not be accepted.
        source = self.write("approved")
        with self.assertRaises((CommandCustodyConflict, CommandCustodyError)):
            self.call(source, source_stage="pending")

    def test_an_unknown_stage_label_is_refused(self):
        source = self.write("approved")
        with self.assertRaises(CommandCustodyError):
            self.call(source, source_stage="not-a-stage")

    def test_a_source_outside_the_queue_is_refused(self):
        stray = self.bridge / f"{self.command_id}.json"
        stray.write_text(json.dumps(self.payload), encoding="utf-8")
        with self.assertRaises(CommandCustodyConflict) as caught:
            self.call(stray, source_stage="approved")
        self.assertIn("outside the canonical queue", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
