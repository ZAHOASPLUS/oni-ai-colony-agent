#!/usr/bin/env python3
"""Run the existing approval transaction and frame-binding regressions together."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import sys
import tempfile
import time
from typing import Any, Callable


_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts import verify_approval_frame_binding as _frame  # noqa: E402
from scripts import verify_approval_transactions as _transaction  # noqa: E402


EXPECTED_CASES = 30


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _parse_approval_helpers(approve_script: pathlib.Path) -> dict[str, pathlib.Path]:
    """Resolve the Python helpers actually referenced by approve-command.ps1."""
    text = approve_script.read_text(encoding="utf-8-sig")
    receipt = re.search(
        r"\$ApprovalHelper\s*=\s*Join-Path\s+\(Split-Path\s+-Parent\s+\$PSScriptRoot\)\s+\"([^\"]+)\"",
        text,
    )
    commit = re.search(
        r"\$ApprovalCommitHelper\s*=\s*Join-Path\s+\$PSScriptRoot\s+\"([^\"]+)\"",
        text,
    )
    if receipt is None or commit is None:
        raise ValueError("approve-command.ps1 Python helper references could not be parsed")
    receipt_path = approve_script.parent.parent / pathlib.Path(receipt.group(1).replace("\\", "/"))
    commit_path = approve_script.parent / pathlib.Path(commit.group(1).replace("\\", "/"))
    return {
        "approve_launches_approval_receipt": receipt_path.resolve(),
        "approve_launches_commit": commit_path.resolve(),
    }


def _source_manifest(approve_script: pathlib.Path) -> dict[str, pathlib.Path]:
    paths = {
        "runner": pathlib.Path(__file__).resolve(),
        "commit_command_approval": _ROOT / "scripts" / "commit_command_approval.py",
        "command_custody": _ROOT / "agent" / "command_custody.py",
        "approval_receipt": _ROOT / "agent" / "approval_receipt.py",
        "decision_frame_archive": _ROOT / "agent" / "decision_frame_archive.py",
        "action_policy": _ROOT / "agent" / "action_policy.py",
        "verify_approval_transactions": _ROOT / "scripts" / "verify_approval_transactions.py",
        "verify_approval_frame_binding": _ROOT / "scripts" / "verify_approval_frame_binding.py",
        "approve_command": approve_script,
    }
    paths.update(_parse_approval_helpers(approve_script))
    return {name: path.resolve() for name, path in paths.items()}


def _hash_sources(paths: dict[str, pathlib.Path]) -> tuple[dict[str, str | None], dict[str, str]]:
    values: dict[str, str | None] = {}
    errors: dict[str, str] = {}
    for name, path in paths.items():
        try:
            values[name] = _sha256(path)
        except Exception as exc:
            values[name] = None
            errors[name] = f"{type(exc).__name__}: {exc}"
    return values, errors


def _allocate_report_dir() -> pathlib.Path:
    reports = _ROOT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    for _ in range(16):
        candidate = reports / f"a09a4-approval-regressions-{stamp}-{os.getpid()}-{secrets.token_hex(8)}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not allocate a unique A09A4 regression report directory")


def _run_case(name: str, function: Callable[[pathlib.Path], dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {"name": name, "status": "failed", "result": "failed"}
    temporary_root: pathlib.Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="oni-a09a4-regression-") as temporary:
            temporary_root = pathlib.Path(temporary)
            try:
                result["details"] = function(temporary_root)
            except Exception as exc:
                result["classification"] = "behavior_fail"
                result["error"] = f"{type(exc).__name__}: {exc}"
            else:
                result["status"] = "passed"
                result["result"] = "passed"
    except Exception as exc:
        result["classification"] = "runner_fail"
        result["error"] = f"{type(exc).__name__}: {exc}"
    removed = temporary_root is not None and not temporary_root.exists()
    result["cleanup"] = {"temporary_root_removed": removed}
    if not removed:
        result["status"] = "failed"
        result["result"] = "failed"
        result.setdefault("classification", "cleanup_fail")
        result.setdefault("error", "TemporaryDirectory root remained after case exit")
    result["duration_seconds"] = round(time.perf_counter() - started, 3)
    result.setdefault("error", None)
    return result


TRANSACTION_CASES: tuple[tuple[str, Callable[[pathlib.Path], dict[str, Any]]], ...] = (
    ("before_claim", lambda root: _transaction.inject(root, 1)),
    ("after_claim_before_payload", lambda root: _transaction.inject(root, 2)),
    ("after_payload_before_publish", lambda root: _transaction.inject(root, 3)),
    ("success", lambda root: _transaction.inject(root, None)),
    ("raw_after_final", lambda root: _transaction.inject_post_publish_fault(root, "raw_after_final")),
    ("fsync_failure", lambda root: _transaction.inject_post_publish_fault(root, "fsync_failure")),
    ("policy_read", lambda root: _transaction.inject_post_publish_fault(root, "policy_read")),
    ("frame_read", lambda root: _transaction.inject_post_publish_fault(root, "frame_read")),
    (
        "target_moved_executing",
        lambda root: _transaction.inject_post_publish_fault(root, "target_moved_executing"),
    ),
    ("reclaim_failure", lambda root: _transaction.inject_post_publish_fault(root, "reclaim_failure")),
    ("restore_failure", lambda root: _transaction.inject_post_publish_fault(root, "restore_failure")),
    ("unowned_lock_preserved", _transaction.case_unowned_lock_is_preserved),
)


FRAME_CASES: tuple[tuple[str, Callable[[pathlib.Path], dict[str, Any]]], ...] = (
    ("valid_bound_approval", _frame.case_valid_bound_approval),
    ("missing_frame_rejected", _frame.case_missing_frame_rejected),
    ("tampered_frame_rejected", _frame.case_tampered_frame_rejected),
    ("blocked_frame_rejected", _frame.case_blocked_frame_rejected),
    ("suggested_suggestions_disabled_rejected", _frame.case_suggested_suggestions_disabled_rejected),
    ("operator_suggestions_disabled_accepted", _frame.case_operator_suggestions_disabled_accepted),
    ("operator_pipeline_blocked_rejected", _frame.case_operator_pipeline_blocked_rejected),
    ("operator_stale_frame_rejected", _frame.case_operator_stale_frame_rejected),
    ("operator_replay_frame_rejected", _frame.case_operator_replay_frame_rejected),
    ("operator_extra_blocker_rejected", _frame.case_operator_extra_blocker_rejected),
    ("operator_policy_blocker_rejected", _frame.case_operator_policy_blocker_rejected),
    ("unsupported_mutating_mode_rejected", _frame.case_unsupported_mutating_mode_rejected),
    ("stale_source_rejected", _frame.case_stale_source_rejected),
    ("missing_survival_certificate_rejected", _frame.case_missing_survival_certificate_rejected),
    ("nonmutating_compatibility", _frame.case_nonmutating_compatibility),
    ("postapproval_command_tamper_rejected", _frame.case_postapproval_command_tamper_rejected),
    ("postapproval_policy_change_rejected", _frame.case_postapproval_policy_change_rejected),
    ("postapproval_receipt_tamper_rejected", _frame.case_postapproval_receipt_tamper_rejected),
)


def main() -> int:
    started_at_utc = _utc_now()
    report_dir = _allocate_report_dir()
    approve_script = pathlib.Path(_frame.APPROVE_SCRIPT).resolve()
    manifest_error: str | None = None
    try:
        source_paths = _source_manifest(approve_script)
    except Exception as exc:
        manifest_error = f"{type(exc).__name__}: {exc}"
        source_paths = {
            "runner": pathlib.Path(__file__).resolve(),
            "commit_command_approval": _ROOT / "scripts" / "commit_command_approval.py",
            "command_custody": _ROOT / "agent" / "command_custody.py",
            "approval_receipt": _ROOT / "agent" / "approval_receipt.py",
            "decision_frame_archive": _ROOT / "agent" / "decision_frame_archive.py",
            "action_policy": _ROOT / "agent" / "action_policy.py",
            "verify_approval_transactions": _ROOT / "scripts" / "verify_approval_transactions.py",
            "verify_approval_frame_binding": _ROOT / "scripts" / "verify_approval_frame_binding.py",
            "approve_command": approve_script,
        }
    source_before, source_errors_before = _hash_sources(source_paths)
    cases = TRANSACTION_CASES + FRAME_CASES
    results = [_run_case(name, function) for name, function in cases]
    source_after, source_errors_after = _hash_sources(source_paths)
    source_drift = bool(
        manifest_error
        or source_errors_before
        or source_errors_after
        or source_before != source_after
    )
    case_count = len(results)
    all_cases_passed = case_count == EXPECTED_CASES and all(item["status"] == "passed" for item in results)
    temporary_cleanup = {
        "all_removed": all(item["cleanup"]["temporary_root_removed"] for item in results),
        "case_count": sum(item["cleanup"]["temporary_root_removed"] for item in results),
    }
    passed = bool(all_cases_passed and temporary_cleanup["all_removed"] and not source_drift)
    exit_code = 0 if passed else 1
    report: dict[str, Any] = {
        "schema": "oni.approval-adapter-regressions.v1",
        "started_at_utc": started_at_utc,
        "ended_at_utc": _utc_now(),
        "status": "passed" if passed else "failed",
        "passed": passed,
        "exit_code": exit_code,
        "exitcode": exit_code,
        "expected_case_count": EXPECTED_CASES,
        "case_count": case_count,
        "cases": results,
        "offline_only": True,
        "game_launched": False,
        "source_files": {
            name: str(path.relative_to(_ROOT)) if path.is_relative_to(_ROOT) else str(path)
            for name, path in source_paths.items()
        },
        "source_hashes_before": source_before,
        "source_hashes_after": source_after,
        "source_drift": source_drift,
        "source_hash_errors": {"before": source_errors_before, "after": source_errors_after},
        "temporary_cleanup": temporary_cleanup,
        "limitations": {
            "child_process_lifecycle": "managed by the existing frame-binding cases",
            "process_census": "not performed by this runner; the parent task performs it separately",
            "real_bridge_queue_game_mod_config": "not read or mutated",
        },
    }
    if manifest_error is not None:
        report["source_manifest_error"] = manifest_error
    report_path = report_dir / "report.json"
    with report_path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    for item in results:
        print(f"[{item['status'].upper()}] {item['name']}: {item.get('error') or item.get('details')}")
    print(f"Report: {report_path}")
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())
