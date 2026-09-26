"""Regression: the functional-building reader's prefab coverage (review M7).

The Python strategy counts flush toilets toward sanitation, so the Probe's
functional-building reader must cover FlushToilet.  The harness compiles the
Mod sources and checks, against the installed game assembly, that every
supported prefab id is a real building id (OxygenDiffuser is a kept legacy
alias) and that the reader and tools/game_verify contract agree.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tools" / "FunctionalBuildingsHarness"
CONTRACT = ROOT / "tools" / "game_verify" / "contracts" / "oni_functional_buildings.v1.json"
MANAGED = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed"
)


@unittest.skipUnless(shutil.which("dotnet") and (MANAGED / "Assembly-CSharp.dll").is_file(),
                     "dotnet SDK and the installed game assemblies are required")
class FunctionalBuildingsHarnessTests(unittest.TestCase):
    def run_harness(self) -> dict:
        build = subprocess.run(
            ["dotnet", "build", str(HARNESS), "-c", "Release", "-nologo", "-v", "q"],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(build.returncode, 0, build.stdout[-2000:] + build.stderr[-2000:])
        run = subprocess.run(
            ["dotnet", str(HARNESS / "bin" / "Release" / "net8.0" / "FunctionalBuildingsHarness.dll"), str(CONTRACT)],
            capture_output=True, text=True, errors="replace",
        )
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])
        result = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(result["status"], "passed")
        return result

    def test_reader_covers_flush_toilets_and_matches_contract(self):
        cases = self.run_harness()["cases"]
        self.assertIn("supported_ids_are_game_buildings", cases)
        self.assertIn("contract_matches_reader", cases)

    def test_payload_declares_sorted_prefab_ids(self):
        self.assertIn("payload_declares_sorted_prefab_ids", self.run_harness()["cases"])
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        # The shape the Python consumer accepts: non-empty, sorted, unique.
        ids = contract["supported_prefab_ids"]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertIn("FlushToilet", ids)
        self.assertEqual(contract["optional_payload_keys"], ["prefab_ids"])


if __name__ == "__main__":
    unittest.main()
