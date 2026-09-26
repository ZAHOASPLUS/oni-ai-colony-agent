"""Regression: the executor's confirmation witness is polled, not spun (review P2).

The executor read the Bridge-confirmation witness every frame, and a read
failure re-latched, re-paused and re-logged every frame.  The harness compiles
the Mod sources and drives ActionExecutor.Update on a temporary bridge
directory: the witness follows a poll cadence, a short sharing violation is
retried, and a lasting or malformed confirmation is resolved exactly once.
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
HARNESS = ROOT / "tools" / "WitnessPollHarness"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


@unittest.skipUnless(shutil.which("dotnet") and (MANAGED / "Assembly-CSharp.dll").is_file(),
                     "dotnet SDK and the installed game assemblies are required")
class WitnessPollTests(unittest.TestCase):
    def test_witness_polled_on_cadence_and_resolved_once(self):
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        with tempfile.TemporaryDirectory(prefix="oni-witness-poll-") as bridge:
            # Keep any ProbeWriter record away from Documents\Klei.
            env = dict(os.environ, ONI_AI_BRIDGE_DIR=bridge)
            run = subprocess.run(
                ["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "WitnessPollHarness.dll")],
                capture_output=True, text=True, errors="replace", env=env,
            )
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])
        result = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["cases"], [
            "polled_on_cadence",
            "transient_violation_retried",
            "lasting_violation_latched",
            "malformed_latched_on_first_poll",
            "each_failure_recorded_once",
        ])


if __name__ == "__main__":
    unittest.main()
