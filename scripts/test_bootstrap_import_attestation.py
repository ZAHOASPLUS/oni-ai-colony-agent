#!/usr/bin/env python3
"""Fixture-only tests for the runtime-bootstrap import attestation boundary.

The worker is exercised in isolated subprocesses with synthetic verifier and
manager sources.  No production verifier ``main`` is called and no ONI, Steam,
Bridge, save, Mod, or canonical report is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

if __package__:
    from .test_bootstrap_attestation_reporting import (
        AttestationReportingSuite,
        build_attestation_evidence_document,
    )
else:
    from test_bootstrap_attestation_reporting import (
        AttestationReportingSuite,
        build_attestation_evidence_document,
    )


SCRIPTS = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent
WORKER = SCRIPTS / "run_live_runtime_bootstrap_verification_worker.py"
RUNNER = SCRIPTS / "run_live_runtime_bootstrap_verification.ps1"
RUNNER_TEST = SCRIPTS / "test_live_runtime_bootstrap_runner.py"
PRODUCTION_VERIFIER = SCRIPTS / "verify_live_runtime_bootstrap.py"
PRODUCTION_MANAGER = SCRIPTS / "manage_live_runtime_bootstrap.py"
PRODUCTION_CANONICAL = PROJECT_ROOT / "reports" / "live-runtime-bootstrap-verification.json"
EVIDENCE = PROJECT_ROOT / "reports" / "b11-bootstrap-import-attestation" / "test-evidence.json"


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Fixture:
    def __init__(self, behavior: str, *, wrong_manager: bool = False) -> None:
        self.behavior = behavior
        self.temporary = tempfile.TemporaryDirectory(prefix="oni-b11-import-")
        self.root = pathlib.Path(self.temporary.name).resolve()
        self.manager = self.root / "fixture manager.py"
        self.wrong_manager = self.root / "wrong manager.py"
        self.wrong_origin = self.root / "wrong-origin.py"
        self.verifier = self.root / "fixture verifier.py"
        self.report = self.root / "worker report.json"
        self.attestation = self.root / "worker attestation.json"
        self.pycache = self.root / "run-scoped pycache"
        self.main_marker = self.root / "main marker.txt"
        self.import_marker = self.root / "import marker.txt"
        self.delayed_marker = self.root / "delayed marker.txt"
        self.import_helper = self.root / "fixture_import_helper.py"
        self.delayed_helper = self.root / "fixture_delayed_helper.py"
        self.pycache.mkdir()
        self.manager.write_text("FIXTURE_MANAGER = True\n", encoding="utf-8")
        self.wrong_manager.write_text("WRONG_MANAGER = True\n", encoding="utf-8")
        self.wrong_origin.write_text("WRONG_ORIGIN = True\n", encoding="utf-8")
        self.import_helper.write_text(
            f"import pathlib\npathlib.Path({str(self.import_marker)!r}).write_text('imported', encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.delayed_helper.write_text(
            f"import pathlib\npathlib.Path({str(self.delayed_marker)!r}).write_text('delayed', encoding='utf-8')\n",
            encoding="utf-8",
        )
        core = self.wrong_manager if wrong_manager else self.manager
        self.verifier.write_text(
            textwrap.dedent(
                f"""
                from __future__ import annotations
                import importlib.util
                import pathlib
                import sys
                sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
                MANAGER = pathlib.Path({str(self.manager)!r})
                _spec = importlib.util.spec_from_file_location('_b11_fixture_manager', pathlib.Path({str(core)!r}))
                if _spec is None or _spec.loader is None:
                    raise RuntimeError('fixture manager spec unavailable')
                core = importlib.util.module_from_spec(_spec)
                sys.modules[_spec.name] = core
                _spec.loader.exec_module(core)
                if {behavior!r} == 'spec_mismatch':
                    core.__spec__.origin = str(pathlib.Path(__file__).with_name('wrong-origin.py'))
                REPORT = pathlib.Path({str(self.root / 'default report.json')!r})
                MAIN_MARKER = pathlib.Path({str(self.main_marker)!r})
                def main():
                    MAIN_MARKER.write_text('main', encoding='utf-8')
                    if {behavior!r} == 'delayed_attested' or {behavior!r} == 'delayed_undeclared':
                        import fixture_delayed_helper
                    REPORT.write_text('{{"status":"passed"}}\\n', encoding='utf-8')
                    return 0
                if {behavior!r} == 'import_undeclared':
                    import fixture_import_helper
                """
            ).lstrip(),
            encoding="utf-8",
        )

    def close(self) -> None:
        self.temporary.cleanup()

    def command(self, attested: list[pathlib.Path], required: list[pathlib.Path]) -> list[str]:
        command = [
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            "-X",
            f"pycache_prefix={self.pycache}",
            "-u",
            str(WORKER),
            "--verifier",
            str(self.verifier),
            "--manager",
            str(self.manager),
            "--rounds",
            "2",
            "--report",
            str(self.report),
            "--pycache-prefix",
            str(self.pycache),
            "--attestation-report",
            str(self.attestation),
        ]
        for source in attested:
            command.extend(("--attested-source", str(source)))
        for root in (PROJECT_ROOT, self.root):
            command.extend(("--module-root", str(root)))
        for source in required:
            command.extend(("--required-loaded-source", str(source)))
        return command


class BootstrapImportAttestationTests(unittest.TestCase):
    records: list[dict[str, object]] = []
    executed_test_ids: list[str] = []
    hashes_before: dict[str, str] = {}
    protected_paths: tuple[pathlib.Path, ...] = ()
    required_test_methods = (
        "test_complete_attested_fixture_executes_main_and_passes",
        "test_import_time_undeclared_import_fails_before_main_marker",
        "test_main_only_undeclared_import_fails_in_post_main_audit",
        "test_missing_required_loaded_source_is_rejected",
        "test_production_fixed_closure_covers_read_only_import_closure",
        "test_spec_origin_mismatch_is_rejected_before_main",
        "test_wrong_manager_binding_is_rejected_before_main",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.records = []
        cls.executed_test_ids = []
        cls.protected_paths = (
            RUNNER,
            WORKER,
            PRODUCTION_MANAGER,
            PRODUCTION_VERIFIER,
            RUNNER_TEST,
            PRODUCTION_CANONICAL,
        )
        cls.hashes_before = {str(path): sha256(path) for path in cls.protected_paths}

    @classmethod
    def required_test_ids(cls) -> set[str]:
        return {
            f"{cls.__module__}.{cls.__name__}.{method}"
            for method in cls.required_test_methods
        }

    def run(self, result: unittest.TestResult | None = None) -> unittest.TestResult:
        if result is None:
            result = self.defaultTestResult()
        type(self).executed_test_ids.append(self.id())
        return super().run(result)

    @classmethod
    def write_evidence(cls, result: unittest.TestResult) -> dict[str, object]:
        hashes_after = {str(path): sha256(path) for path in cls.protected_paths}
        document = build_attestation_evidence_document(
            result=result,
            required_test_ids=cls.required_test_ids(),
            executed_test_ids=cls.executed_test_ids,
            cases=cls.records,
            source_hashes_before=cls.hashes_before,
            source_hashes_after=hashes_after,
        )
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return document

    def run_case(
        self,
        behavior: str,
        *,
        declared: list[pathlib.Path] | None = None,
        required: list[pathlib.Path] | None = None,
        wrong_manager: bool = False,
        declare_delayed: bool = False,
        require_delayed: bool = False,
    ) -> tuple[Fixture, subprocess.CompletedProcess[str]]:
        fixture = Fixture(behavior, wrong_manager=wrong_manager)
        self.addCleanup(fixture.close)
        declared_sources = list(declared or [])
        if declare_delayed:
            declared_sources.append(fixture.delayed_helper)
        attested = [WORKER, fixture.verifier, fixture.manager] + declared_sources
        required_sources = [fixture.verifier, fixture.manager] + list(required or [])
        if require_delayed:
            required_sources.append(fixture.delayed_helper)
        environment = os.environ.copy()
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE"):
            environment.pop(name, None)
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(fixture.pycache),
                "PYTHONNOUSERSITE": "1",
                "PYTHONSAFEPATH": "1",
                "PYTHONUTF8": "1",
            }
        )
        started = time.monotonic()
        result = subprocess.run(
            fixture.command(attested, required_sources),
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )
        self.records.append(
            {
                "case": behavior,
                "testName": self._testMethodName,
                "testId": self.id(),
                "exitCode": result.returncode,
                "durationSeconds": round(time.monotonic() - started, 6),
                "root": str(fixture.root),
                "attestedSourceCount": len(attested),
                "requiredSourceCount": len(required_sources),
                "mainMarker": fixture.main_marker.exists(),
                "importMarker": fixture.import_marker.exists(),
                "delayedMarker": fixture.delayed_marker.exists(),
                "report": fixture.report.exists(),
                "attestation": fixture.attestation.exists(),
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        return fixture, result

    def test_complete_attested_fixture_executes_main_and_passes(self) -> None:
        fixture, result = self.run_case("delayed_attested", declare_delayed=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(fixture.main_marker.exists())
        self.assertTrue(fixture.delayed_marker.exists())
        self.assertTrue(fixture.report.is_file())
        self.assertTrue(fixture.attestation.is_file())
        document = json.loads(fixture.attestation.read_text(encoding="utf-8"))
        self.assertIn(str(fixture.delayed_helper.resolve()), document["attestedSources"])
        self.assertEqual(list(fixture.pycache.rglob("*")), [])

    def test_import_time_undeclared_import_fails_before_main_marker(self) -> None:
        fixture, result = self.run_case("import_undeclared")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(fixture.import_marker.exists())
        self.assertFalse(fixture.main_marker.exists())
        self.assertFalse(fixture.attestation.exists())
        self.assertIn("outside the held source set", result.stderr)

    def test_main_only_undeclared_import_fails_in_post_main_audit(self) -> None:
        fixture, result = self.run_case("delayed_undeclared")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(fixture.main_marker.exists())
        self.assertTrue(fixture.delayed_marker.exists())
        self.assertTrue(fixture.report.exists())
        self.assertFalse(fixture.attestation.exists())
        self.assertIn("outside the held source set", result.stderr)

    def test_missing_required_loaded_source_is_rejected(self) -> None:
        fixture, result = self.run_case(
            "plain",
            declare_delayed=True,
            require_delayed=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(fixture.main_marker.exists())
        self.assertTrue(fixture.report.exists())
        self.assertFalse(fixture.attestation.exists())
        self.assertIn("required project sources were not loaded", result.stderr)

    def test_wrong_manager_binding_is_rejected_before_main(self) -> None:
        fixture, result = self.run_case("plain", wrong_manager=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(fixture.main_marker.exists())
        self.assertFalse(fixture.report.exists())
        self.assertFalse(fixture.attestation.exists())
        self.assertIn("does not match the attested manager", result.stderr)

    def test_spec_origin_mismatch_is_rejected_before_main(self) -> None:
        fixture, result = self.run_case("spec_mismatch")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(fixture.main_marker.exists())
        self.assertFalse(fixture.report.exists())
        self.assertFalse(fixture.attestation.exists())
        self.assertIn("spec.origin mismatched", result.stderr)

    def test_production_fixed_closure_covers_read_only_import_closure(self) -> None:
        runner_source = RUNNER.read_text(encoding="utf-8")
        start = runner_source.index("$productionDependencies = @(")
        end = runner_source.index("$dependencyIndex", start)
        dependency_block = runner_source[start:end]
        project_entries = set(
            re.findall(r'\(Join-Path \$ProjectRoot "([^"]+)"\)', dependency_block)
        )
        script_entries = set(
            re.findall(r'\(Join-Path \$PSScriptRoot "([^"]+)"\)', dependency_block)
        )
        fixed_sources = {
            (PROJECT_ROOT / pathlib.Path(raw.replace("\\", "/"))).resolve()
            for raw in project_entries
        }
        fixed_sources.update(
            (SCRIPTS / pathlib.Path(raw.replace("\\", "/"))).resolve()
            for raw in script_entries
        )
        probe = textwrap.dedent(
            """
            import importlib.util, json, pathlib, sys
            project = pathlib.Path(sys.argv[1]).resolve()
            verifier = pathlib.Path(sys.argv[2]).resolve()
            sys.path.insert(0, str(project))
            spec = importlib.util.spec_from_file_location('_b11_production_verifier', verifier)
            if spec is None or spec.loader is None:
                raise RuntimeError('production verifier spec unavailable')
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded = set()
            for item in sys.modules.values():
                path = getattr(item, '__file__', None)
                if isinstance(path, (str, bytes, pathlib.Path)):
                    candidate = pathlib.Path(path).resolve(strict=False)
                    if candidate.suffix.casefold() == '.py':
                        try:
                            candidate.relative_to(project)
                        except ValueError:
                            continue
                        loaded.add(str(candidate))
            print(json.dumps(sorted(loaded), ensure_ascii=False))
            """
        )
        environment = os.environ.copy()
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE"):
            environment.pop(name, None)
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", probe, str(PROJECT_ROOT), str(PRODUCTION_VERIFIER)],
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded_sources = {pathlib.Path(path) for path in json.loads(result.stdout)}
        allowed_sources = fixed_sources | {
            PRODUCTION_VERIFIER.resolve(),
            PRODUCTION_MANAGER.resolve(),
        }
        missing_from_fixed = sorted(str(path) for path in loaded_sources - allowed_sources)
        self.assertEqual(missing_from_fixed, [], f"unlocked production imports: {missing_from_fixed}")
        expected = {
            (PROJECT_ROOT / "agent" / name).resolve()
            for name in ("action_policy.py", "command_custody.py", "control_packet.py")
        }
        self.assertTrue(expected.issubset(fixed_sources))
        self.assertTrue(expected.issubset(loaded_sources))
        self.records.append(
            {
                "case": "production_fixed_closure",
                "testName": self._testMethodName,
                "testId": self.id(),
                "exitCode": result.returncode,
                "loadedCount": len(loaded_sources),
                "fixedDependencyCount": len(fixed_sources),
                "newlyLockedSources": sorted(str(path) for path in expected),
                "unlockedSources": missing_from_fixed,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    """Keep evidence finalization active for both unittest entry points."""

    return AttestationReportingSuite(
        tests,
        finalize=BootstrapImportAttestationTests.write_evidence,
    )


if __name__ == "__main__":
    unittest.main()
