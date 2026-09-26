"""Regression: a validation process cannot autosave (code review 2026-09-26, P0-1).

ONI's autosave keeps only the newest ten autosaves of a colony GUID in the
active save folder and deletes the rest; a test clone shares the user's GUID,
so every clone autosave deleted one of the user's own saves.  The harness
compiles the Mod sources and drives AutosaveGuard against the real SaveGame
and GameClock types: interval forced to 0 on every session start, re-asserted
by the tick and before each save write, the DoAutoSave prefix refusing the
autosave, and normal play (no accepted binding) left untouched.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tools" / "AutosaveGuardHarness"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


@unittest.skipUnless(shutil.which("dotnet") and (MANAGED / "Assembly-CSharp.dll").is_file(),
                     "dotnet SDK and the installed game assemblies are required")
class AutosaveGuardTests(unittest.TestCase):
    def test_validation_session_keeps_autosave_off(self):
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        with tempfile.TemporaryDirectory(prefix="oni-autosave-guard-") as bridge:
            # The harness redirects ProbeWriter itself; this keeps it away from
            # Documents\Klei even if that ever regresses.
            env = dict(os.environ, ONI_AI_BRIDGE_DIR=bridge)
            run = subprocess.run(
                ["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "AutosaveGuardHarness.dll")],
                capture_output=True, text=True, errors="replace", env=env,
            )
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])
        result = json.loads(run.stdout)
        self.assertEqual(result["status"], "passed")
        cases = set(result["cases"])
        self.assertTrue(
            {"suppression_patch_installed", "suppression_patch_failure_reported"} & cases,
            result["cases"],
        )
        self.assertLessEqual({
            "inactive_without_binding",
            "save_unavailable",
            "session_start_forces_zero",
            "prefix_binds_do_autosave",
            "tick_restores_zero",
            "save_write_serializes_zero",
            "do_autosave_refused",
            "reload_forces_zero",
            "events_recorded",
        }, cases)


if __name__ == "__main__":
    unittest.main()
