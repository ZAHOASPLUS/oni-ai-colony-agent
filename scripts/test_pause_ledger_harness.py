"""Regression: the executor's unpause never takes another holder's pause (review P2).

ONI's pause is reference counted: pausing modals (pause menu, save/load
screens, story sequences) call Pause() on open and Unpause() on close.  The
executor's set_paused=false called Unpause() once whatever held the pause.
The harness compiles the Mod sources and checks, against the real
SpeedControlScreen type, that an unpause is refused before dispatch unless the
Mod's Pause/Unpause patch observed exactly one reference.
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
HARNESS = ROOT / "tools" / "PauseLedgerHarness"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


@unittest.skipUnless(shutil.which("dotnet") and (MANAGED / "Assembly-CSharp.dll").is_file(),
                     "dotnet SDK and the installed game assemblies are required")
class PauseLedgerTests(unittest.TestCase):
    def test_executor_never_takes_another_holders_pause(self):
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        with tempfile.TemporaryDirectory(prefix="oni-pause-ledger-") as bridge:
            # Keep any ProbeWriter record away from Documents\Klei.
            env = dict(os.environ, ONI_AI_BRIDGE_DIR=bridge)
            run = subprocess.run(
                ["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "PauseLedgerHarness.dll")],
                capture_output=True, text=True, errors="replace", env=env,
            )
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])
        result = json.loads(run.stdout)
        self.assertEqual(result["status"], "passed")
        self.assertLessEqual({
            "patch_binds_pause_count",
            "unpaused_game_not_refused",
            "unknown_count_refused",
            "shared_pause_refused",
            "new_screen_not_inherited",
            "single_reference_and_modal_rules",
        }, set(result["cases"]))


if __name__ == "__main__":
    unittest.main()
