#!/usr/bin/env python3
"""8.3 short names must not bypass the TestRoot / live-root isolation (review section 6).

Everything runs under a temporary directory.  The "live" ONI root is a fake
profile's Documents\\Klei\\OxygenNotIncluded, selected by overriding USERPROFILE
for the child process (PowerShell) or the in-process environment (Python).
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import configure_live_validation_runtime as runtime_config  # noqa: E402
from scripts import manage_live_validation_environment as environment  # noqa: E402

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
CREATE = ROOT / "scripts" / "create-named-test-clone.ps1"
RESTORE = ROOT / "scripts" / "restore-named-test-clone.ps1"


def short_path(path: pathlib.Path) -> str | None:
    if os.name != "nt":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    if not length or "~" not in buffer.value:
        return None
    return buffer.value


def oni_save_bytes() -> bytes:
    header = json.dumps({"baseName": "Alias Base", "isAutoSave": False}).encode("utf-8")
    return struct.pack("<IiIi", 744825, len(header), 1, 1) + header + b"alias-fixture-save"


class ShortPathIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="oni-short-path-")).resolve()
        self.profile = self.work / "FakeProfileWithLongName"
        self.live_oni = self.profile / "Documents" / "Klei" / "OxygenNotIncluded"
        colony = self.live_oni / "save_files" / "AliasColony"
        colony.mkdir(parents=True)
        self.source = colony / "Original.sav"
        self.source.write_bytes(oni_save_bytes())
        self.source.with_suffix(".png").write_bytes(b"alias-fixture-preview")
        self.process_fixture = self.profile / "process-idle.json"
        self.process_fixture.write_text(json.dumps({
            "schema": "oni.process-state-fixture.v1",
            "oxygen_not_included_count": 0, "bridge_count": 0,
        }), encoding="utf-8")
        alias = short_path(self.profile)
        if alias is None:
            shutil.rmtree(self.work, ignore_errors=True)
            self.skipTest("8.3 short names are not generated on this volume")
        self.alias = pathlib.Path(alias)

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def aliased(self, path: pathlib.Path) -> str:
        return str(self.alias / path.relative_to(self.profile))

    def powershell(self, script: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        if not POWERSHELL:
            self.skipTest("PowerShell is unavailable")
        env = dict(os.environ, USERPROFILE=str(self.profile))
        return subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script), *arguments],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, check=False, env=env,
        )

    def test_python_path_helpers_expand_aliases(self) -> None:
        for module in (environment, runtime_config):
            with self.subTest(module.__name__):
                self.assertTrue(module.paths_overlap(self.alias, self.profile))
                self.assertTrue(module.path_within(self.aliased(self.live_oni), self.profile))
                self.assertEqual(
                    module.path_id(self.aliased(self.live_oni), contract="lower"),
                    module.path_id(self.live_oni, contract="lower"),
                )
                # A missing tail below an aliased prefix keeps its spelling.
                self.assertEqual(
                    str(module.normalized(self.alias / "not-yet-created")).lower(),
                    str(self.profile / "not-yet-created").lower(),
                )

    def test_environment_refuses_testroot_alias_of_live_root(self) -> None:
        evidence = self.alias / "evidence"
        args = environment.build_parser().parse_args([
            "prepare", "--run-id", "alias-run",
            "--oni-root", self.aliased(self.live_oni),
            "--evidence-root", str(evidence),
            "--boundary-report-root", str(evidence / "live-boundaries"),
            "--pre-boundary", str(evidence / "pre.json"),
            "--clone-manifest", str(evidence / "clone.json"),
            "--source-save", self.aliased(self.source),
            "--loader-save", self.aliased(self.source.parent.parent / "L" / "L.sav"),
            "--loader-stem", "L",
            "--test-root", str(self.alias),
            "--what-if",
        ])
        (self.profile / "evidence" / "live-boundaries").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"USERPROFILE": str(self.profile)}):
            with self.assertRaisesRegex(environment.WorkflowError, "cannot overlap live ONI data"):
                environment.resolve_roots(args)

    def test_create_refuses_testroot_alias_of_live_root(self) -> None:
        completed = self.powershell(
            CREATE,
            "-SourceSave", self.aliased(self.source),
            "-TargetStem", "AliasClone",
            "-RunId", "alias-run",
            "-OniUserRoot", self.aliased(self.live_oni),
            "-TestRoot", str(self.alias),
            "-ProcessStateFixture", self.aliased(self.process_fixture),
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("Test root cannot overlap the live ONI user-data root", completed.stderr)
        self.assertFalse((self.live_oni / "save_files" / "AliasClone").exists())
        self.assertEqual(sorted(p.name for p in self.source.parent.iterdir()), ["Original.png", "Original.sav"])

    def test_restore_refuses_testroot_alias_of_live_root(self) -> None:
        completed = self.powershell(
            RESTORE,
            "-CloneManifest", str(self.alias / "missing-manifest.json"),
            "-SourceSave", self.aliased(self.source),
            "-TargetSave", self.aliased(self.source),
            "-ConfirmRestore",
            "-OniUserRoot", self.aliased(self.live_oni),
            "-TestRoot", str(self.alias),
            "-ProcessStateFixture", self.aliased(self.process_fixture),
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("Test root cannot overlap the live ONI user-data root", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
