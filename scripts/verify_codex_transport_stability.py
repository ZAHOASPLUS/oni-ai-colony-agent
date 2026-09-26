#!/usr/bin/env python3
"""Offline fault-matrix verification for the Codex transport stability gate."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "scripts" / "test-codex-transport-stability.ps1"
REPORT = PROJECT_ROOT / "reports" / "codex-transport-stability-verification.json"

WRAPPER_SOURCE = r'''param(
    [Parameter(Mandatory = $true)]
    [string]$TargetScript,
    [Parameter(Mandatory = $true)]
    [ValidateSet('success', 'no_events', 'disconnect', 'query_failure', 'log_missing', 'access_denied', 'log_disabled', 'helper_fresh', 'process_query_failure', 'process_access_denied')]
    [string]$Scenario
)

$ErrorActionPreference = 'Stop'

function Get-Process {
    [CmdletBinding()]
    param([string[]]$Name)

    if ($PSBoundParameters.ContainsKey('Name')) {
        return @()
    }
    if ($Scenario -eq 'process_query_failure') {
        $record = [Management.Automation.ErrorRecord]::new(
            [InvalidOperationException]::new('offline fixture'),
            'ProcessEnumerationFailed',
            [Management.Automation.ErrorCategory]::InvalidOperation,
            $null
        )
        $PSCmdlet.ThrowTerminatingError($record)
    }
    if ($Scenario -eq 'process_access_denied') {
        $record = [Management.Automation.ErrorRecord]::new(
            [UnauthorizedAccessException]::new('offline fixture'),
            'ProcessEnumerationAccessDenied',
            [Management.Automation.ErrorCategory]::PermissionDenied,
            $null
        )
        $PSCmdlet.ThrowTerminatingError($record)
    }
    if ($Scenario -eq 'helper_fresh') {
        return [pscustomobject]@{
            ProcessName = 'clash-verge'
            Id = 4242
            StartTime = [DateTime]::Now
        }
    }
    return @()
}

function Get-WinEvent {
    [CmdletBinding(DefaultParameterSetName = 'Filter')]
    param(
        [Parameter(ParameterSetName = 'List', Mandatory = $true)]
        [string]$ListLog,
        [Parameter(ParameterSetName = 'Filter', Mandatory = $true)]
        [hashtable]$FilterHashtable
    )

    if ($PSCmdlet.ParameterSetName -eq 'List') {
        if ($Scenario -eq 'log_missing') {
            $record = [Management.Automation.ErrorRecord]::new(
                [InvalidOperationException]::new('offline fixture'),
                'NoMatchingLogsFound',
                [Management.Automation.ErrorCategory]::ObjectNotFound,
                $null
            )
            $PSCmdlet.ThrowTerminatingError($record)
        }
        if ($Scenario -eq 'access_denied') {
            $record = [Management.Automation.ErrorRecord]::new(
                [UnauthorizedAccessException]::new('offline fixture'),
                'EventLogAccessDenied',
                [Management.Automation.ErrorCategory]::PermissionDenied,
                $null
            )
            $PSCmdlet.ThrowTerminatingError($record)
        }
        return [pscustomobject]@{ IsEnabled = ($Scenario -ne 'log_disabled') }
    }

    if ($Scenario -eq 'no_events') {
        $record = [Management.Automation.ErrorRecord]::new(
            [InvalidOperationException]::new('offline fixture'),
            'NoMatchingEventsFound',
            [Management.Automation.ErrorCategory]::ObjectNotFound,
            $null
        )
        $PSCmdlet.ThrowTerminatingError($record)
    }
    if ($Scenario -eq 'query_failure') {
        $record = [Management.Automation.ErrorRecord]::new(
            [InvalidOperationException]::new('offline fixture'),
            'EventQueryFailed',
            [Management.Automation.ErrorCategory]::InvalidOperation,
            $null
        )
        $PSCmdlet.ThrowTerminatingError($record)
    }
    if ($Scenario -eq 'disconnect') {
        return [pscustomobject]@{ Id = 10001; TimeCreated = [DateTime]::UtcNow }
    }
    return [pscustomobject]@{ Id = 10000; TimeCreated = [DateTime]::UtcNow }
}

& $TargetScript -WindowSeconds 120
exit $LASTEXITCODE
'''


class VerificationFailure(RuntimeError):
    pass


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationFailure(message)


def run_case(
    powershell: str,
    wrapper: pathlib.Path,
    scenario: str,
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(wrapper),
            "-TargetScript",
            str(TARGET),
            "-Scenario",
            scenario,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = (completed.stderr or completed.stdout).strip().replace("\r", " ").replace("\n", " ")
        raise VerificationFailure(f"{scenario} did not emit structured JSON: {detail[-500:]}") from exc
    event_query = document.get("eventQuery")
    assert_true(isinstance(event_query, dict), f"{scenario} omitted eventQuery status")
    assert_true(
        set(event_query)
        == {"source", "logName", "succeeded", "stage", "category", "eventCount"},
        f"{scenario} eventQuery shape is not exact",
    )
    assert_true(
        event_query.get("source") == "windows_event_log"
        and event_query.get("logName") == "Microsoft-Windows-NetworkProfile/Operational",
        f"{scenario} event query source binding is wrong",
    )
    process_query = document.get("processQuery")
    assert_true(isinstance(process_query, dict), f"{scenario} omitted processQuery status")
    assert_true(
        set(process_query)
        == {"source", "succeeded", "stage", "category", "processCount"},
        f"{scenario} processQuery shape is not exact",
    )
    assert_true(
        process_query.get("source") == "windows_process_table"
        and process_query.get("stage") == "helper_enumeration",
        f"{scenario} process query source binding is wrong",
    )
    rendered = json.dumps(document, ensure_ascii=False, sort_keys=True)
    assert_true("offline fixture" not in rendered and str(wrapper) not in rendered, "error detail leaked")
    return {
        "returncode": completed.returncode,
        "document": document,
    }


def main() -> int:
    powershell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise SystemExit("PowerShell is required")

    cases: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="oni-transport-gate-") as temporary:
        test_root = pathlib.Path(temporary)
        wrapper = test_root / "transport-fixture-wrapper.ps1"
        wrapper.write_text(WRAPPER_SOURCE, encoding="utf-8")
        for scenario in (
            "success",
            "no_events",
            "disconnect",
            "query_failure",
            "log_missing",
            "access_denied",
            "log_disabled",
            "helper_fresh",
            "process_query_failure",
            "process_access_denied",
        ):
            cases[scenario] = run_case(powershell, wrapper, scenario)

    expected = {
        "success": (0, True, True, "ok", 0, True, "ok_no_processes", 0, 0),
        "no_events": (0, True, True, "ok_no_events", 0, True, "ok_no_processes", 0, 0),
        "disconnect": (2, False, True, "ok", 1, True, "ok_no_processes", 0, 0),
        "query_failure": (2, False, False, "event_query_failed", 0, True, "ok_no_processes", 0, 0),
        "log_missing": (2, False, False, "log_missing", 0, True, "ok_no_processes", 0, 0),
        "access_denied": (2, False, False, "access_denied", 0, True, "ok_no_processes", 0, 0),
        "log_disabled": (2, False, False, "log_disabled", 0, True, "ok_no_processes", 0, 0),
        "helper_fresh": (2, False, True, "ok", 0, True, "ok", 1, 1),
        "process_query_failure": (2, False, True, "ok", 0, False, "process_query_failed", 0, 0),
        "process_access_denied": (2, False, True, "ok", 0, False, "access_denied", 0, 0),
    }
    evidence: dict[str, Any] = {}
    for scenario, (
        returncode,
        stable,
        event_succeeded,
        event_category,
        disconnects,
        process_succeeded,
        process_category,
        process_count,
        fresh_count,
    ) in expected.items():
        actual = cases[scenario]
        document = actual["document"]
        event_query = document["eventQuery"]
        process_query = document["processQuery"]
        assert_true(actual["returncode"] == returncode, f"{scenario} return code is wrong")
        assert_true(document.get("stable") is stable, f"{scenario} stable decision is wrong")
        assert_true(event_query.get("succeeded") is event_succeeded, f"{scenario} event query status is wrong")
        assert_true(event_query.get("category") == event_category, f"{scenario} event category is wrong")
        assert_true(
            document.get("networkDisconnectCount") == disconnects,
            f"{scenario} disconnect count is wrong",
        )
        assert_true(
            process_query.get("succeeded") is process_succeeded,
            f"{scenario} process query status is wrong",
        )
        assert_true(
            process_query.get("category") == process_category
            and process_query.get("processCount") == process_count,
            f"{scenario} process query category/count is wrong",
        )
        assert_true(
            document.get("freshTransportProcessCount") == fresh_count,
            f"{scenario} fresh helper count is wrong",
        )
        if not event_succeeded or not process_succeeded:
            assert_true(
                "did not complete successfully" in str(document.get("recommendation") or ""),
                f"{scenario} failure recommendation is not fail-closed",
            )
        evidence[scenario] = {
            "returncode": actual["returncode"],
            "stable": document["stable"],
            "event_query_succeeded": event_query["succeeded"],
            "event_category": event_query["category"],
            "disconnect_count": document["networkDisconnectCount"],
            "process_query_succeeded": process_query["succeeded"],
            "process_category": process_query["category"],
            "process_count": process_query["processCount"],
            "fresh_helper_count": document["freshTransportProcessCount"],
        }

    report: dict[str, Any] = {
        "schema": "oni.codex-transport-stability-verification.v1",
        "status": "passed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "temporary_TestRoot_only",
        "live_system_event_log_accessed": False,
        "game_or_bridge_launched": False,
        "case_count": len(evidence),
        "passed_count": len(evidence),
        "failed_count": 0,
        "cases": evidence,
        "scripts": {
            TARGET.name: sha256(TARGET),
            pathlib.Path(__file__).name: sha256(pathlib.Path(__file__).resolve()),
        },
    }
    unsigned = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    report["report_hash"] = "sha256:" + hashlib.sha256(unsigned).hexdigest()
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary_report = REPORT.with_name(f".{REPORT.name}.{os.getpid()}.tmp")
    try:
        temporary_report.write_text(payload, encoding="utf-8")
        os.replace(temporary_report, REPORT)
    finally:
        temporary_report.unlink(missing_ok=True)
    print(json.dumps({"status": "passed", "passed": len(evidence), "total": len(evidence)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
