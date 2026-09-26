#!/usr/bin/env python3
"""Tests for the start-live-profile.ps1 ForbiddenRoot guard.

Only the read-only ``-Action Inventory`` path is exercised, without
``-GameExe``: it inspects the current profile and prints JSON, and never starts
Steam or ONI, writes a state file or touches Klei user data.  The Bridge
directory is a temporary directory inside the profile (under %TEMP%).
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
SCRIPT = ROOT / "scripts" / "start-live-profile.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
PROFILE = pathlib.Path(os.environ.get("USERPROFILE") or "")


def short_path(path: pathlib.Path) -> str | None:
    """Return the 8.3 alias of an existing path, or None when there is none."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    except (AttributeError, OSError):
        return None
    if not length or buffer.value.lower() == str(path).lower():
        return None
    return buffer.value


@unittest.skipUnless(POWERSHELL and os.name == "nt" and PROFILE.is_dir(), "Windows PowerShell and USERPROFILE required")
class ForbiddenRootTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = pathlib.Path(tempfile.gettempdir()).resolve()
        if not str(temp).lower().startswith(str(PROFILE.resolve()).lower() + os.sep):
            self.skipTest("%TEMP% is not inside USERPROFILE")
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="oni-live-profile-test-")).resolve()
        self.bridge = self.work / "BridgeDirectoryWithLongName"
        self.bridge.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def inventory(self, forbidden_root: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
                "-Action", "Inventory", "-BridgeDir", str(self.bridge),
                "-ForbiddenRoot", forbidden_root,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )

    def assert_refused(self, forbidden_root: str) -> None:
        completed = self.inventory(forbidden_root)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("actual profile overlaps ForbiddenRoot", completed.stderr)
        self.assertFalse((self.bridge / "live-profile-process.json").exists())

    def test_profile_itself_is_refused(self) -> None:
        self.assert_refused(str(PROFILE))

    def test_profile_with_trailing_separator_and_dotdot_is_refused(self) -> None:
        self.assert_refused(str(PROFILE / "Documents" / "..") + "\\")

    def test_ancestor_of_profile_is_refused(self) -> None:
        self.assert_refused(str(PROFILE.parent))

    def test_descendant_of_profile_is_refused(self) -> None:
        self.assert_refused(str(PROFILE / "Documents"))

    def test_short_name_alias_of_descendant_is_refused(self) -> None:
        alias = short_path(self.bridge)
        if alias is None:
            self.skipTest("8.3 short names are not generated on this volume")
        self.assert_refused(alias)

    def test_path_comparison_expands_short_names(self) -> None:
        alias = short_path(self.bridge)
        if alias is None:
            self.skipTest("8.3 short names are not generated on this volume")
        # Load only the script's function definitions (its body needs a
        # profile and would run the guard) and compare the alias directly.
        command = (
            "$ErrorActionPreference='Stop';$t=$null;$e=$null;"
            f"$ast=[Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',[ref]$t,[ref]$e);"
            "foreach($f in $ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst]},$false)){"
            ". ([scriptblock]::Create($f.Extent.Text))};"
            f"[pscustomobject]@{{norm=(Norm '{alias}');same=(Same '{alias}' '{self.bridge}');"
            f"desc=(Desc '{alias}\\x' '{self.bridge}')}}|ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout.lstrip("\ufeff"))
        self.assertEqual(payload["norm"].lower(), str(self.bridge).lower())
        self.assertIs(payload["same"], True)
        self.assertIs(payload["desc"], True)

    def test_unrelated_sibling_root_is_allowed(self) -> None:
        # A sibling whose name only shares a string prefix with the profile.
        completed = self.inventory(str(PROFILE) + "-unrelated-sibling")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout.lstrip("\ufeff"))
        self.assertEqual(payload["schema"], "oni.live-profile-inventory.v1")


if __name__ == "__main__":
    unittest.main()
