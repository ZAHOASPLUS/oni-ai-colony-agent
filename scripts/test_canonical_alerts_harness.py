"""Regression: canonical alerts cover every world (code review 2026-09-26, G3).

The alert reader used to scan only the camera's world yet mark the list
complete, so a duplicant suffocating on a second asteroid or in a rocket
produced alerts=[].  CanonicalAlertsHarness links the production reader and
catalog against typed doubles; CommandContractsHarness checks the executor's
alert contract against the reader's source and scope.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


def build_and_run(name: str) -> subprocess.CompletedProcess[str]:
    harness = TOOLS / name
    build = subprocess.run(
        ["dotnet", "build", str(harness), "-c", "Release", "-nologo", "-v", "q"],
        capture_output=True, text=True, errors="replace",
    )
    if build.returncode != 0:
        raise AssertionError(build.stdout[-2000:] + build.stderr[-2000:])
    return subprocess.run(
        ["dotnet", str(harness / "bin" / "Release" / "net8.0" / f"{name}.dll")],
        capture_output=True, text=True, errors="replace",
    )


@unittest.skipUnless(shutil.which("dotnet"), "dotnet SDK is required")
class CanonicalAlertsHarnessTests(unittest.TestCase):
    def test_reader_covers_every_world_and_tags_alerts(self):
        run = build_and_run("CanonicalAlertsHarness")
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])
        self.assertIn("all_worlds_tagged", run.stdout)
        self.assertIn("catalog=oni.alert.catalog.all_worlds.v1", run.stdout)

    @unittest.skipUnless((MANAGED / "Newtonsoft.Json.dll").is_file(), "installed game assemblies are required")
    def test_executor_alert_contract_matches_reader(self):
        run = build_and_run("CommandContractsHarness")
        self.assertEqual(run.returncode, 0, run.stdout[-3000:] + run.stderr[-3000:])
        self.assertIn("valid_canonical_alerts", run.stdout)


if __name__ == "__main__":
    unittest.main()
