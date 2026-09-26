#!/usr/bin/env python3
"""Exercise real C# blueprint field gates and their Python counterparts offline.

The production CommandFieldContracts.cs and CanonicalAlertCatalog.cs are copied
byte-for-byte into a private temporary executable project. Only the unrelated
CanonicalAlertReader.Source constant is stubbed; no blueprint validation code
is substituted. The harness invokes the private ValueMatchesPath via reflection
and the real DeclarationMatches/ValidateCurrentState methods directly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from xml.sax.saxutils import escape


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import field_contracts, state_quality  # noqa: E402


CONTRACT = ROOT / "mod/Source/Contracts/CommandFieldContracts.cs"
ALERT_CATALOG = ROOT / "mod/Source/Probe/CanonicalAlertCatalog.cs"
REPORT = ROOT / "reports/blueprint-dynamic-contracts-verification.json"
DEFAULT_JSON_DLL = pathlib.Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded"
    r"\OxygenNotIncluded_Data\Managed\Newtonsoft.Json.dll"
)
INT_MAX = 2**31 - 1
INT64_MAX = 2**63 - 1
MISSING = object()


HARNESS = r'''
using System;
using System.IO;
using System.Reflection;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using OniAiProbeMod.Contracts;

namespace OniAiProbeMod.Probe
{
    // This constant initializes an unrelated alert contract. No alert branch
    // is exercised and no game, Probe, or blueprint API is stubbed here.
    internal static class CanonicalAlertReader
    {
        internal const string Source = "offline-harness-unexercised-alert-source";
    }
}

internal static class Program
{
    private static int Main(string[] args)
    {
        JArray cases = JArray.Parse(File.ReadAllText(args[0]));
        MethodInfo valueMethod = typeof(CommandFieldContracts).GetMethod(
            "ValueMatchesPath", BindingFlags.NonPublic | BindingFlags.Static);
        if (valueMethod == null)
            throw new InvalidOperationException("Production ValueMatchesPath is missing.");
        JArray results = new JArray();
        foreach (JObject item in cases)
        {
            string path = item["path"].Value<string>();
            JObject condition = (JObject)item["condition"];
            JObject state = (JObject)item["state"];
            // Freshness is assessed at the real executor boundary, not against
            // the time spent compiling this isolated executable.
            state["ts"] = DateTimeOffset.UtcNow.ToString("o");
            JObject result = new JObject { ["name"] = item["name"] };
            try
            {
                result["value_matches"] = (bool)valueMethod.Invoke(
                    null, new object[] { path, item["actual"] });
                result["declaration_matches"] = CommandFieldContracts.DeclarationMatches(
                    condition, out string declarationError);
                result["current_state_matches"] = CommandFieldContracts.ValidateCurrentState(
                    state, condition, item["actual"], out string currentError);
                result["declaration_error"] = declarationError;
                result["current_error"] = currentError;
                result["exception"] = null;
            }
            catch (Exception ex)
            {
                result["exception"] = ex.GetBaseException().GetType().FullName;
            }
            results.Add(result);
        }
        Console.WriteLine(results.ToString(Formatting.None));
        return 0;
    }
}
'''


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put_path(state: dict[str, Any], path: str, value: Any) -> None:
    cursor = state
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def make_case(
    name: str, path: str, actual: Any, accepted: bool,
    *, op: str = "eq", operand: Any = MISSING,
    declaration: bool | None = None, wrong_contract: bool = False,
    wrong_quality: bool = False,
) -> dict[str, Any]:
    contract = field_contracts.contract_for_path(path)
    contract_id = contract.contract_id if contract is not None else "oni.qc.blueprints.instance.v1"
    state: dict[str, Any] = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(), "quality": {},
    }
    put_path(state, path, actual)
    if contract is not None:
        state["quality"][path] = {
            "status": "observed", "source": contract.sources[0],
            "semantic": contract.semantics[0], "unit": contract.units[0],
            "scope": contract.scopes[0], "completeness": contract.completeness[0],
            "captured_at": "snapshot.ts", "contract_id": contract_id,
        }
        if wrong_quality:
            state["quality"][path]["unit"] = "guessed"
    condition = {
        "path": path, "op": op, "value": actual if operand is MISSING else operand,
        "quality": ["observed"],
        "quality_contract_id": "oni.qc.unknown.v1" if wrong_contract else contract_id,
    }
    expected_declaration = accepted if declaration is None else declaration
    return {
        "name": name, "path": path, "actual": actual,
        "condition": condition, "state": state,
        "expected": {
            "value_matches": accepted,
            "declaration_matches": expected_declaration,
            "current_state_matches": accepted and expected_declaration and not wrong_quality,
        },
    }


def vectors() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    fields = {
        "class": ("blueprint_priority_class", "priority_class"),
        "priority": ("blueprint_priority_value", "priority_value"),
        "progress": ("blueprint_progress_percent", "progress_percent"),
        "worker": ("blueprint_worker_instance_id", "worker_instance_id"),
        "root": ("blueprint_root_cell", "root_cell"),
        "footprint": ("blueprint_footprint_cells", "footprint_cells"),
    }
    values = {
        "class": [(name, name, True) for name in
                  ("idle", "basic", "high", "personalNeeds", "topPriority", "compulsory")]
                 + [("empty", "", False), ("unknown", "bogus", False),
                    ("case_changed", "Basic", False), ("number", 0, False),
                    ("boolean", True, False), ("null", None, False)],
        "priority": [("minimum", 1, True), ("middle", 5, True), ("maximum", 9, True),
                     ("below", 0, False), ("above", 10, False), ("negative", -1, False),
                     ("float", 1.0, False), ("boolean", True, False),
                     ("string", "1", False), ("null", None, False),
                     ("int64_maximum", INT64_MAX, False)],
        "progress": [("zero", 0, True), ("one", 1, True), ("maximum", 100, True),
                     ("below", -1, False), ("above", 101, False), ("float", 0.0, False),
                     ("boolean", False, False), ("string", "0", False),
                     ("null", None, False), ("int64_maximum", INT64_MAX, False)],
        "worker": [("zero", 0, True), ("positive", 17, True), ("maximum", INT_MAX, True),
                   ("below", -1, False), ("above", INT_MAX + 1, False),
                   ("float", 0.0, False), ("boolean", False, False),
                   ("string", "0", False), ("null", None, False),
                   ("int64_maximum", INT64_MAX, False)],
        "footprint": [("single_zero", [0], True), ("single_maximum", [INT_MAX], True),
                      ("sorted", [0, 3, 7], True), ("maximum_count", list(range(64)), True),
                      ("empty", [], False), ("too_many", list(range(65)), False),
                      ("duplicate", [0, 0], False), ("unsorted", [7, 3], False),
                      ("negative", [-1], False), ("above", [INT_MAX + 1], False),
                      ("float", [1.0], False), ("boolean", [True], False),
                      ("string_item", ["1"], False), ("null_item", [None], False),
                      ("null", None, False), ("string", "[1]", False),
                      ("int64_maximum", [INT64_MAX], False)],
    }
    values["root"] = values["worker"]
    for kind, pair in fields.items():
        for side, field in zip(("cell", "instance"), pair):
            prefix = "cells.by_id.cell_10" if side == "cell" else "blueprints.by_instance_id.id_7"
            path = f"{prefix}.{field}"
            for label, value, accepted in values[kind]:
                cases.append(make_case(f"{side}_{kind}_{label}", path, value, accepted))
            if kind in {"priority", "progress", "worker", "root"}:
                value = 5 if kind == "priority" else 0
                for op in ("ne", "lt", "lte", "gt", "gte"):
                    cases.append(make_case(f"{side}_{kind}_{op}", path, value, True, op=op))
                cases.append(make_case(f"{side}_{kind}_contains_rejected", path, value, True,
                                       op="contains", declaration=False))
            elif kind == "class":
                cases.append(make_case(f"{side}_{kind}_ne", path, "basic", True, op="ne"))
                cases.append(make_case(f"{side}_{kind}_ordered_rejected", path, "basic", True,
                                       op="lt", declaration=False))
                cases.append(make_case(f"{side}_{kind}_contains_rejected", path, "basic", True,
                                       op="contains", declaration=False))
            else:
                cases.append(make_case(f"{side}_{kind}_ne", path, [0, 1], True, op="ne"))
                for label, operand, accepted in (
                    ("zero", 0, True), ("maximum", INT_MAX, True),
                    ("negative", -1, False), ("above", INT_MAX + 1, False),
                    ("float", 0.0, False), ("boolean", False, False),
                    ("string", "0", False), ("array", [0], False),
                    ("null", None, False), ("int64_maximum", INT64_MAX, False),
                ):
                    cases.append(make_case(f"{side}_{kind}_contains_{label}", path, [0], True,
                                           op="contains", operand=operand, declaration=accepted))
                cases.append(make_case(f"{side}_{kind}_ordered_rejected", path, [0], True,
                                       op="gt", declaration=False))
    priority_path = "blueprints.by_instance_id.id_7.priority_value"
    cases.extend([
        make_case("wrong_contract", priority_path, 5, True, declaration=False, wrong_contract=True),
        make_case("wrong_quality", priority_path, 5, True, wrong_quality=True),
        make_case("unknown_operator", priority_path, 5, True, op="guess", declaration=False),
        make_case("unknown_path", "blueprints.by_instance_id.id_7.priority_guess", 5, False),
        make_case("legacy_paused", "game.paused", True, True),
        make_case("legacy_paused_integer_rejected", "game.paused", 1, False),
        make_case("legacy_instance", "blueprints.by_instance_id.id_7.instance_id", 7, True),
        make_case("legacy_fingerprint", "cells.by_id.cell_10.fingerprint", "sha256:" + "a" * 64, True),
        make_case("legacy_materials", "blueprints.by_instance_id.id_7.material_tags", ["Copper"], True),
    ])
    if len({case["name"] for case in cases}) != len(cases):
        raise AssertionError("duplicate vector name")
    return cases


def python_results(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for case in cases:
        value_blockers = field_contracts.field_contract_blockers(case["state"], case["path"])
        # A deliberately corrupted quality entry does not change wire validity.
        value_state = json.loads(json.dumps(case["state"]))
        if case["name"] == "wrong_quality":
            value_state["quality"][case["path"]]["unit"] = "field_native"
        results.append({
            "name": case["name"],
            "value_matches": not field_contracts.field_contract_blockers(value_state, case["path"]),
            "declaration_matches": not field_contracts.condition_declaration_blockers(case["condition"]),
            "current_state_matches": not field_contracts.condition_contract_blockers(case["state"], case["condition"]),
            "value_blockers": value_blockers,
        })
    return results


def run_process(args: list[str], cwd: pathlib.Path) -> dict[str, Any]:
    started = time.monotonic()
    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
    result = subprocess.run(args, cwd=cwd, env=env, text=True, encoding="utf-8",
                            errors="replace", capture_output=True, timeout=30, check=False)
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
            "duration_seconds": round(time.monotonic() - started, 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=pathlib.Path, default=REPORT)
    parser.add_argument("--newtonsoft-json", type=pathlib.Path, default=DEFAULT_JSON_DLL)
    args = parser.parse_args()
    report_path = args.report.resolve()
    if not report_path.is_relative_to((ROOT / "reports").resolve()):
        raise ValueError("report must stay inside this workspace reports directory")
    files = {
        "csharp_contract": CONTRACT,
        "alert_catalog": ALERT_CATALOG,
        "python_contract": pathlib.Path(field_contracts.__file__).resolve(),
        "python_quality": pathlib.Path(state_quality.__file__).resolve(),
        "verifier": pathlib.Path(__file__).resolve(),
        "newtonsoft_json": args.newtonsoft_json.resolve(),
    }
    if files["python_contract"] != ROOT / "agent/field_contracts.py":
        raise RuntimeError("Python contract import escaped current workspace")
    source_bytes = {name: path.read_bytes() for name, path in files.items()}
    hashes_before = {name: sha256(value) for name, value in source_bytes.items()}
    report: dict[str, Any] = {
        "schema": "oni.blueprint-dynamic-contracts-verification.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "failed", "offline_only": True, "live_admission": False, "action_types": [],
        "source_paths": {name: str(path) for name, path in files.items()},
        "hashes_before": hashes_before,
        "harness": {"production_contract_copied_byte_for_byte": True,
                    "unexercised_stub": "CanonicalAlertReader.Source only",
                    "nuget_package_sources": [], "temporary_cleanup_verified": False},
    }
    temp_path: pathlib.Path | None = None
    try:
        dotnet = shutil.which("dotnet")
        if not dotnet:
            raise RuntimeError("dotnet executable is unavailable")
        sdk = run_process([dotnet, "--version"], ROOT)
        if sdk["returncode"]:
            raise RuntimeError("dotnet SDK query failed")
        sdk_version = sdk["stdout"].strip()
        major = int(sdk_version.split(".", 1)[0])
        if major < 8:
            raise RuntimeError("harness requires a locally installed .NET SDK 8 or newer")
        framework = f"net{major}.0"
        report["harness"].update({"sdk_version": sdk_version, "target_framework": framework})
        cases = vectors()
        py_results = python_results(cases)
        with tempfile.TemporaryDirectory(prefix="oni-blueprint-dynamic-") as temporary:
            temp_path = pathlib.Path(temporary).resolve()
            if not temp_path.is_relative_to(pathlib.Path(tempfile.gettempdir()).resolve()):
                raise RuntimeError("temporary harness escaped the system temporary directory")
            (temp_path / "CommandFieldContracts.cs").write_bytes(source_bytes["csharp_contract"])
            (temp_path / "CanonicalAlertCatalog.cs").write_bytes(source_bytes["alert_catalog"])
            (temp_path / "Program.cs").write_text(HARNESS, encoding="utf-8")
            (temp_path / "vectors.json").write_text(json.dumps(cases, allow_nan=False), encoding="utf-8")
            (temp_path / "NuGet.Config").write_text(
                '<configuration><packageSources><clear /></packageSources></configuration>', encoding="utf-8")
            (temp_path / "Harness.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                f'<TargetFramework>{framework}</TargetFramework><Nullable>disable</Nullable>'
                '<LangVersion>latest</LangVersion></PropertyGroup><ItemGroup>'
                '<Reference Include="Newtonsoft.Json"><HintPath>'
                + escape(str(files["newtonsoft_json"]))
                + '</HintPath><Private>true</Private></Reference></ItemGroup></Project>', encoding="utf-8")
            build = run_process([dotnet, "build", "Harness.csproj", "-c", "Release", "--nologo",
                                 "--configfile", "NuGet.Config", "--disable-build-servers"], temp_path)
            report["harness"]["build"] = {key: value for key, value in build.items() if key not in {"stdout", "stderr"}}
            if build["returncode"]:
                report["harness"]["build"]["output_tail"] = (build["stdout"] + build["stderr"])[-4000:]
                raise RuntimeError("C# harness build failed")
            executable = temp_path / "bin/Release" / framework / "Harness.dll"
            report["harness"]["assembly_sha256"] = sha256(executable.read_bytes())
            run = run_process([dotnet, str(executable), str(temp_path / "vectors.json")], temp_path)
            report["harness"]["run"] = {key: value for key, value in run.items() if key not in {"stdout", "stderr"}}
            if run["returncode"]:
                report["harness"]["run"]["output_tail"] = (run["stdout"] + run["stderr"])[-4000:]
                raise RuntimeError("C# harness execution failed")
            cs_results = json.loads(run["stdout"])
        report["harness"]["temporary_cleanup_verified"] = temp_path is not None and not temp_path.exists()
        if [item["name"] for item in cs_results] != [case["name"] for case in cases]:
            raise AssertionError("C# case order/count differs from input")
        failures = []
        matched = 0
        for case, py_result, cs_result in zip(cases, py_results, cs_results):
            mismatches = {
                field: {"expected": expected, "python": py_result[field], "csharp": cs_result.get(field)}
                for field, expected in case["expected"].items()
                if py_result[field] is not expected or cs_result.get(field) is not expected
            }
            if mismatches or cs_result.get("exception"):
                failures.append({"name": case["name"], "path": case["path"], "mismatches": mismatches,
                                 "csharp_exception": cs_result.get("exception")})
            else:
                matched += 1
        report.update({"case_count": len(cases), "matched_case_count": matched,
                       "assertion_count": len(cases) * 6,
                       "failures": failures,
                       "case_names": [case["name"] for case in cases]})
        if not failures and report["harness"]["temporary_cleanup_verified"]:
            report["status"] = "passed"
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
    if temp_path is not None:
        report["harness"]["temporary_cleanup_verified"] = not temp_path.exists()
    hashes_after = {name: sha256(path.read_bytes()) for name, path in files.items()}
    report["hashes_after"] = hashes_after
    report["source_unchanged"] = hashes_before == hashes_after
    if not report["source_unchanged"]:
        report["status"] = "failed"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "case_count": report.get("case_count", 0),
                      "matched_case_count": report.get("matched_case_count", 0),
                      "failure_count": len(report.get("failures", [])), "error": report.get("error"),
                      "source_unchanged": report["source_unchanged"], "report": str(report_path)}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
