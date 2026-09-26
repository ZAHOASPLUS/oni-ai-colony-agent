"""Regression: done/status=verified for non-mutating commands (review 2026-09-26, L5).

The executor completes noop/focus_note directly in done/status=verified, as
docs/protocol.md specifies.  The strict custody inventory accepted only
"confirmed" in done/, so one successful focus_note made every later custody
operation (recovery, publication, approval, Bridge sync) raise.
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

from agent import command_custody as custody  # noqa: E402


def command(command_id: str, action: str, status: str) -> dict:
    return {"schema": custody.COMMAND_SCHEMA, "id": command_id, "action": action, "status": status}


class DoneStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-done-status-")
        self.addCleanup(self.temp.cleanup)
        self.bridge = pathlib.Path(self.temp.name) / "bridge"
        self.bridge.mkdir()
        custody.recover_lifecycle_transactions(self.bridge)  # canonical layout

    def put(self, stage: str, value: dict) -> pathlib.Path:
        path = self.bridge / "commands" / stage / f"{value['id']}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_verified_focus_note_in_done_keeps_custody_usable(self):
        self.put("done", command("cmd-note", "focus_note", "verified"))
        self.put("done", command("cmd-noop", "noop", "verified"))
        custody.recover_lifecycle_transactions(self.bridge)

    def test_pending_observation_tolerates_verified_focus_note(self):
        self.put("done", command("cmd-note", "focus_note", "verified"))
        pending = command("cmd-pause", "set_paused", custody.PENDING_STATUS)
        source = self.put("pending", pending)
        observed = custody.observe_pending_envelope(self.bridge, source, expected_source=pending)
        self.assertEqual(observed.path.name, source.name)

    def test_mutating_verified_in_done_still_fails_closed(self):
        self.put("done", command("cmd-pause", "set_paused", "verified"))
        with self.assertRaises(custody.CommandCustodyError):
            custody.recover_lifecycle_transactions(self.bridge)

    def test_confirmed_mutation_in_done_is_accepted(self):
        self.put("done", command("cmd-pause", "set_paused", "confirmed"))
        custody.recover_lifecycle_transactions(self.bridge)


if __name__ == "__main__":
    unittest.main()
