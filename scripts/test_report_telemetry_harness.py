"""Regression: the Probe publishes the EnergyCreated report counters (review M7).

The Python strategy derives power production/consumption watts from the last
closed daily report's EnergyCreated entry, published beside OxygenCreated as
report_counters.<period>.energy_created.positive_joules / negative_joules
(unit J, scope save_report_manager_energy_created).  The harness compiles the
Mod sources and drives ReportTelemetryReader against the real ReportManager
report types: both periods mirror the oxygen layout, an energy failure never
withdraws the oxygen counters, and a rejected period clears both.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tools" / "ReportTelemetryHarness"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


@unittest.skipUnless(shutil.which("dotnet") and (MANAGED / "Assembly-CSharp.dll").is_file(),
                     "dotnet SDK and the installed game assemblies are required")
class ReportTelemetryHarnessTests(unittest.TestCase):
    def test_energy_counters_mirror_oxygen(self):
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        run = subprocess.run(
            ["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "ReportTelemetryHarness.dll")],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])
        result = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["cases"], [
            "energy_mirrors_oxygen",
            "energy_failure_is_isolated",
            "period_failure_clears_energy",
            "unavailable_is_unsupported",
        ])


if __name__ == "__main__":
    unittest.main()
