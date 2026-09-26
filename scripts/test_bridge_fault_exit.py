"""Regression: Bridge failures fail closed and reach the caller (review 2026-09-26, L4).

The watch loop had no exception boundary, so an exception (run18:
CommandCustodyConflict after the Bridge directory was replaced) killed the
process while the last ``ok`` health stayed in place, and run-bridge.ps1
still exited 0 because a -File script ignores a native command's exit code.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.command_custody import CommandCustodyConflict  # noqa: E402
from bridge import oni_bridge  # noqa: E402


class BridgeFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oni-bridge-fault-")
        self.addCleanup(self.temp.cleanup)
        self.bridge = pathlib.Path(self.temp.name) / "bridge"
        self.bridge.mkdir()
        (self.bridge / "state.jsonl").write_bytes(b"")

    def health(self) -> dict:
        return json.loads((self.bridge / "health.json").read_text(encoding="utf-8"))

    def test_watch_publishes_fault_health_and_reraises(self):
        failure = CommandCustodyConflict("Bridge directory identity changed")
        with mock.patch.object(oni_bridge, "write_outputs", side_effect=[None, failure]), \
                mock.patch.object(oni_bridge.time, "sleep"):
            with self.assertRaises(CommandCustodyConflict):
                oni_bridge.watch(self.bridge, 0.1)
        health = self.health()
        self.assertEqual((health["status"], health["trust_ready"]), ("bridge_fault", False))
        self.assertEqual(health["fault"]["type"], "CommandCustodyConflict")

    def test_run_once_publishes_fault_health_and_reraises(self):
        with mock.patch.object(oni_bridge, "write_outputs", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                oni_bridge.run_once(self.bridge)
        self.assertEqual(self.health()["status"], "bridge_fault")

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell is required")
    def test_launcher_returns_the_bridge_exit_code(self):
        invalid_anchors = [
            "-ExpectedValidationRunId", "run", "-ExpectedValidationProcessId", "1",
            "-ExpectedStartupBindingSha256", "bad", "-ExpectedStartupIntentHash", "bad",
            "-ExpectedRuntimeConfigEvidenceSha256", "bad", "-ExpectedLoaderPathIdUpper", "bad",
            "-ExpectedLoaderSaveSha256", "bad", "-ExpectedGameBuild", "bad", "-ExpectedModVersion", "bad",
        ]
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(ROOT / "scripts" / "run-bridge.ps1"), "-BridgeDir", str(self.bridge), "-Once",
             *invalid_anchors],
            capture_output=True, text=True, errors="replace", timeout=120,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.bridge / "health.json").exists())  # rejected before any output


if __name__ == "__main__":
    unittest.main()
