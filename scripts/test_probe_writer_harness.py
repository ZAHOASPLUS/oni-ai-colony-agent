"""Run the ProbeWriter harness compiled from the Mod sources (review 2026-09-26, L3).

One sharing violation while opening state.jsonl (Bridge rotation, a copy
tool) used to raise the sticky process fault and end automation for the
rest of the game process.  The harness checks that records stay queued, no
fault is raised, and the retry writes one complete line.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tools" / "ProbeWriterHarness"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


@unittest.skipUnless(shutil.which("dotnet") and (MANAGED / "Assembly-CSharp.dll").is_file(),
                     "dotnet SDK and the installed game assemblies are required")
class ProbeWriterHarnessTests(unittest.TestCase):
    def test_transient_open_failure_keeps_records_and_process_healthy(self):
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        run = subprocess.run(
            ["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "ProbeWriterHarness.dll")],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        self.assertEqual(json.loads(run.stdout)["status"], "passed")


if __name__ == "__main__":
    unittest.main()
