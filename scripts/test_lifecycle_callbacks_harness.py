"""Regression: no Mod Harmony callback lets an exception reach the game (review P2).

An exception escaping a Harmony prefix/postfix propagates into the patched
game method, so a Probe bug could fail the player's load or save.  The harness
compiles the Mod sources, checks every LifecyclePatches callback for a
catch-all boundary, and checks that a reported failure is recorded and raises
the sticky safety fault that stops the controllers.
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
HARNESS = ROOT / "tools" / "LifecycleCallbacksHarness"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


@unittest.skipUnless(shutil.which("dotnet") and (MANAGED / "Assembly-CSharp.dll").is_file(),
                     "dotnet SDK and the installed game assemblies are required")
class LifecycleCallbacksTests(unittest.TestCase):
    def test_callbacks_catch_and_report(self):
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        with tempfile.TemporaryDirectory(prefix="oni-lifecycle-callbacks-") as bridge:
            # Keep any ProbeWriter record away from Documents\Klei.
            env = dict(os.environ, ONI_AI_BRIDGE_DIR=bridge)
            run = subprocess.run(
                ["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "LifecycleCallbacksHarness.dll")],
                capture_output=True, text=True, errors="replace", env=env,
            )
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])
        # Unity's logger may print the safety-fault line before the result.
        result = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["cases"],
            ["every_callback_catches", "failure_raises_safety_fault", "failure_recorded"],
        )


if __name__ == "__main__":
    unittest.main()
