#!/usr/bin/env python3
"""Shared unittest-result reporting and an independent fixture-only negative test.

The reporting helper is deliberately test-side code.  It observes the public
``unittest.TestResult`` collections after a real suite has run, so an unchanged
source hash cannot turn a failed, skipped, or incomplete suite into a pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from typing import Callable, Iterable


REPORT_SCHEMA = "oni.live-runtime-bootstrap-import-attestation-tests.v2"


def _test_id(entry: object) -> str:
    candidate = entry[0] if isinstance(entry, tuple) else entry
    identifier = getattr(candidate, "id", None)
    return identifier() if callable(identifier) else str(candidate)


def _result_ids(result: unittest.TestResult, attribute: str) -> list[str]:
    return sorted({_test_id(entry) for entry in getattr(result, attribute, ())})


def build_attestation_evidence_document(
    *,
    result: unittest.TestResult,
    required_test_ids: Iterable[str],
    executed_test_ids: Iterable[str],
    cases: Iterable[dict[str, object]],
    source_hashes_before: dict[str, str],
    source_hashes_after: dict[str, str],
) -> dict[str, object]:
    """Build evidence from the actual result object and observed test starts."""

    required = sorted(set(required_test_ids))
    executed_list = list(executed_test_ids)
    executed = set(executed_list)
    failures = _result_ids(result, "failures")
    errors = _result_ids(result, "errors")
    skipped = _result_ids(result, "skipped")
    expected_failures = _result_ids(result, "expectedFailures")
    unexpected_successes = _result_ids(result, "unexpectedSuccesses")
    required_set = set(required)
    missing = sorted(required_set - executed)
    unexpected = sorted(executed - required_set)
    duplicate_execution = len(executed_list) != len(executed)
    source_hashes_unchanged = bool(source_hashes_before) and source_hashes_before == source_hashes_after

    status_by_required: dict[str, str] = {}
    for test_id in required:
        if test_id in errors:
            status_by_required[test_id] = "error"
        elif test_id in failures:
            status_by_required[test_id] = "failure"
        elif test_id in unexpected_successes:
            status_by_required[test_id] = "unexpected_success"
        elif test_id in expected_failures:
            status_by_required[test_id] = "expected_failure"
        elif test_id in skipped:
            status_by_required[test_id] = "skipped"
        elif test_id in executed:
            status_by_required[test_id] = "passed"
        else:
            status_by_required[test_id] = "not_run"

    collection_complete = (
        executed == required_set
        and not duplicate_execution
        and not unexpected
        and result.testsRun == len(executed_list)
    )
    result_has_bad_outcome = bool(failures or errors or expected_failures or unexpected_successes)
    required_incomplete = bool(missing or (required_set & set(skipped)))
    if not source_hashes_unchanged or result_has_bad_outcome:
        status = "failed"
    elif not collection_complete or required_incomplete:
        status = "incomplete"
    else:
        status = "passed"

    reported_cases: list[dict[str, object]] = []
    for case in cases:
        reported = dict(case)
        test_id = reported.get("testId")
        reported["unittestStatus"] = status_by_required.get(str(test_id), "not_run")
        reported_cases.append(reported)

    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "sourceHashesBefore": dict(source_hashes_before),
        "sourceHashesAfter": dict(source_hashes_after),
        "sourceHashesUnchanged": source_hashes_unchanged,
        "unittest": {
            "testsRun": result.testsRun,
            "requiredTestCount": len(required),
            "requiredTestIds": required,
            "executedTestCount": len(executed_list),
            "executedTestIds": sorted(executed),
            "missingRequiredTestIds": missing,
            "unexpectedTestIds": unexpected,
            "duplicateExecution": duplicate_execution,
            "collectionComplete": collection_complete,
            "resultWasSuccessful": result.wasSuccessful(),
            "failures": failures,
            "errors": errors,
            "skipped": skipped,
            "expectedFailures": expected_failures,
            "unexpectedSuccesses": unexpected_successes,
            "requiredCaseStatuses": status_by_required,
        },
        "cases": reported_cases,
    }


class AttestationReportingSuite(unittest.TestSuite):
    """Run a suite, then finalize evidence from the real result object."""

    def __init__(self, tests: unittest.TestSuite, *, finalize: Callable[[unittest.TestResult], object]) -> None:
        super().__init__(tests)
        self._finalize = finalize

    def run(self, result: unittest.TestResult, debug: bool = False) -> unittest.TestResult:
        completed = super().run(result, debug)
        self._finalize(completed)
        return completed


class ReportingNegativeTests(unittest.TestCase):
    """Execute failure and skip boundaries in fresh temporary fixture roots."""

    PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
    PROTECTED = (
        PROJECT_ROOT / "scripts" / "run_live_runtime_bootstrap_verification.ps1",
        PROJECT_ROOT / "scripts" / "run_live_runtime_bootstrap_verification_worker.py",
        PROJECT_ROOT / "scripts" / "manage_live_runtime_bootstrap.py",
        PROJECT_ROOT / "scripts" / "verify_live_runtime_bootstrap.py",
        PROJECT_ROOT / "scripts" / "test_live_runtime_bootstrap_runner.py",
        PROJECT_ROOT / "reports" / "live-runtime-bootstrap-verification.json",
    )
    EVIDENCE = PROJECT_ROOT / "reports" / "b11t-attestation-evidence" / "negative-reporting-evidence.json"

    def test_real_failure_and_skip_cannot_be_green_with_unchanged_sources(self) -> None:
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.PROTECTED}
        runs: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory(prefix="oni-b11-reporting-negative-") as temporary:
            root = pathlib.Path(temporary)
            driver = root / "fixture-reporting-driver.py"
            for mode in ("failure", "skip"):
                report = root / f"{mode}-report.json"
                driver.write_text(self._driver_source(mode, report, before), encoding="utf-8")
                environment = os.environ.copy()
                for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE"):
                    environment.pop(name, None)
                environment.update(
                    {
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONNOUSERSITE": "1",
                        "PYTHONSAFEPATH": "1",
                        "PYTHONUTF8": "1",
                    }
                )
                started = subprocess.run(
                    [sys.executable, "-B", str(driver)],
                    cwd=self.PROJECT_ROOT,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    timeout=30,
                )
                self.assertTrue(report.is_file(), started.stderr)
                document = json.loads(report.read_text(encoding="utf-8"))
                expected_status = "failed" if mode == "failure" else "incomplete"
                self.assertEqual(started.returncode, 1, started.stdout + started.stderr)
                self.assertEqual(document["status"], expected_status)
                self.assertTrue(document["sourceHashesUnchanged"])
                if mode == "failure":
                    self.assertTrue(document["unittest"]["failures"])
                else:
                    self.assertTrue(document["unittest"]["skipped"])
                    self.assertEqual(
                        set(document["unittest"]["requiredCaseStatuses"].values()),
                        {"passed", "skipped"},
                    )
                runs.append(
                    {
                        "mode": mode,
                        "processExitCode": started.returncode,
                        "reportStatus": document["status"],
                        "sourceHashesUnchanged": document["sourceHashesUnchanged"],
                        "report": document,
                        "stdout": started.stdout,
                        "stderr": started.stderr,
                    }
                )
        after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.PROTECTED}
        self.assertEqual(after, before)
        self.EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        self.EVIDENCE.write_text(
            json.dumps(
                {
                    "schema": "oni.b11t-attestation-reporting-negative.v1",
                    "status": "passed",
                    "sourceHashesBefore": before,
                    "sourceHashesAfter": after,
                    "sourceHashesUnchanged": before == after,
                    "runs": runs,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _driver_source(
        self,
        mode: str,
        report: pathlib.Path,
        source_hashes: dict[str, str],
    ) -> str:
        required = ["__main__.FixtureReportingTests.test_control"]
        if mode == "failure":
            required.append("__main__.FixtureReportingTests.test_injected_failure")
        else:
            required.append("__main__.FixtureReportingTests.test_injected_skip")
        return textwrap.dedent(
            f"""
            import hashlib
            import json
            import pathlib
            import sys
            import unittest
            sys.path.insert(0, {str(self.PROJECT_ROOT)!r})
            from scripts.test_bootstrap_attestation_reporting import (
                AttestationReportingSuite,
                build_attestation_evidence_document,
            )
            REPORT = pathlib.Path({str(report)!r})
            BEFORE = {source_hashes!r}
            REQUIRED = {required!r}
            EXECUTED = []

            class FixtureReportingTests(unittest.TestCase):
                def run(self, result=None):
                    if result is None:
                        result = self.defaultTestResult()
                    EXECUTED.append(self.id())
                    return super().run(result)

                def test_control(self):
                    self.assertTrue(True)

                def test_injected_failure(self):
                    self.assertEqual("deliberate-left", "deliberate-right")

                def test_injected_skip(self):
                    self.skipTest("deliberate required-case skip boundary")

            def finalize(result):
                after = {{str(path): hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest() for path in BEFORE}}
                document = build_attestation_evidence_document(
                    result=result,
                    required_test_ids=REQUIRED,
                    executed_test_ids=EXECUTED,
                    cases=[],
                    source_hashes_before=BEFORE,
                    source_hashes_after=after,
                )
                REPORT.write_text(json.dumps(document, indent=2) + "\\n", encoding="utf-8")

            def load_tests(loader, tests, pattern):
                names = [
                    "test_control",
                    "test_injected_failure" if {mode!r} == "failure" else "test_injected_skip",
                ]
                selected = unittest.TestSuite(FixtureReportingTests(name) for name in names)
                return AttestationReportingSuite(selected, finalize=finalize)

            if __name__ == "__main__":
                unittest.main(module=__name__, exit=False)
                document = json.loads(REPORT.read_text(encoding="utf-8"))
                raise SystemExit(0 if document["status"] == "passed" else 1)
            """
        ).lstrip()


if __name__ == "__main__":
    unittest.main()
