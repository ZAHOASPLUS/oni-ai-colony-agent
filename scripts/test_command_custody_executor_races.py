"""Regression: Bridge custody tolerates the unlocked executor's transient states (review 2026-09-26, L10).

The C# executor claims and finishes commands without the lifecycle lock.  The
strict inventory raised on its normal millisecond states (a temp/backup
sibling, status=executing still in approved/, a file moved away mid-read, one
id in two stages during copy-then-delete), and Python read handles lacked
FILE_SHARE_DELETE, so the executor's own rename failed while Bridge read the
file.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import command_custody  # noqa: E402

GUID = "0123456789abcdef0123456789abcdef"


def command(command_id: str, status: str, **extra) -> bytes:
    body = {"schema": "oni.command.v3", "id": command_id, "status": status, "action": "set_paused"}
    body.update(extra)
    return json.dumps(body).encode("utf-8")


def later(delay: float, action) -> threading.Thread:
    def run():
        time.sleep(delay)
        action()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class ExecutorRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-custody-races-")
        self.addCleanup(self.temp.cleanup)
        self.bridge = pathlib.Path(self.temp.name) / "bridge"
        self.bridge.mkdir()
        self.commands = self.bridge / "commands"
        for stage in command_custody.COMMAND_STAGES:
            (self.commands / stage).mkdir(parents=True)

    def stages(self):
        return sorted(item.stage for item in command_custody._scan_command_locations(self.bridge))

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_open_read_handle_does_not_deny_the_executor_rename(self):
        source = self.commands / "approved" / "cmd-a.json"
        source.write_bytes(command("cmd-a", "approved"))
        descriptor = command_custody._open_read_descriptor(source)
        try:
            os.replace(source, self.commands / "executing" / "cmd-a.json")
        finally:
            os.close(descriptor)

    def test_scan_waits_out_an_executor_temp_file(self):
        (self.commands / "approved" / "cmd-a.json").write_bytes(command("cmd-a", "approved"))
        residue = self.commands / "approved" / f"cmd-a.json.tmp-{GUID}"
        residue.write_bytes(b"{")
        later(0.15, residue.unlink)
        self.assertEqual(self.stages(), ["approved"])

    def test_scan_waits_out_an_in_place_claim(self):
        claimed = self.commands / "approved" / "cmd-a.json"
        claimed.write_bytes(command("cmd-a", "executing", execution_claim={"schema": "claim"}))
        later(0.15, lambda: os.replace(claimed, self.commands / "executing" / "cmd-a.json"))
        self.assertEqual(self.stages(), ["executing"])

    def test_copy_then_delete_settles_to_one_location(self):
        source = self.commands / "approved" / "cmd-a.json"
        source.write_bytes(command("cmd-a", "approved"))
        (self.commands / "failed" / "cmd-a.json").write_bytes(command("cmd-a", "failed"))
        later(0.15, source.unlink)
        self.assertEqual(self.stages(), ["failed"])

    def test_persistent_residue_still_fails_closed(self):
        (self.commands / "approved" / f"cmd-a.json.backup-{GUID}").write_bytes(b"{}")
        with self.assertRaises(command_custody.CommandCustodyUnknown):
            command_custody._scan_command_locations(self.bridge)

    def test_unexplained_status_fails_closed_immediately(self):
        (self.commands / "approved" / "cmd-a.json").write_bytes(command("cmd-a", "executing"))
        started = time.monotonic()
        with self.assertRaises(command_custody.CommandCustodyConflict):
            command_custody._scan_command_locations(self.bridge)
        self.assertLess(time.monotonic() - started, 0.5)  # no claim envelope: not a transient state


if __name__ == "__main__":
    unittest.main()
