#!/usr/bin/env python3
"""Fast fixture-only tests for the runtime-bootstrap publication runner.

These tests never import or execute the production verifier and never launch
ONI. Every candidate canonical is inside a Unicode/space-containing TestRoot,
except the provenance-refusal test which proves a fake source cannot target the
production canonical.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import marshal
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent
RUNNER = SCRIPTS / "run_live_runtime_bootstrap_verification.ps1"
WORKER = SCRIPTS / "run_live_runtime_bootstrap_verification_worker.py"
PRODUCTION_VERIFIER = SCRIPTS / "verify_live_runtime_bootstrap.py"
PRODUCTION_MANAGER = SCRIPTS / "manage_live_runtime_bootstrap.py"
PRODUCTION_CANONICAL = PROJECT_ROOT / "reports" / "live-runtime-bootstrap-verification.json"
REQUESTED_POWERSHELL = os.environ.get("ONI_RUNNER_POWERSHELL")
POWERSHELL = (
    shutil.which(REQUESTED_POWERSHELL)
    if REQUESTED_POWERSHELL
    else (shutil.which("pwsh") or shutil.which("powershell"))
)

STATIC_NAMES = [
    "manager_has_no_build_and_workflow_has_no_game_launch_surface",
    "process_census_no_self_match",
    "process_get_process_failure_closed",
    "process_query_no_source_text_self_match",
    "process_query_exact_bridge_argv_counted",
    "process_query_bool_pid_refused",
    "legacy_path_0_and_1_unit_boundaries",
    "verifier_injected_marker_legacy_path_regression",
    "legacy_path_budget_enumerates_all_transaction_transients",
    "legacy_path_259_pass_260_fail",
]
ROUND_NAMES = [f"fixture-scenario-{index:03d}" for index in range(247)]
SENTINEL = b"fixture canonical sentinel\n"
CROCKFORD_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
EXECUTION_ROOT_NAME = re.compile(
    r"^r[0123456789abcdefghjkmnpqrstvwxyz]{25}[048cgmrw]$"
)
LEGACY_EXECUTION_ROOT_NAME = re.compile(r"^oni-rtb-run-[0-9a-f]{32}$")
PATH_BUDGET_ORIGINAL_SCENARIO = "r1-abort-gate-8f6325b5da"
PATH_BUDGET_COMPACT_SCENARIO = "r1-abort--95d168fbf3"
PATH_BUDGET_SHORT_UNITS_OFFSET_FROM_TEMP_PARENT = 217
PATH_BUDGET_LEGACY_UNITS_OFFSET_FROM_TEMP_PARENT = 234
PATH_BUDGET_UNIT_LIMIT = 259


def utf16_units(path: Path) -> int:
    return len(str(path).encode("utf-16-le")) // 2


def reference_crockford_base32(payload: bytes) -> str:
    if len(payload) != 16:
        raise ValueError("reference input must contain exactly 16 bytes")
    padded = int.from_bytes(payload, "big") << 2
    return "".join(
        CROCKFORD_ALPHABET[(padded >> shift) & 31]
        for shift in range(125, -1, -5)
    )


def decode_canonical_crockford_base32(token: str) -> bytes:
    if len(token) != 26 or any(character not in CROCKFORD_ALPHABET for character in token):
        raise ValueError("token is not lowercase Crockford Base32")
    value = 0
    for character in token:
        value = (value << 5) | CROCKFORD_ALPHABET.index(character)
    if value & 3:
        raise ValueError("token has noncanonical padding bits")
    return (value >> 2).to_bytes(16, "big")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            sha256(path),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def write_matching_timestamp_pyc(source: Path, payload: str) -> Path:
    """Write a header-compatible adjacent cache without invoking py_compile."""
    source_stat = source.stat()
    cache_path = (
        source.parent
        / "__pycache__"
        / f"{source.stem}.{sys.implementation.cache_tag}.pyc"
    )
    cache_path.parent.mkdir(exist_ok=True)
    code = compile(payload, str(source), "exec")
    header = importlib.util.MAGIC_NUMBER + struct.pack(
        "<III",
        0,
        int(source_stat.st_mtime) & 0xFFFFFFFF,
        source_stat.st_size & 0xFFFFFFFF,
    )
    cache_path.write_bytes(header + marshal.dumps(code))
    return cache_path


def remove_owned_execution_root(
    path: Path,
    expected_root_identity: tuple[int, int],
    expected_temp_identity: tuple[int, int],
) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    temp_stat = temp_root.stat()
    if (temp_stat.st_dev, temp_stat.st_ino) != expected_temp_identity:
        raise AssertionError("refusing cleanup after TEMP parent identity changed")
    absolute = path.absolute()
    if absolute.parent.resolve(strict=True) != temp_root:
        raise AssertionError(f"refusing execution-root cleanup outside TEMP: {absolute}")
    if not EXECUTION_ROOT_NAME.fullmatch(absolute.name):
        raise AssertionError(f"refusing malformed execution-root cleanup target: {absolute}")
    if LEGACY_EXECUTION_ROOT_NAME.fullmatch(absolute.name):
        raise AssertionError(f"refusing legacy execution-root cleanup target: {absolute}")
    if not absolute.is_dir():
        return
    root_stat = absolute.lstat()
    if (root_stat.st_dev, root_stat.st_ino) != expected_root_identity:
        raise AssertionError("refusing execution-root cleanup after root identity changed")
    if getattr(root_stat, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    ):
        raise AssertionError("refusing reparse-point execution root cleanup")
    for current, directory_names, file_names in os.walk(
        absolute,
        topdown=True,
        followlinks=False,
    ):
        items = [Path(current)] + [Path(current) / name for name in directory_names + file_names]
        for item in items:
            attributes = getattr(item.lstat(), "st_file_attributes", 0)
            if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise AssertionError(f"refusing reparse point in execution-root cleanup: {item}")
    shutil.rmtree(absolute)


def render_verifier(
    verifier: Path,
    manager: Path,
    canonical: Path,
    behavior: str,
    wrong_core: Path | None = None,
) -> str:
    core_path = wrong_core or manager
    return textwrap.dedent(
        f"""
        from __future__ import annotations
        import importlib.util
        import json
        import os
        import pathlib
        import shutil
        import subprocess
        import sys
        import tempfile
        import time

        if __name__ not in sys.modules:
            raise RuntimeError("spec module was not registered in sys.modules")

        MANAGER = pathlib.Path({str(manager)!r})
        CORE_PATH = pathlib.Path({str(core_path)!r})
        _core_spec = importlib.util.spec_from_file_location("_oni_fixture_manager", CORE_PATH)
        if _core_spec is None or _core_spec.loader is None:
            raise RuntimeError("fixture manager spec could not be created")
        core = importlib.util.module_from_spec(_core_spec)
        sys.modules[_core_spec.name] = core
        _core_spec.loader.exec_module(core)
        DEFAULT_REPORT = pathlib.Path(__file__).with_name("default-report-must-not-exist.json")
        REPORT = DEFAULT_REPORT
        STATIC_NAMES = {STATIC_NAMES!r}
        ROUND_NAMES = {ROUND_NAMES!r}
        BEHAVIOR = {behavior!r}
        CANONICAL = pathlib.Path({str(canonical)!r})

        def build_report():
            names = list(ROUND_NAMES)
            if BEHAVIOR == "wrong_names":
                names[-1] = "forged-scenario-name"
            elif BEHAVIOR == "wrong_order":
                names[0], names[1] = names[1], names[0]
            rounds = []
            for round_number in (1, 2):
                scenarios = []
                for index, name in enumerate(names):
                    item = {{"name": name, "status": "passed"}}
                    if index % 31 == 0:
                        item["classification"] = "fixture_pass"
                    scenarios.append(item)
                if BEHAVIOR == "fake_count" and round_number == 2:
                    scenarios.pop()
                rounds.append({{
                    "round": round_number,
                    "status": "passed",
                    "scenario_count": 247,
                    "scenarios": scenarios,
                }})
            return {{
                "schema": "oni.live-runtime-bootstrap-verification.v1",
                "status": "passed",
                "classification": "static_probe_and_testroot_fault_injection",
                "round_count": 2,
                "required_round_count": 2,
                "scenario_count": 504,
                "assertion_count": 4032,
                "duration_seconds": 0.125,
                "static_scenarios": [
                    {{"name": name, "status": "passed"}} for name in STATIC_NAMES
                ],
                "rounds": rounds,
                "failure": None,
                "preserved_test_root": None,
                "game_launched": False,
                "real_klei_writes": False,
            }}

        def main():
            expected_argv = [str(pathlib.Path(__file__).resolve()), "--rounds", "2"]
            if sys.argv != expected_argv:
                raise RuntimeError(f"wrong argv: {{sys.argv!r}}")
            report_path = pathlib.Path(REPORT).resolve()
            if report_path == DEFAULT_REPORT.resolve() or report_path.exists():
                raise RuntimeError("REPORT was not redirected to a fresh external path")
            if pathlib.Path(tempfile.gettempdir()).resolve() != report_path.parent.resolve():
                raise RuntimeError("verifier tempdir is not grouped with worker evidence")
            print("fixture stdout Ω 中文; not pure JSON", flush=True)
            print("fixture stderr Ω 中文", file=sys.stderr, flush=True)
            if BEHAVIOR == "source_lock_probe":
                for label, target in (("verifier", pathlib.Path(__file__)), ("manager", MANAGER)):
                    original = target.read_bytes()
                    try:
                        target.write_bytes(original + b"# forbidden drift\\n")
                    except OSError as exc:
                        print(f"source-lock-blocked {{label}} write winerror={{getattr(exc, 'winerror', None)}}", flush=True)
                    else:
                        raise RuntimeError(f"source lock did not block {{label}} write")
                    if target.read_bytes() != original:
                        raise RuntimeError(f"source bytes changed after blocked {{label}} write")
                replacement = report_path.parent / "replacement verifier.py"
                replacement.write_text("REPLACEMENT = True\\n", encoding="utf-8")
                try:
                    os.replace(replacement, pathlib.Path(__file__))
                except OSError as exc:
                    print(f"source-lock-blocked verifier replace winerror={{getattr(exc, 'winerror', None)}}", flush=True)
                else:
                    raise RuntimeError("source lock did not block verifier replace")
                replacement.unlink(missing_ok=True)
            if BEHAVIOR == "spawn_child_valid":
                subprocess.Popen(
                    [sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                    close_fds=True,
                )
            if BEHAVIOR == "malicious_adjacent_pyc":
                child_result = report_path.parent / "child-manager-result.txt"
                subprocess.run(
                    [sys.executable, str(MANAGER), str(child_result)],
                    check=True,
                    close_fds=True,
                )
                if child_result.read_text(encoding="utf-8") != "source":
                    raise RuntimeError("manager child executed adjacent cached code")
            if BEHAVIOR == "hang":
                time.sleep(30)
            if BEHAVIOR == "log_flood":
                sys.stdout.write("X" * (9 * 1024 * 1024))
                sys.stdout.flush()
                time.sleep(30)
            if BEHAVIOR == "report_oversize_hang":
                report_path.write_bytes(b'{{"oversize":"' + b"R" * (33 * 1024 * 1024) + b'"}}')
                time.sleep(30)
            if BEHAVIOR == "root_oversize_hang":
                with (report_path.parent / "oversize sparse.bin").open("wb") as stream:
                    stream.truncate(65 * 1024 * 1024)
                time.sleep(30)
            if BEHAVIOR in ("resource_monitor_byte_limit_64mib", "resource_monitor_byte_limit_64mib_plus_one"):
                def _execution_root_file_bytes() -> int:
                    total_bytes = 0
                    for current, _, files in os.walk(report_path.parent):
                        for name in files:
                            total_bytes += (pathlib.Path(current) / name).stat().st_size
                    return total_bytes

                stable_bytes = None
                for _ in range(40):
                    current_bytes = _execution_root_file_bytes()
                    if current_bytes == stable_bytes:
                        break
                    stable_bytes = current_bytes
                    time.sleep(0.025)
                else:
                    raise RuntimeError("execution-root ordinary file bytes did not stabilize")

                target_bytes = 64 * 1024 * 1024
                if BEHAVIOR == "resource_monitor_byte_limit_64mib_plus_one":
                    target_bytes += 1
                padding_bytes = target_bytes - current_bytes
                if padding_bytes <= 0:
                    raise RuntimeError("byte-limit fixture already exceeded its target")
                padding_path = report_path.parent / "fixture-resource-monitor-byte-limit.bin"
                with padding_path.open("xb") as stream:
                    stream.truncate(padding_bytes)
                if _execution_root_file_bytes() != target_bytes:
                    raise RuntimeError("byte-limit fixture did not reach its exact target")
                time.sleep(1.35)
                if BEHAVIOR == "resource_monitor_byte_limit_64mib":
                    if _execution_root_file_bytes() != target_bytes:
                        raise RuntimeError("byte-limit fixture drifted before cleanup")
                    padding_path.unlink()
            if BEHAVIOR == "resource_monitor_nested_rename":
                archive_root = (
                    report_path.parent
                    / "fixture-resource-monitor-rename-source"
                    / "runtime-bootstrap"
                    / "stage"
                    / "archive-root"
                )
                archive_root.mkdir(parents=True)
                (archive_root / "old-state.json").write_bytes(
                    b"R" * (1024 * 1024 + 37)
                )
                (report_path.parent / "fixture-resource-monitor-rename-ready.txt").write_text(
                    "ready\\n", encoding="utf-8"
                )
                time.sleep(1.35)
            if BEHAVIOR == "resource_monitor_io_failure":
                fault_root = report_path.parent / "fixture-resource-monitor-io-failure"
                fault_root.mkdir()
                (fault_root / "payload.bin").write_bytes(b"fixture")
                time.sleep(1.35)
            if BEHAVIOR in ("resource_monitor_entry_limit_10000", "resource_monitor_entry_limit_10001"):
                entry_root = report_path.parent / "fixture-resource-monitor-entry-limit"
                entry_root.mkdir()
                def _count_execution_entries() -> int:
                    total_entries = 0
                    for _, directories, files in os.walk(report_path.parent):
                        total_entries += len(directories) + len(files)
                    return total_entries

                # The worker report and attestation are the two entries still to be
                # created after this fixture block. Keep the final root at the exact
                # boundary without relying on file bytes to trip the monitor.
                if BEHAVIOR == "resource_monitor_entry_limit_10000":
                    desired_entries = 10000
                else:
                    desired_entries = 10001
                target_entries = desired_entries - _count_execution_entries() - 2
                if target_entries < 0:
                    raise RuntimeError("entry-limit fixture already exceeded its target")
                for index in range(target_entries):
                    (entry_root / f"{{index:05d}}-dir").mkdir()
                time.sleep(1.35)
            if BEHAVIOR in ("resource_monitor_churn_tolerated", "resource_monitor_churn_unstable"):
                churn_root = report_path.parent / "fixture-resource-monitor-churn"
                churn_root.mkdir()
                for index in range(64):
                    (churn_root / f"fixture-resource-monitor-churn-{{index:02d}}.bin").write_bytes(
                        b"fixture"
                    )
                time.sleep(1.35)
            if BEHAVIOR == "slow_valid":
                time.sleep(1.35)
            if BEHAVIOR == "static_exception":
                raise RuntimeError("fixture static contract exception")
            if BEHAVIOR == "missing_report":
                return 0

            path_budget_failed = False
            path_budget_root = None
            if BEHAVIOR == "path_budget_contract":
                path_budget_root = pathlib.Path(tempfile.mkdtemp(prefix="oni-rtb-v-"))
                original_scenario = {PATH_BUDGET_ORIGINAL_SCENARIO!r}
                scenario = {PATH_BUDGET_COMPACT_SCENARIO!r}
                run_id = "run-" + scenario
                archive_name = run_id + "-preexisting-rtb-0123456789abcdef01234567"
                candidate = (
                    path_budget_root
                    / scenario
                    / "evidence"
                    / run_id
                    / "runtime-bootstrap"
                    / "abort"
                    / "custody"
                    / "a"
                    / archive_name
                    / "old-state.json"
                )
                execution_root = report_path.parent.resolve()
                relative_candidate = candidate.relative_to(execution_root)
                legacy_root = execution_root.parent / ("oni-rtb-run-" + "0" * 32)
                legacy_candidate = legacy_root / relative_candidate
                short_units = len(str(candidate).encode("utf-16-le")) // 2
                legacy_units = len(str(legacy_candidate).encode("utf-16-le")) // 2
                execution_root_parent_units = len(str(execution_root.parent).encode("utf-16-le")) // 2
                short_units_expected = execution_root_parent_units + {PATH_BUDGET_SHORT_UNITS_OFFSET_FROM_TEMP_PARENT!r}
                legacy_units_expected = (
                    execution_root_parent_units
                    + {PATH_BUDGET_LEGACY_UNITS_OFFSET_FROM_TEMP_PARENT!r}
                )
                path_budget_failed = short_units > {PATH_BUDGET_UNIT_LIMIT!r}
                if not path_budget_failed:
                    shutil.rmtree(path_budget_root)
                budget_evidence = {{
                    "schema": "oni.live-runtime-bootstrap-runner-path-budget-fixture.v1",
                    "executionRoot": str(execution_root),
                    "executionRootLeafUnits": len(execution_root.name.encode("utf-16-le")) // 2,
                    "executionRootParentUnits": execution_root_parent_units,
                    "originalScenario": original_scenario,
                    "compactedScenario": scenario,
                    "verifierRoot": str(path_budget_root),
                    "relativeCandidate": str(relative_candidate),
                    "shortCandidate": str(candidate),
                    "shortCandidateUnits": short_units,
                    "legacyExecutionRoot": str(legacy_root),
                    "legacyCandidate": str(legacy_candidate),
                    "legacyCandidateUnits": legacy_units,
                    "shortCandidateUnitsExpected": short_units_expected,
                    "legacyCandidateUnitsExpected": legacy_units_expected,
                    "shortUnitsOffsetFromTempParent": {PATH_BUDGET_SHORT_UNITS_OFFSET_FROM_TEMP_PARENT!r},
                    "legacyUnitsOffsetFromTempParent": {PATH_BUDGET_LEGACY_UNITS_OFFSET_FROM_TEMP_PARENT!r},
                    "legacyRejected": legacy_units > {PATH_BUDGET_UNIT_LIMIT!r},
                    "limitUnits": {PATH_BUDGET_UNIT_LIMIT!r},
                    "verifierRootPreserved": path_budget_root.exists(),
                }}
                (execution_root / "fixture-path-budget-evidence.json").write_text(
                    json.dumps(budget_evidence, sort_keys=True) + "\\n",
                    encoding="utf-8",
                )

            report = build_report()
            if path_budget_failed:
                report["status"] = "failed"
                report["round_count"] = 0
                report["scenario_count"] = 9
                report["assertion_count"] = 72
                report["rounds"] = []
                report["failure"] = "runtime bootstrap path exceeds the 259 UTF-16-unit legacy budget"
                report["preserved_test_root"] = str(path_budget_root.resolve())
            elif BEHAVIOR == "failed_report":
                preserved = report_path.parent / "oni-rtb-v-preserved-fixture"
                preserved.mkdir()
                report["status"] = "failed"
                report["round_count"] = 0
                report["scenario_count"] = 9
                report["assertion_count"] = 72
                report["rounds"] = []
                report["failure"] = "fixture behavior_fail"
                report["preserved_test_root"] = str(preserved)
            elif BEHAVIOR == "huge_failed_report":
                report["status"] = "failed"
                report["round_count"] = 0
                report["scenario_count"] = 9
                report["assertion_count"] = 72
                report["rounds"] = []
                report["failure"] = "F" * 10000
            elif BEHAVIOR in ("deep_json_64", "deep_json_65"):
                nested = None
                count = 63 if BEHAVIOR == "deep_json_64" else 64
                for _ in range(count):
                    nested = [nested]
                report["failure"] = nested

            raw = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\\n"
            if BEHAVIOR == "duplicate_json_key":
                marker = '"schema": "oni.live-runtime-bootstrap-verification.v1"'
                raw = raw.replace(marker, marker + ",\\n  " + marker, 1)
            elif BEHAVIOR == "nested_duplicate_json_key":
                marker = f'"name": "{{STATIC_NAMES[0]}}"'
                raw = raw.replace(marker, marker + ",\\n      " + marker, 1)
            report_path.write_text(raw, encoding="utf-8")
            if BEHAVIOR == "source_drift":
                source = pathlib.Path(__file__)
                source.write_text(source.read_text(encoding="utf-8") + "# drift\\n", encoding="utf-8")
            if BEHAVIOR == "canonical_drift":
                CANONICAL.write_bytes(b"external canonical drift\\n")
            return 1 if path_budget_failed or BEHAVIOR in ("failed_report", "huge_failed_report") else 0
        """
    ).lstrip()


class RunnerFixture:
    def __init__(
        self,
        behavior: str,
        *,
        wrong_core: bool = False,
        canonical_exists: bool = True,
    ) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="oni-runner-test-")
        self.root = Path(self.temporary.name) / "空 格 fixture"
        self.root.mkdir()
        self.manager = self.root / "fixture manager.py"
        self.manager.write_text(
            textwrap.dedent(
                """
                from __future__ import annotations
                import os
                import pathlib
                import sys

                FIXTURE_MANAGER = True
                if __name__ == "__main__":
                    for unsafe_name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE"):
                        if os.environ.get(unsafe_name):
                            raise RuntimeError(f"unsafe child environment survived: {unsafe_name}")
                    if not sys.flags.no_user_site or not getattr(sys.flags, "safe_path", False):
                        raise RuntimeError("child site/path isolation flags are missing")
                    manager_root = pathlib.Path(__file__).resolve().parent
                    sys.path.insert(0, str(manager_root))
                    import child_probe_helper
                    expected_helper = (manager_root / "child_probe_helper.py").resolve()
                    if pathlib.Path(child_probe_helper.__file__).resolve() != expected_helper:
                        raise RuntimeError("child helper source path escaped the fixture")
                    expected_prefix = pathlib.Path(os.environ["PYTHONPYCACHEPREFIX"]).resolve()
                    if pathlib.Path(sys.pycache_prefix).resolve() != expected_prefix:
                        raise RuntimeError("child sys.pycache_prefix mismatch")
                    helper_cached = pathlib.Path(child_probe_helper.__cached__).resolve()
                    if not helper_cached.is_relative_to(expected_prefix) or helper_cached.exists():
                        raise RuntimeError("child helper cache path escaped or was materialized")
                    pathlib.Path(sys.argv[1]).write_text(child_probe_helper.VALUE, encoding="utf-8")
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.alternate_manager = self.root / "wrong manager.py"
        self.alternate_manager.write_text("WRONG_MANAGER = True\n", encoding="utf-8")
        self.explicit_dependency = self.root / "child_probe_helper.py"
        self.explicit_dependency.write_text("VALUE = 'source'\n", encoding="utf-8")
        self.canonical = self.root / "canonical 报告.json"
        if canonical_exists:
            self.canonical.write_bytes(SENTINEL)
        self.manifest = self.root / "round manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "schema": "oni.live-runtime-bootstrap-runner-test-manifest.v1",
                    "names": ROUND_NAMES,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.verifier = self.root / "fixture verifier.py"
        self.verifier.write_text(
            render_verifier(
                self.verifier,
                self.manager,
                self.canonical,
                behavior,
                self.alternate_manager if wrong_core else None,
            ),
            encoding="utf-8",
        )
        self.malicious_marker = self.root / "malicious-verifier-cache-executed.txt"
        self.malicious_manager_marker = self.root / "malicious-manager-cache-executed.txt"
        self.malicious_child_helper_marker = self.root / "malicious-child-helper-cache-executed.txt"
        self.hostile_pythonpath_marker = self.root / "hostile-pythonpath-executed.txt"
        self.hostile_pythonpath = self.root / "hostile pythonpath"
        self.hostile_pythonpath.mkdir()
        (self.hostile_pythonpath / "child_probe_helper.py").write_text(
            "import pathlib\n"
            f"pathlib.Path({str(self.hostile_pythonpath_marker)!r}).write_text('hostile', encoding='utf-8')\n"
            "VALUE = 'hostile'\n",
            encoding="utf-8",
        )
        if behavior == "malicious_adjacent_pyc":
            write_matching_timestamp_pyc(
                self.verifier,
                "import pathlib\n"
                f"pathlib.Path({str(self.malicious_marker)!r}).write_text('malicious', encoding='utf-8')\n"
                "raise RuntimeError('malicious adjacent verifier pyc executed')\n",
            )
            write_matching_timestamp_pyc(
                self.manager,
                "import pathlib\n"
                f"pathlib.Path({str(self.malicious_manager_marker)!r}).write_text('malicious', encoding='utf-8')\n"
                "raise RuntimeError('malicious adjacent manager pyc executed')\n",
            )
            write_matching_timestamp_pyc(
                self.explicit_dependency,
                "import pathlib\n"
                f"pathlib.Path({str(self.malicious_child_helper_marker)!r}).write_text('malicious', encoding='utf-8')\n"
                "VALUE = 'malicious'\n",
            )

    def close(self) -> None:
        self.temporary.cleanup()


class LiveRuntimeBootstrapRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if POWERSHELL is None:
            raise unittest.SkipTest("PowerShell is unavailable")
        cls.protected_paths = [
            RUNNER,
            WORKER,
            PRODUCTION_VERIFIER,
            PRODUCTION_MANAGER,
            PRODUCTION_CANONICAL,
        ]
        cls.protected_hashes = {str(path): sha256(path) for path in cls.protected_paths}

    @classmethod
    def tearDownClass(cls) -> None:
        observed = {str(path): sha256(path) for path in cls.protected_paths}
        if observed != cls.protected_hashes:
            raise AssertionError("production runner/verifier/manager/canonical drifted during tests")

    def run_fixture(
        self,
        behavior: str,
        *,
        what_if: bool = False,
        wrong_core: bool = False,
        production_canonical: bool = False,
        precreate_lock: bool = False,
        canonical_exists: bool = True,
        total_timeout_seconds: int | None = 20,
        inject_job_cleanup_failure: bool = False,
        inject_post_publish_failure: bool = False,
        inject_rollback_identity_swap: bool = False,
        inject_source_audit_failure: bool = False,
        inject_canonical_audit_failure: bool = False,
        inject_publish_lock_release_failure: bool = False,
        inject_source_handle_release_failure: bool = False,
        probe_source_handles_after_transaction: bool = False,
        inject_resource_monitor_nested_rename: bool = False,
        inject_resource_monitor_enumeration_io_failure: bool = False,
        inject_resource_monitor_transient_churn: bool = False,
        inject_resource_monitor_persistent_churn: bool = False,
        hostile_python_environment: bool = False,
    ) -> tuple[RunnerFixture, subprocess.CompletedProcess[str], list[dict], dict]:
        fixture = RunnerFixture(
            behavior,
            wrong_core=wrong_core,
            canonical_exists=canonical_exists,
        )
        self.addCleanup(fixture.close)
        canonical = PRODUCTION_CANONICAL if production_canonical else fixture.canonical
        if precreate_lock:
            lock = fixture.root / f".{fixture.canonical.name}.publish.lock"
            lock.write_text("held by fixture\n", encoding="utf-8")
            fixture.preexisting_lock = lock
            fixture.preexisting_lock_bytes = lock.read_bytes()
            lock_stat = lock.stat()
            fixture.preexisting_lock_identity = (lock_stat.st_dev, lock_stat.st_ino)
        command = [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
            "-CanonicalReport",
            str(canonical),
            "-WorkerScript",
            str(WORKER),
            "-VerifierScript",
            str(fixture.verifier),
            "-ManagerScript",
            str(fixture.manager),
            "-PythonExecutable",
            sys.executable,
            "-HeartbeatSeconds",
            "1",
        ]
        if total_timeout_seconds is not None:
            command.extend(["-TotalTimeoutSeconds", str(total_timeout_seconds)])
        command.extend(
            [
                "-TestFixtureRoundManifest",
                str(fixture.manifest),
                "-AdditionalSourceFile",
                str(fixture.explicit_dependency),
                "-TestRoot",
                str(fixture.root),
            ]
        )
        if what_if:
            command.append("-WhatIf")
        if inject_job_cleanup_failure:
            command.append("-TestInjectJobCleanupFailure")
        if inject_post_publish_failure:
            command.append("-TestInjectPostPublishFailure")
        if inject_rollback_identity_swap:
            command.append("-TestInjectRollbackIdentitySwap")
        if inject_source_audit_failure:
            command.append("-TestInjectSourceAuditFailure")
        if inject_canonical_audit_failure:
            command.append("-TestInjectCanonicalAuditFailure")
        if inject_publish_lock_release_failure:
            command.append("-TestInjectPublishLockReleaseFailure")
        if inject_source_handle_release_failure:
            command.append("-TestInjectSourceHandleReleaseFailure")
        if inject_resource_monitor_nested_rename:
            command.append("-TestInjectResourceMonitorNestedRename")
        if inject_resource_monitor_enumeration_io_failure:
            command.append("-TestInjectResourceMonitorEnumerationIoFailure")
        if inject_resource_monitor_transient_churn:
            command.append("-TestInjectResourceMonitorTransientChurn")
        if inject_resource_monitor_persistent_churn:
            command.append("-TestInjectResourceMonitorPersistentChurn")
        if probe_source_handles_after_transaction:
            command.append("-TestProbeSourceHandlesAfterTransaction")
        child_environment = {**os.environ, "PYTHONUTF8": "1"}
        if hostile_python_environment:
            child_environment.update(
                {
                    "PYTHONPATH": str(fixture.hostile_pythonpath),
                    "PYTHONHOME": str(fixture.hostile_pythonpath),
                    "PYTHONUSERBASE": str(fixture.hostile_pythonpath),
                }
            )
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=child_environment,
            timeout=45,
        )
        try:
            documents = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            self.fail(f"runner emitted non-JSON stdout: {exc}\nstdout={result.stdout!r}\nstderr={result.stderr!r}")
        self.assertTrue(documents, f"runner emitted no JSON; stderr={result.stderr!r}")
        final = documents[-1]
        self.assertEqual(final.get("schema"), "oni.live-runtime-bootstrap-verification-run.v1")
        execution_root = final.get("executionRoot")
        if isinstance(execution_root, str) and execution_root:
            owned_root = Path(execution_root)
            root_stat = owned_root.lstat()
            temp_stat = Path(tempfile.gettempdir()).resolve(strict=True).stat()
            self.addCleanup(
                remove_owned_execution_root,
                owned_root,
                (root_stat.st_dev, root_stat.st_ino),
                (temp_stat.st_dev, temp_stat.st_ino),
            )
        return fixture, result, documents, final

    def assert_total_timeout_parameter_rejected(self, value: int) -> None:
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(RUNNER),
                "-TotalTimeoutSeconds",
                str(value),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=20,
        )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("TotalTimeoutSeconds", output)
        self.assertNotIn("oni.live-runtime-bootstrap-verification-run.v1", output)

    def invoke_runner_base32_helper(self, payloads: list[bytes]) -> list[str]:
        command = r"""
        $ErrorActionPreference = 'Stop'
        $tokens = $null
        $parseErrors = $null
        $ast = [Management.Automation.Language.Parser]::ParseFile(
            $env:ONI_RUNNER_BASE32_SOURCE,
            [ref]$tokens,
            [ref]$parseErrors
        )
        if (@($parseErrors).Count -ne 0) { throw 'runner source did not parse' }
        $definitions = @($ast.FindAll({
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -ceq 'ConvertTo-LowerCrockfordBase32'
        }, $true))
        if ($definitions.Count -ne 1) { throw 'Base32 helper definition was not unique' }
        Invoke-Expression $definitions[0].Extent.Text
        $encoded = [Collections.Generic.List[string]]::new()
        foreach ($hex in $env:ONI_RUNNER_BASE32_VECTORS.Split([char]',')) {
            if ($hex.Length -ne 32) { throw 'test vector was not 16 bytes' }
            $bytes = [byte[]]::new(16)
            for ($index = 0; $index -lt 16; $index++) {
                $bytes[$index] = [Convert]::ToByte($hex.Substring($index * 2, 2), 16)
            }
            [void]$encoded.Add((ConvertTo-LowerCrockfordBase32 -Bytes $bytes))
        }
        [Console]::Out.WriteLine((ConvertTo-Json -Compress -InputObject @($encoded.ToArray())))
        """
        child_environment = {
            **os.environ,
            "ONI_RUNNER_BASE32_SOURCE": str(RUNNER),
            "ONI_RUNNER_BASE32_VECTORS": ",".join(payload.hex() for payload in payloads),
        }
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=child_environment,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        observed = json.loads(result.stdout)
        self.assertIsInstance(observed, list)
        return observed

    def assert_failed_without_publish(self, fixture: RunnerFixture, result, final) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(final["status"], "failed")
        self.assertFalse(final["reportPublished"])
        self.assertTrue(final["evidence"]["failureEvidencePreserved"])
        self.assertTrue(Path(final["executionRoot"]).is_dir())

    def test_file_invocation_binds_production_source_defaults_before_execution_root(self) -> None:
        fixture = RunnerFixture("valid")
        self.addCleanup(fixture.close)
        direct_temp = fixture.root / "direct invocation temp"
        direct_temp.mkdir()
        command = [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
            "-CanonicalReport",
            str(fixture.canonical),
            "-PythonExecutable",
            sys.executable,
            "-HeartbeatSeconds",
            "1",
            "-TotalTimeoutSeconds",
            "20",
            "-TestFixtureRoundManifest",
            str(fixture.manifest),
            "-TestRoot",
            str(fixture.root),
        ]
        child_environment = {
            **os.environ,
            "PYTHONUTF8": "1",
            "TEMP": str(direct_temp),
            "TMP": str(direct_temp),
        }

        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=child_environment,
            timeout=30,
        )

        expected = (
            "test-fixture manifest is forbidden with the production verifier "
            "or canonical report"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected, result.stdout + result.stderr)
        documents = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip()
        ]
        self.assertTrue(documents, result.stderr)
        final = documents[-1]
        self.assertEqual(final.get("schema"), "oni.live-runtime-bootstrap-verification-run.v1")
        self.assertEqual(final.get("status"), "failed")
        self.assertIn(expected, final.get("message", ""))
        self.assertIsNone(final.get("executionRoot"))
        self.assertFalse(final.get("reportPublished"))
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        self.assertFalse(
            [
                entry
                for entry in direct_temp.iterdir()
                if EXECUTION_ROOT_NAME.fullmatch(entry.name)
            ]
        )

    def test_valid_fixture_is_atomically_published_as_exact_bytes(self) -> None:
        pycache_before = file_tree_snapshot(SCRIPTS / "__pycache__")
        fixture, result, documents, final = self.run_fixture(
            "valid", probe_source_handles_after_transaction=True
        )
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        self.assertEqual(final["status"], "passed")
        execution_root = Path(final["executionRoot"])
        self.assertRegex(execution_root.name, EXECUTION_ROOT_NAME)
        self.assertFalse(LEGACY_EXECUTION_ROOT_NAME.fullmatch(execution_root.name))
        self.assertEqual(execution_root.parent.resolve(), Path(tempfile.gettempdir()).resolve())
        root_attributes = getattr(execution_root.lstat(), "st_file_attributes", 0)
        self.assertFalse(root_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        self.assertTrue(final["reportPublished"])
        self.assertTrue(final["testFixtureMode"])
        worker_report = Path(final["workerReportPath"])
        self.assertEqual(fixture.canonical.read_bytes(), worker_report.read_bytes())
        hashes = final["hashes"]
        self.assertEqual(hashes["candidateRawSha256"], hashes["adjacentTempRawSha256"])
        self.assertEqual(hashes["candidateRawSha256"], hashes["publishedRawSha256"])
        self.assertEqual(hashes["sourcePre"], hashes["sourcePost"])
        self.assertIn("pythonExecutable", hashes["sourcePre"])
        self.assertEqual(final["validatedReport"]["staticScenarioCount"], 10)
        self.assertEqual(final["validatedReport"]["scenarioCount"], 504)
        self.assertEqual(final["validatedReport"]["assertionCount"], 4032)
        self.assertTrue(final["evidence"]["killOnCloseJobAssigned"])
        self.assertEqual(final["evidence"]["jobActiveAfterCleanup"], 0)
        self.assertTrue(final["evidence"]["jobCleanupSucceeded"])
        self.assertTrue(final["evidence"]["copyDrainCompleted"])
        self.assertEqual(
            final["evidence"]["threatModel"],
            {
                "concurrencyCoordination": "cooperative_publish_lock_plus_canonical_read_handle",
                "postOperationIdentityChecks": True,
                "handleBoundPathDeletion": False,
                "activeNoncooperativeSameUserPathnameRace": "out_of_scope",
            },
        )
        self.assertTrue(final["evidence"]["publishLockReleased"])
        self.assertTrue(final["evidence"]["publicationTransactionCompleted"])
        self.assertTrue(final["evidence"]["sourceHandlesHeldThroughTransaction"])
        self.assertTrue(final["evidence"]["publishedCanonicalBridgeHeld"])
        self.assertIsNone(final["evidence"]["publishedCanonicalBridgeError"])
        self.assertIsNone(final["evidence"]["sourceHandleReleaseError"])
        self.assertIsNone(final["evidence"]["postAudits"]["sourceError"])
        self.assertIsNone(final["evidence"]["postAudits"]["canonicalError"])
        self.assertFalse(final["evidence"]["rollback"]["attempted"])
        self.assertFalse(list(fixture.root.glob("*.publish.tmp")))
        self.assertFalse(list(fixture.root.glob("*.rollback.bak")))
        self.assertFalse(list(fixture.root.rglob("__pycache__")))
        self.assertEqual(pycache_before, file_tree_snapshot(SCRIPTS / "__pycache__"))
        attestation_path = Path(final["workerAttestationPath"])
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        self.assertEqual(
            sha256(attestation_path),
            final["hashes"]["workerPycacheAttestationRawSha256"],
        )
        prefix = Path(attestation["pycachePrefix"])
        self.assertTrue(prefix.is_dir())
        self.assertFalse(list(prefix.rglob("*")))
        expected_sources = {
            str(path.resolve())
            for path in (WORKER, fixture.verifier, fixture.manager, fixture.explicit_dependency)
        }
        self.assertTrue(expected_sources.issubset(set(attestation["attestedSources"])))
        loaded_files = {item["filePath"] for item in attestation["loadedProjectModules"]}
        self.assertTrue({str(WORKER.resolve()), str(fixture.verifier.resolve()), str(fixture.manager.resolve())}.issubset(loaded_files))
        for module in attestation["loadedProjectModules"]:
            self.assertIn(module["filePath"], attestation["attestedSources"])
            if module["name"] == "__main__":
                self.assertIsNone(module["cachedPath"])
                self.assertIsNone(module["specCachedPath"])
                continue
            cached = Path(module["cachedPath"])
            spec_cached = Path(module["specCachedPath"])
            self.assertTrue(cached.is_relative_to(prefix))
            self.assertEqual(cached, spec_cached)
            self.assertNotIn("__pycache__", cached.parts)
            self.assertFalse(cached.exists())
        self.assertTrue(any(item.get("stage") == "completed" for item in documents[:-1]))

    def test_crockford_root_and_actual_legacy_path_budget_contract(self) -> None:
        fixed_guid_bytes = bytes.fromhex("030201000504070608090a0b0c0d0e0f")
        payloads = [bytes(16), bytes([255]) * 16, fixed_guid_bytes]
        payloads.extend(
            hashlib.sha256(f"crockford-vector-{index}".encode("ascii")).digest()[:16]
            for index in range(16)
        )
        observed = self.invoke_runner_base32_helper(payloads)
        expected = [reference_crockford_base32(payload) for payload in payloads]
        self.assertEqual(observed, expected)
        self.assertEqual(observed[2], "0c1020050g3gc209185gr38e1w")
        for payload, token in zip(payloads, observed, strict=True):
            self.assertEqual(len(token), 26)
            self.assertEqual(decode_canonical_crockford_base32(token), payload)
            self.assertRegex("r" + token, EXECUTION_ROOT_NAME)

        for rejected in (
            "r" + "0" * 25 + "1",
            "r" + "0" * 25 + "z",
            "r" + "0" * 25 + "I",
            "r" + "i" * 25 + "0",
            "r" + "l" * 25 + "0",
            "r" + "o" * 25 + "0",
            "r" + "u" * 25 + "0",
            "oni-rtb-run-" + "0" * 32,
        ):
            self.assertIsNone(EXECUTION_ROOT_NAME.fullmatch(rejected), rejected)

        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        temp_stat = temp_root.stat()
        with self.assertRaisesRegex(AssertionError, "malformed execution-root cleanup"):
            remove_owned_execution_root(
                temp_root / ("oni-rtb-run-" + "0" * 32),
                (0, 0),
                (temp_stat.st_dev, temp_stat.st_ino),
            )

        pycache_before = file_tree_snapshot(SCRIPTS / "__pycache__")
        fixture, result, _, final = self.run_fixture("path_budget_contract", what_if=True)
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        self.assertEqual(final["status"], "passed")
        self.assertFalse(final["reportPublished"])
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)

        execution_root = Path(final["executionRoot"])
        self.assertRegex(execution_root.name, EXECUTION_ROOT_NAME)
        self.assertEqual(len(execution_root.name), 27)
        self.assertEqual(execution_root.parent.resolve(strict=True), temp_root)
        root_stat = execution_root.lstat()
        self.assertFalse(
            getattr(root_stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
        ownership = final["evidence"]["executionRootOwnership"]
        self.assertEqual(Path(ownership["tempRoot"]), temp_root)
        self.assertEqual(
            ownership["tempRootIdentityBeforeCreate"],
            ownership["tempRootIdentityAfterCreate"],
        )
        self.assertTrue(ownership["executionRootIdentity"])
        self.assertEqual(ownership["namePattern"], EXECUTION_ROOT_NAME.pattern)
        with self.assertRaisesRegex(AssertionError, "root identity changed"):
            remove_owned_execution_root(
                execution_root,
                (-1, -1),
                (temp_stat.st_dev, temp_stat.st_ino),
            )
        self.assertTrue(execution_root.is_dir())

        evidence_path = execution_root / "fixture-path-budget-evidence.json"
        budget = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(
            budget["schema"],
            "oni.live-runtime-bootstrap-runner-path-budget-fixture.v1",
        )
        self.assertEqual(Path(budget["executionRoot"]), execution_root)
        self.assertEqual(budget["executionRootLeafUnits"], 27)
        self.assertEqual(budget["originalScenario"], PATH_BUDGET_ORIGINAL_SCENARIO)
        self.assertEqual(budget["compactedScenario"], PATH_BUDGET_COMPACT_SCENARIO)
        self.assertEqual(utf16_units(Path(budget["compactedScenario"])), 20)
        self.assertEqual(
            budget["shortCandidateUnitsExpected"],
            budget["executionRootParentUnits"] + budget["shortUnitsOffsetFromTempParent"],
        )
        self.assertEqual(
            budget["legacyCandidateUnitsExpected"],
            budget["executionRootParentUnits"] + budget["legacyUnitsOffsetFromTempParent"],
        )
        self.assertEqual(
            budget["legacyCandidateUnitsExpected"] - budget["shortCandidateUnitsExpected"],
            budget["legacyUnitsOffsetFromTempParent"] - budget["shortUnitsOffsetFromTempParent"],
        )
        self.assertEqual(budget["shortCandidateUnits"], budget["shortCandidateUnitsExpected"])
        self.assertEqual(budget["legacyCandidateUnits"], budget["legacyCandidateUnitsExpected"])
        self.assertEqual(budget["shortUnitsOffsetFromTempParent"], 217)
        self.assertEqual(budget["legacyUnitsOffsetFromTempParent"], 234)
        self.assertEqual(budget["limitUnits"], 259)
        self.assertLessEqual(budget["shortCandidateUnits"], budget["limitUnits"])
        self.assertTrue(budget["legacyRejected"])
        self.assertGreater(budget["legacyCandidateUnits"], budget["limitUnits"])
        self.assertEqual(
            utf16_units(Path(budget["shortCandidate"])),
            budget["shortCandidateUnits"],
        )
        self.assertEqual(
            utf16_units(Path(budget["legacyCandidate"])),
            budget["legacyCandidateUnits"],
        )
        self.assertFalse(Path(budget["verifierRoot"]).exists())
        self.assertFalse(budget["verifierRootPreserved"])

        resource_limits = final["evidence"]["resourceLimits"]
        self.assertIsNone(resource_limits["violation"])
        self.assertLessEqual(
            resource_limits["executionRootBytesObserved"],
            resource_limits["executionRootObservedKillThresholdBytes"],
        )
        self.assertTrue(final["evidence"]["jobCleanupSucceeded"])
        self.assertEqual(final["evidence"]["jobActiveAfterCleanup"], 0)
        self.assertTrue(final["evidence"]["copyDrainCompleted"])
        attestation = json.loads(
            Path(final["workerAttestationPath"]).read_text(encoding="utf-8")
        )
        self.assertTrue(attestation["prefixEmptyBeforeImport"])
        self.assertTrue(attestation["prefixEmptyAfterMain"])
        prefix = Path(attestation["pycachePrefix"])
        self.assertTrue(prefix.is_relative_to(execution_root))
        self.assertFalse(list(prefix.rglob("*")))
        self.assertEqual(pycache_before, file_tree_snapshot(SCRIPTS / "__pycache__"))

    def test_matching_adjacent_parent_and_child_pyc_are_never_executed(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "malicious_adjacent_pyc", hostile_python_environment=True
        )
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        self.assertEqual(final["status"], "passed")
        self.assertTrue(final["reportPublished"])
        self.assertFalse(fixture.malicious_marker.exists())
        self.assertFalse(fixture.malicious_manager_marker.exists())
        self.assertFalse(fixture.malicious_child_helper_marker.exists())
        self.assertFalse(fixture.hostile_pythonpath_marker.exists())
        self.assertTrue(list(fixture.root.rglob("*.pyc")), "malicious fixture pycs were not created")
        attestation = json.loads(
            Path(final["workerAttestationPath"]).read_text(encoding="utf-8")
        )
        prefix = Path(attestation["pycachePrefix"])
        self.assertTrue(attestation["prefixEmptyBeforeImport"])
        self.assertTrue(attestation["prefixEmptyAfterMain"])
        self.assertTrue(attestation["childEnvironmentSanitized"])
        self.assertFalse(list(prefix.rglob("*")))
        child_result = Path(final["executionRoot"]) / "child-manager-result.txt"
        self.assertEqual(child_result.read_text(encoding="utf-8"), "source")
        for module in attestation["loadedProjectModules"]:
            if module["name"] != "__main__":
                self.assertTrue(Path(module["cachedPath"]).is_relative_to(prefix))

    def test_whatif_emits_running_heartbeat_and_captures_nonpure_logs(self) -> None:
        fixture, result, documents, final = self.run_fixture("slow_valid", what_if=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        self.assertFalse(final["reportPublished"])
        stages = [item["stage"] for item in documents[:-1] if item.get("kind") == "heartbeat"]
        self.assertIn("started", stages)
        self.assertIn("running", stages)
        self.assertIn("completed", stages)
        stdout_log = Path(final["evidence"]["stdoutLog"]).read_text(encoding="utf-8")
        stderr_log = Path(final["evidence"]["stderrLog"]).read_text(encoding="utf-8")
        self.assertIn("not pure JSON", stdout_log)
        self.assertIn("Ω 中文", stderr_log)

    def test_static_exception_preserves_logs_and_does_not_overwrite(self) -> None:
        fixture, result, _, final = self.run_fixture("static_exception")
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        stderr_log = Path(final["evidence"]["stderrLog"]).read_text(encoding="utf-8")
        self.assertIn("fixture static contract exception", stderr_log)

    def test_success_without_report_is_rejected(self) -> None:
        fixture, result, _, final = self.run_fixture("missing_report")
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        self.assertIn("non-zero exit code", final["message"])

    def test_failed_report_summary_and_preserved_root_are_retained(self) -> None:
        fixture, result, _, final = self.run_fixture("failed_report")
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        summary = final["evidence"]["failedReport"]
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["failure"], "fixture behavior_fail")
        self.assertTrue(Path(summary["preservedTestRoot"]).is_dir())
        self.assertTrue(Path(summary["preservedTestRoot"]).is_relative_to(Path(final["executionRoot"])))

    def test_fake_count_and_ordered_name_manifest_are_rejected(self) -> None:
        for behavior, expected in (
            ("fake_count", "exactly 247"),
            ("wrong_names", "manifest hash mismatch"),
            ("wrong_order", "manifest hash mismatch"),
        ):
            with self.subTest(behavior=behavior):
                fixture, result, _, final = self.run_fixture(behavior)
                self.assert_failed_without_publish(fixture, result, final)
                self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
                self.assertIn(expected, final["message"])

    def test_duplicate_json_key_is_rejected(self) -> None:
        fixture, result, _, final = self.run_fixture("duplicate_json_key")
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        self.assertIn("duplicate", final["message"].lower())

    def test_held_source_handles_block_change_run_restore_and_release(self) -> None:
        fixture, result, _, final = self.run_fixture("source_lock_probe", what_if=True)
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        self.assertEqual(final["status"], "passed")
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        stdout_log = Path(final["evidence"]["stdoutLog"]).read_text(encoding="utf-8")
        self.assertIn("source-lock-blocked verifier write", stdout_log)
        self.assertIn("source-lock-blocked manager write", stdout_log)
        self.assertIn("source-lock-blocked verifier replace", stdout_log)
        renamed = fixture.manager.with_name("released manager.py")
        fixture.manager.rename(renamed)
        renamed.rename(fixture.manager)

    def test_canonical_drift_is_rejected_including_whatif(self) -> None:
        canonical_fixture, canonical_result, _, canonical_final = self.run_fixture(
            "canonical_drift", what_if=True
        )
        self.assert_failed_without_publish(canonical_fixture, canonical_result, canonical_final)
        self.assertEqual(canonical_fixture.canonical.read_bytes(), b"external canonical drift\n")
        self.assertIn("canonical report drifted", canonical_final["message"])

    def test_child_process_cleanup_and_fail_closed_gate(self) -> None:
        fixture, result, _, final = self.run_fixture("spawn_child_valid", what_if=True)
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        self.assertGreaterEqual(final["evidence"]["jobActiveBeforeCleanup"], 1)
        self.assertEqual(final["evidence"]["jobActiveAfterCleanup"], 0)
        self.assertTrue(final["evidence"]["jobCleanupSucceeded"])
        self.assertTrue(final["evidence"]["copyDrainCompleted"])

        failed_fixture, failed_result, _, failed_final = self.run_fixture(
            "spawn_child_valid",
            inject_job_cleanup_failure=True,
        )
        self.assert_failed_without_publish(failed_fixture, failed_result, failed_final)
        self.assertEqual(failed_fixture.canonical.read_bytes(), SENTINEL)
        self.assertIn("cleanup gate failed", failed_final["message"])
        self.assertFalse(failed_final["evidence"]["jobCleanupSucceeded"])

    def test_total_timeout_parameter_boundaries_and_default(self) -> None:
        for accepted in (1800, 3000):
            with self.subTest(accepted=accepted):
                fixture, result, _, final = self.run_fixture(
                    "valid",
                    what_if=True,
                    total_timeout_seconds=accepted,
                )
                self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
                self.assertEqual(final["status"], "passed")
                self.assertFalse(final["reportPublished"])
                self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
                self.assertEqual(
                    final["evidence"]["resourceLimits"]["deadlineSeconds"],
                    accepted,
                )

        default_fixture, default_result, _, default_final = self.run_fixture(
            "valid",
            what_if=True,
            total_timeout_seconds=None,
        )
        self.assertEqual(
            default_result.returncode,
            0,
            default_result.stderr + "\n" + default_result.stdout,
        )
        self.assertEqual(default_final["status"], "passed")
        self.assertFalse(default_final["reportPublished"])
        self.assertEqual(default_fixture.canonical.read_bytes(), SENTINEL)
        self.assertEqual(
            default_final["evidence"]["resourceLimits"]["deadlineSeconds"],
            900,
        )

        for rejected in (0, 3001):
            with self.subTest(rejected=rejected):
                self.assert_total_timeout_parameter_rejected(rejected)

    def test_one_second_total_timeout_fails_closed(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "hang",
            total_timeout_seconds=1,
        )
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        limits = final["evidence"]["resourceLimits"]
        self.assertEqual(limits["deadlineSeconds"], 1)
        self.assertEqual(limits["violation"], "worker exceeded the 1-second total deadline")
        self.assertIn("worker exceeded the 1-second total deadline", final["message"])
        self.assertTrue(final["evidence"]["killOnCloseJobAssigned"])
        self.assertEqual(final["evidence"]["jobActiveAfterCleanup"], 0)
        self.assertTrue(final["evidence"]["jobCleanupSucceeded"])
        self.assertTrue(final["evidence"]["copyDrainCompleted"])

    def test_total_log_report_and_execution_root_caps_fail_closed(self) -> None:
        cases = (
            ("hang", "deadline"),
            ("log_flood", "stdout exceeded"),
            ("report_oversize_hang", "report exceeded"),
            ("root_oversize_hang", "execution root exceeded"),
        )
        for behavior, expected in cases:
            with self.subTest(behavior=behavior):
                fixture, result, _, final = self.run_fixture(
                    behavior,
                    total_timeout_seconds=2,
                )
                self.assert_failed_without_publish(fixture, result, final)
                self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
                self.assertIn(expected, final["message"])
                limits = final["evidence"]["resourceLimits"]
                self.assertIsNotNone(limits["violation"])
                self.assertEqual(final["evidence"]["jobActiveAfterCleanup"], 0)
                if behavior == "log_flood":
                    self.assertFalse(final["evidence"]["copyDrainCompleted"])
                    self.assertTrue(limits["stdoutTruncated"])
                    self.assertGreater(
                        limits["stdoutBytesObserved"],
                        limits["stdoutHardCaptureLimitBytes"],
                    )
                    self.assertLessEqual(
                        Path(final["evidence"]["stdoutLog"]).stat().st_size,
                        limits["stdoutHardCaptureLimitBytes"],
                    )
                    self.assertEqual(
                        limits["stdoutEnforcement"],
                        "hard_capped_write_stream_then_job_kill",
                    )
                else:
                    self.assertTrue(final["evidence"]["copyDrainCompleted"])

    def test_resource_monitor_byte_limit_allows_exact_64mib_and_fails_plus_one(
        self,
    ) -> None:
        limit_pass, limit_pass_result, _, limit_pass_final = self.run_fixture(
            "resource_monitor_byte_limit_64mib",
            what_if=True,
        )
        self.assertEqual(
            limit_pass_result.returncode,
            0,
            limit_pass_result.stderr + "\n" + limit_pass_result.stdout,
        )
        self.assertEqual(limit_pass_final["status"], "passed")
        self.assertFalse(limit_pass_final["reportPublished"])
        self.assertEqual(limit_pass.canonical.read_bytes(), SENTINEL)
        pass_limits = limit_pass_final["evidence"]["resourceLimits"]
        self.assertIsNone(pass_limits["violation"])
        self.assertEqual(
            pass_limits["executionRootBytesObserved"],
            pass_limits["executionRootObservedKillThresholdBytes"],
        )

        limit_fail, limit_fail_result, _, limit_fail_final = self.run_fixture(
            "resource_monitor_byte_limit_64mib_plus_one",
            what_if=True,
        )
        self.assert_failed_without_publish(limit_fail, limit_fail_result, limit_fail_final)
        self.assertIn(
            "execution root exceeded its observed byte/entry kill threshold",
            limit_fail_final["evidence"]["resourceLimits"]["violation"],
        )
        fail_limits = limit_fail_final["evidence"]["resourceLimits"]
        self.assertEqual(
            fail_limits["executionRootBytesObserved"],
            fail_limits["executionRootObservedKillThresholdBytes"] + 1,
        )

    def test_resource_monitor_tolerates_queued_nested_directory_disappearance(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "resource_monitor_nested_rename",
            what_if=True,
            inject_resource_monitor_nested_rename=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        self.assertEqual(final["status"], "passed")
        self.assertFalse(final["reportPublished"])
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        execution_root = Path(final["executionRoot"])
        self.assertTrue((execution_root / "fixture-resource-monitor-rename-ready.txt").is_file())
        self.assertFalse(
            (
                execution_root
                / "fixture-resource-monitor-rename-source"
                / "runtime-bootstrap"
                / "stage"
                / "archive-root"
            ).exists()
        )
        moved_payload = (
            execution_root
            / "fixture-resource-monitor-archive-destination"
            / "old-state.json"
        )
        self.assertEqual(moved_payload.stat().st_size, 1024 * 1024 + 37)
        limits = final["evidence"]["resourceLimits"]
        self.assertTrue(limits["fixtureNestedRenameInjected"])
        self.assertGreaterEqual(limits["executionRootScanRestartsObserved"], 1)
        self.assertGreaterEqual(limits["executionRootTransientDisappearancesObserved"], 1)
        self.assertGreaterEqual(limits["executionRootBytesObserved"], moved_payload.stat().st_size)
        self.assertEqual(final["evidence"]["jobActiveAfterCleanup"], 0)

    def test_resource_monitor_entry_limits_allow_10000_zero_byte_directories_and_fail_10001(
        self,
    ) -> None:
        limit_pass, limit_pass_result, _, limit_pass_final = self.run_fixture(
            "resource_monitor_entry_limit_10000",
            what_if=True,
        )
        self.assertEqual(
            limit_pass_result.returncode,
            0,
            limit_pass_result.stderr + "\n" + limit_pass_result.stdout,
        )
        self.assertEqual(limit_pass_final["status"], "passed")
        self.assertFalse(limit_pass_final["reportPublished"])
        self.assertEqual(limit_pass.canonical.read_bytes(), SENTINEL)
        pass_limits = limit_pass_final["evidence"]["resourceLimits"]
        self.assertIsNone(pass_limits["violation"])
        self.assertEqual(
            pass_limits["executionRootEntriesObserved"],
            pass_limits["executionRootEntryKillThreshold"],
        )
        self.assertEqual(
            pass_limits["executionRootFilesObserved"]
            + pass_limits["executionRootDirectoriesObserved"],
            pass_limits["executionRootEntriesObserved"],
        )
        self.assertGreater(pass_limits["executionRootDirectoriesObserved"], 9900)
        self.assertLess(
            pass_limits["executionRootBytesObserved"],
            pass_limits["executionRootObservedKillThresholdBytes"],
        )
        self.assertTrue(
            (Path(limit_pass_final["executionRoot"]) / "fixture-resource-monitor-entry-limit").is_dir()
        )
        self.assertGreaterEqual(pass_limits["executionRootScanRestartsObserved"], 0)

        limit_fail, limit_fail_result, _, limit_fail_final = self.run_fixture(
            "resource_monitor_entry_limit_10001",
            what_if=True,
        )
        self.assert_failed_without_publish(limit_fail, limit_fail_result, limit_fail_final)
        self.assertIn(
            "execution root exceeded its observed byte/entry kill threshold",
            limit_fail_final["evidence"]["resourceLimits"]["violation"],
        )
        fail_limits = limit_fail_final["evidence"]["resourceLimits"]
        self.assertEqual(
            fail_limits["executionRootEntriesObserved"],
            fail_limits["executionRootEntryKillThreshold"] + 1,
        )
        self.assertEqual(
            fail_limits["executionRootFilesObserved"]
            + fail_limits["executionRootDirectoriesObserved"],
            fail_limits["executionRootEntriesObserved"],
        )

    def test_resource_monitor_unexpected_enumeration_io_failure_is_fatal(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "resource_monitor_io_failure",
            what_if=True,
            inject_resource_monitor_enumeration_io_failure=True,
        )
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        self.assertIn(
            "fixture injected resource-monitor enumeration IO failure",
            final["message"],
        )
        limits = final["evidence"]["resourceLimits"]
        self.assertTrue(limits["fixtureEnumerationIoFailureInjected"])
        self.assertEqual(final["evidence"]["jobActiveAfterCleanup"], 0)

    def test_resource_monitor_churn_is_tolerated(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "resource_monitor_churn_tolerated",
            what_if=True,
            inject_resource_monitor_transient_churn=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        self.assertEqual(final["status"], "passed")
        self.assertFalse(final["reportPublished"])
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        limits = final["evidence"]["resourceLimits"]
        self.assertIsNone(limits["violation"])
        self.assertTrue(limits["fixtureTransientChurnInjected"])
        self.assertEqual(limits["fixturePersistentChurnInjectionCount"], 0)
        self.assertGreater(limits["executionRootTransientDisappearancesObserved"], 0)
        self.assertGreaterEqual(limits["executionRootScanRestartsObserved"], 1)

    def test_resource_monitor_unstable_churn_still_fails(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "resource_monitor_churn_unstable",
            what_if=True,
            inject_resource_monitor_persistent_churn=True,
        )
        self.assert_failed_without_publish(fixture, result, final)
        self.assertIn(
            "execution root remained unstable",
            final["message"],
        )
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        limits = final["evidence"]["resourceLimits"]
        self.assertEqual(
            limits["executionRootScanAttemptLimit"],
            limits["executionRootScanRestartsObserved"],
        )
        self.assertFalse(limits["fixtureTransientChurnInjected"])
        self.assertEqual(
            limits["fixturePersistentChurnInjectionCount"],
            limits["executionRootScanAttemptLimit"],
        )
        self.assertEqual(
            limits["executionRootTransientDisappearancesObserved"],
            limits["executionRootScanAttemptLimit"],
        )

    def test_path_budget_parent_units_31_to_42_pass_43_rejects(self) -> None:
        for parent_units in range(31, 43):
            short_candidate_units = parent_units + PATH_BUDGET_SHORT_UNITS_OFFSET_FROM_TEMP_PARENT
            self.assertLessEqual(
                short_candidate_units,
                PATH_BUDGET_UNIT_LIMIT,
                f"short path budget should pass for parent_units={parent_units}",
            )
        rejected_short_candidate_units = 43 + PATH_BUDGET_SHORT_UNITS_OFFSET_FROM_TEMP_PARENT
        self.assertGreater(
            rejected_short_candidate_units,
            PATH_BUDGET_UNIT_LIMIT,
            "short path budget should reject parent_units=43",
        )

    def test_json_depth_cap_and_nested_duplicate_key(self) -> None:
        depth_ok_fixture, depth_ok_result, _, depth_ok_final = self.run_fixture("deep_json_64")
        self.assert_failed_without_publish(depth_ok_fixture, depth_ok_result, depth_ok_final)
        self.assertIn("report.failure must be null", depth_ok_final["message"])
        self.assertNotIn("nesting depth", depth_ok_final["message"])

        depth_fail_fixture, depth_fail_result, _, depth_fail_final = self.run_fixture("deep_json_65")
        self.assert_failed_without_publish(depth_fail_fixture, depth_fail_result, depth_fail_final)
        self.assertIn("maximum JSON nesting depth", depth_fail_final["message"])

        duplicate_fixture, duplicate_result, _, duplicate_final = self.run_fixture(
            "nested_duplicate_json_key"
        )
        self.assert_failed_without_publish(duplicate_fixture, duplicate_result, duplicate_final)
        self.assertIn("duplicate", duplicate_final["message"].lower())

    def test_failed_report_summary_is_bounded(self) -> None:
        fixture, result, _, final = self.run_fixture("huge_failed_report")
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        failure = final["evidence"]["failedReport"]["failure"]
        self.assertLessEqual(len(failure), 4110)
        self.assertTrue(failure.endswith("...[truncated]"))

    def test_final_source_and_canonical_audits_are_independent(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "valid",
            what_if=True,
            inject_source_audit_failure=True,
            inject_canonical_audit_failure=True,
        )
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        self.assertIn("source post-audit failed", final["message"])
        self.assertIn("canonical post-audit failed", final["message"])
        audits = final["evidence"]["postAudits"]
        self.assertIsNotNone(audits["sourceObserved"])
        self.assertIsNotNone(audits["canonicalObserved"])
        self.assertIn("injected source", audits["sourceError"])
        self.assertIn("injected canonical", audits["canonicalError"])

    def test_post_publish_failure_restores_existing_identity(self) -> None:
        fixture = None
        fixture, result, _, final = self.run_fixture(
            "valid",
            inject_post_publish_failure=True,
        )
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        rollback = final["evidence"]["rollback"]
        self.assertTrue(rollback["attempted"])
        self.assertTrue(rollback["succeeded"])
        self.assertTrue(rollback["restored"])
        self.assertEqual(rollback["action"], "restore_backup")
        self.assertEqual(
            rollback["currentAfter"]["fileIdentity"],
            final["hashes"]["canonicalPre"]["fileIdentity"],
        )

    def test_publish_lock_release_failure_is_failed_and_rolled_back(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "valid",
            inject_publish_lock_release_failure=True,
        )
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        self.assertFalse(final["evidence"]["publishLockReleased"])
        self.assertIn(
            "injected publish-lock release failure",
            final["evidence"]["publishLockReleaseError"],
        )
        self.assertTrue(final["evidence"]["rollback"]["succeeded"])
        self.assertTrue(final["evidence"]["publicationTransactionCompleted"])

    def test_source_handle_release_failure_preserves_honest_publish_fact(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "valid",
            inject_source_handle_release_failure=True,
            probe_source_handles_after_transaction=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(final["status"], "failed")
        self.assertTrue(final["reportPublished"])
        self.assertEqual(
            fixture.canonical.read_bytes(),
            Path(final["workerReportPath"]).read_bytes(),
        )
        self.assertIn(
            "injected source handle release failure",
            final["evidence"]["sourceHandleReleaseError"],
        )
        self.assertTrue(final["evidence"]["sourceHandlesHeldThroughTransaction"])
        self.assertTrue(final["evidence"]["publicationTransactionCompleted"])
        self.assertFalse(final["evidence"]["rollback"]["attempted"])

    def test_absent_first_publish_rolls_back_to_absence(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "valid",
            canonical_exists=False,
            inject_post_publish_failure=True,
        )
        self.assert_failed_without_publish(fixture, result, final)
        self.assertFalse(fixture.canonical.exists())
        rollback = final["evidence"]["rollback"]
        self.assertTrue(rollback["succeeded"])
        self.assertEqual(rollback["action"], "remove_new_canonical")
        self.assertFalse(rollback["currentAfter"]["exists"])

    def test_same_bytes_new_identity_is_not_overwritten_by_rollback(self) -> None:
        fixture, result, _, final = self.run_fixture(
            "valid",
            inject_rollback_identity_swap=True,
        )
        self.assert_failed_without_publish(fixture, result, final)
        rollback = final["evidence"]["rollback"]
        self.assertTrue(rollback["attempted"])
        self.assertFalse(rollback["succeeded"])
        self.assertFalse(rollback["restored"])
        self.assertNotEqual(
            rollback["currentBefore"]["fileIdentity"],
            rollback["publishedState"]["fileIdentity"],
        )
        self.assertEqual(
            fixture.canonical.read_bytes(),
            Path(final["workerReportPath"]).read_bytes(),
        )
        self.assertTrue(Path(rollback["backupPath"]).is_file())
        self.assertIn("rollback", final["message"].lower())

    def test_wrong_core_binding_is_rejected(self) -> None:
        fixture, result, _, final = self.run_fixture("valid", wrong_core=True)
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        stderr_log = Path(final["evidence"]["stderrLog"]).read_text(encoding="utf-8")
        self.assertIn("core module does not match", stderr_log)

    def test_preexisting_publish_lock_refuses_concurrent_publisher(self) -> None:
        fixture, result, _, final = self.run_fixture("valid", precreate_lock=True)
        self.assert_failed_without_publish(fixture, result, final)
        self.assertEqual(fixture.canonical.read_bytes(), SENTINEL)
        self.assertIn("already exists", final["message"].lower())
        self.assertEqual(
            fixture.preexisting_lock.read_bytes(),
            fixture.preexisting_lock_bytes,
        )
        lock_stat = fixture.preexisting_lock.stat()
        self.assertEqual(
            (lock_stat.st_dev, lock_stat.st_ino),
            fixture.preexisting_lock_identity,
        )

    def test_fake_sources_cannot_target_production_canonical(self) -> None:
        production_before = PRODUCTION_CANONICAL.read_bytes()
        fixture, result, _, final = self.run_fixture("valid", production_canonical=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(final["status"], "failed")
        self.assertFalse(final["reportPublished"])
        self.assertIn("production canonical requires", final["message"])
        self.assertEqual(PRODUCTION_CANONICAL.read_bytes(), production_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
