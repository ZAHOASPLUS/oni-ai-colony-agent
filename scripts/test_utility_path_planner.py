"""Roadmap B15: the Probe's supply-link path search, checked without the game.

mod/Source/Probe/UtilityPathPlanner.cs is pure (the Probe passes the game's own
placement check as a callback), so tools/UtilityPathPlannerHarness compiles it
alone and checks routing, bounds, row edges and the multi-stop trunk.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tools" / "UtilityPathPlannerHarness"


@unittest.skipUnless(shutil.which("dotnet"), "dotnet SDK is required")
class UtilityPathPlannerTests(unittest.TestCase):
    def test_planner_checks_pass(self) -> None:
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        run = subprocess.run(
            ["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "UtilityPathPlannerHarness.dll")],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(run.returncode, 0, run.stdout[-3000:] + run.stderr[-3000:])
        self.assertIn("all_checks_passed", run.stdout)
        self.assertNotIn("FAIL", run.stdout)


if __name__ == "__main__":
    unittest.main()
