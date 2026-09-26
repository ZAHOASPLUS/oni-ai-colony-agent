#!/usr/bin/env python3
"""Self-contained tests for :mod:`detect_runtime_compatibility`.

The project intentionally keeps this test runner dependency-free so it can run
on a clean ONI modding machine with ``py -3``.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

# ``py scripts/test_runtime_compatibility.py`` puts ``scripts/`` (rather than
# the project root) on ``sys.path``.  Add the root so the same test also works
# when imported as ``scripts.test_runtime_compatibility``.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.detect_runtime_compatibility import (
    _parse_version_output,
    detect_runtime,
    extract_klei_version,
    read_mod_target,
)


class RuntimeCompatibilityTests(unittest.TestCase):
    def make_fixture(self) -> tuple[pathlib.Path, pathlib.Path, bytes]:
        temp = pathlib.Path(tempfile.mkdtemp(prefix="oni-runtime-compat-test-"))
        managed = temp / "OxygenNotIncluded_Data" / "Managed"
        managed.mkdir(parents=True)
        # Include the strongly labelled fallback form.  The real ONI assembly
        # is parsed through ilspycmd; this fixture deliberately exercises the
        # conservative no-tool path as well.
        assembly_bytes = b"MZ\x00fixture\x00 build cl-765432\x00"
        assembly = managed / "Assembly-CSharp.dll"
        assembly.write_bytes(assembly_bytes)
        # The remaining files need not be valid PE files; the detector only
        # checks their presence/hash and never loads them.
        for name in (
            "0Harmony.dll",
            "Assembly-CSharp-firstpass.dll",
            "UnityEngine.CoreModule.dll",
            "Newtonsoft.Json.dll",
        ):
            (managed / name).write_bytes((name + " fixture").encode("ascii"))

        project = temp / "mod" / "OniAiProbeMod.csproj"
        project.parent.mkdir(parents=True)
        project.write_text(
            """<Project Sdk=\"Microsoft.NET.Sdk\">
  <PropertyGroup>
    <TargetFramework>netstandard2.1</TargetFramework>
    <AssemblyName>OniAiProbeMod</AssemblyName>
    <RootNamespace>OniAiProbeMod</RootNamespace>
    <ONIManagedDir>$(ONIManagedDir)</ONIManagedDir>
  </PropertyGroup>
  <ItemGroup>
    <Reference Include=\"Assembly-CSharp\"><HintPath>$(ONIManagedDir)\\Assembly-CSharp.dll</HintPath></Reference>
    <Reference Include=\"0Harmony\"><HintPath>$(ONIManagedDir)\\0Harmony.dll</HintPath></Reference>
  </ItemGroup>
</Project>
""",
            encoding="utf-8",
        )
        (temp / "mod" / "Source").mkdir()
        (temp / "mod" / "Source" / "Mod.cs").write_text(
            'internal const string ModVersion = "9.9.9-test";\n', encoding="utf-8"
        )
        (temp / "mod" / "mod_info.yaml").write_text("APIVersion: 2\n", encoding="utf-8")
        (temp / "mod" / "mod.yaml").write_text(
            'staticID: "test.OniAiProbe"\ntitle: "Fixture"\n', encoding="utf-8"
        )
        return temp, assembly, assembly_bytes

    def test_parse_version_variants(self) -> None:
        parsed = _parse_version_output(
            'public const uint ChangeList = 744825u;\n'
            'public const string BuildBranch = "release";\n'
            'public const string Patch = "hotfix";',
            source="test",
        )
        self.assertEqual(parsed["id"], "cl-744825")
        self.assertEqual(parsed["change_list"], 744825)
        self.assertEqual(parsed["branch"], "release")
        self.assertEqual(parsed["patch"], "hotfix")
        self.assertEqual(parsed["confidence"], "high")

    def test_binary_fallback_is_conservative(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-version-fallback-") as root:
            path = pathlib.Path(root) / "Assembly-CSharp.dll"
            path.write_bytes(b"MZ ChangeList\x00 744825 BuildBranch\x00 release")
            parsed, diagnostics = extract_klei_version(
                path, allow_external_tool=False
            )
            # A number near a field name is not enough to claim an association.
            self.assertIsNone(parsed["id"])
            self.assertIn("external_version_tool_disabled", diagnostics)

            path.write_bytes(b"MZ\x00build cl-744825\x00")
            parsed, _ = extract_klei_version(path, allow_external_tool=False)
            self.assertEqual(parsed["id"], "cl-744825")
            self.assertEqual(parsed["confidence"], "medium")

    def test_report_contains_build_hash_and_target_info_without_writes(self) -> None:
        root, assembly, assembly_bytes = self.make_fixture()
        try:
            before = assembly.read_bytes()
            report = detect_runtime(
                managed_dir=assembly.parent,
                project_root=root,
                allow_external_tool=False,
            )
            expected_hash = hashlib.sha256(assembly_bytes).hexdigest()
            self.assertEqual(report["schema"], "oni.runtime-compatibility.v1")
            self.assertTrue(report["read_only"])
            self.assertEqual(report["assembly_csharp_sha256"], expected_hash)
            self.assertEqual(report["assembly_csharp_hash"], f"sha256:{expected_hash}")
            self.assertEqual(report["status"], "compatible")
            target = report["mod_target_info"]
            self.assertEqual(target["target_framework"], "netstandard2.1")
            self.assertEqual(target["target_framework_status"], "supported")
            self.assertEqual(target["mod_version"], "9.9.9-test")
            self.assertEqual(target["static_id"], "test.OniAiProbe")
            self.assertEqual(target["api_version"], 2)
            self.assertTrue(target["managed_dir_match"])
            self.assertEqual(before, assembly.read_bytes())
        finally:
            # The fixture is outside the project/ONI tree; cleanup is safe and
            # keeps repeated local runs tidy.
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_expected_pins_are_optional_and_dynamic(self) -> None:
        root, assembly, _ = self.make_fixture()
        try:
            report = detect_runtime(
                managed_dir=assembly.parent,
                project_root=root,
                allow_external_tool=False,
                expected_build="765433",
            )
            self.assertEqual(report["policy"]["expected_build"], "cl-765433")
            self.assertTrue(
                any(
                    check["id"] == "expected_build_matches" and not check["ok"]
                    for check in report["checks"]
                )
            )
            self.assertEqual(report["status"], "incompatible")
            # Supplying an exact pin turns that comparison into a hard policy
            # gate; without ``--expected-build`` the detector remains dynamic.
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_missing_runtime_is_incompatible_but_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oni-missing-runtime-") as root:
            report = detect_runtime(
                managed_dir=pathlib.Path(root) / "does-not-exist",
                project_root=pathlib.Path(root),
                allow_external_tool=False,
            )
            self.assertEqual(report["status"], "incompatible")
            self.assertFalse(report["ok"])
            # Ensure no non-JSON values leak into a shell/bridge response.
            json.dumps(report)

    def test_mod_target_can_be_read_in_isolation(self) -> None:
        root, assembly, _ = self.make_fixture()
        try:
            target = read_mod_target(
                project_root=root, managed_dir=assembly.parent
            )
            names = {entry["name"] for entry in target["references"]}
            self.assertEqual(names, {"Assembly-CSharp", "0Harmony"})
            self.assertTrue(all(entry["present"] for entry in target["references"]))
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
