"""Regression: the Probe rejects a stale startup binding (code review 2026-09-26).

StartupBinding accepted any binding file left in the Bridge directory: two
processes on 2026-09-13 both read the binding written on 2026-09-09.  The
harness compiles the Mod sources and evaluates the freshness rule against
its own process start time.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tools" / "StartupBindingHarness"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


@unittest.skipUnless(shutil.which("dotnet") and (MANAGED / "Assembly-CSharp.dll").is_file(),
                     "dotnet SDK and the installed game assemblies are required")
class StartupBindingFreshnessTests(unittest.TestCase):
    def test_only_a_binding_written_shortly_before_start_is_fresh(self):
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        run = subprocess.run(
            ["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "StartupBindingHarness.dll")],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])
        self.assertEqual(json.loads(run.stdout)["fresh"], {
            "one_hour_before_start": True,
            "thirteen_hours_before_start": False,
            "ten_minutes_after_start": False,
            "unparseable": False,
        })


if __name__ == "__main__":
    unittest.main()
