#!/usr/bin/env python3
"""Bounded offline regressions for approval commit transport boundaries."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import multiprocessing as mp
import os
import pathlib
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from typing import Any, Dict, Optional
from unittest import mock

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from agent import command_custody as cc  # noqa: E402
from scripts import commit_command_approval as approval_commit  # noqa: E402
from scripts import verify_approval_custody_integration as approval_custody  # noqa: E402
from scripts import verify_approval_transactions as approval_transactions  # noqa: E402

OBSERVATION_WINDOW_SECONDS = 0.6
CHILD_READY_TIMEOUT_SECONDS = 3.0
CHILD_COMPLETION_TIMEOUT_SECONDS = 4.0
CHILD_JOIN_TIMEOUT_SECONDS = 3.0
CHILD_TERMINATE_JOIN_TIMEOUT_SECONDS = 1.0
CASE_A_HARD_TIMEOUT_SECONDS = 18.0
EXPECTED_CASE_NAMES = ("valid_approval_baseline", "parent_lock_precludes_invalid_dir_creation", "unowned_recovery_dir_forces_outcome_unknown", "no_delete_replaced_and_owned_recovery_dirs")
SOURCE_FILES = (
    PROJECT_ROOT / "agent" / "command_custody.py",
    PROJECT_ROOT / "agent" / "approval_receipt.py",
    PROJECT_ROOT / "agent" / "decision_frame_archive.py",
    PROJECT_ROOT / "agent" / "action_policy.py",
    PROJECT_ROOT / "scripts" / "commit_command_approval.py",
    PROJECT_ROOT / "scripts" / "verify_approval_custody_integration.py",
    PROJECT_ROOT / "scripts" / "verify_approval_transactions.py",
    PROJECT_ROOT / "scripts" / "verify_approval_frame_binding.py",
    PROJECT_ROOT / "scripts" / "verify_approval_boundary_safety.py",
)
EXPECTED_MODULES = {
    "boundary_verifier": SOURCE_FILES[-1], "command_custody": SOURCE_FILES[0], "approval_receipt": SOURCE_FILES[1],
    "decision_frame_archive": SOURCE_FILES[2], "action_policy": SOURCE_FILES[3], "approval_commit": SOURCE_FILES[4],
    "approval_custody": SOURCE_FILES[5], "approval_transactions": SOURCE_FILES[6], "approval_frame_binding": SOURCE_FILES[7],
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _key(path: pathlib.Path) -> str:
    return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _hash_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _capture_hashes(paths: tuple[pathlib.Path, ...]) -> tuple[Dict[str, Optional[str]], Dict[str, str]]:
    hashes: Dict[str, Optional[str]] = {}; errors: Dict[str, str] = {}
    for path in paths:
        try: hashes[_key(path)] = _hash_file(path)
        except BaseException as exc: hashes[_key(path)] = None; errors[_key(path)] = f"{type(exc).__name__}: {exc}"
    return hashes, errors


def _new_case(name: str) -> Dict[str, Any]:
    return {"name": name, "executed": True, "started_at_utc": utc_now(), "assertions": []}


def _check(case: Dict[str, Any], name: str, passed: bool, *, observed: Any = None, expected: Any = None) -> None:
    assertion = {"name": name, "passed": bool(passed)}
    if observed is not None: assertion["observed"] = observed
    if expected is not None: assertion["expected"] = expected
    case.setdefault("assertions", []).append(assertion)
    if not passed: case.setdefault("failures", []).append(name)


def _finish_case(case: Dict[str, Any]) -> Dict[str, Any]:
    assertions = list(case.get("assertions", [])); failures = list(dict.fromkeys(str(v) for v in case.get("failures", [])))
    if not assertions: failures.append("zero_assertions")
    if case.get("status_error"): failures.append("status_error")
    if not isinstance(case.get("cleanup"), Mapping) or case["cleanup"].get("temporary_root_removed") is not True: failures.append("cleanup_failed")
    if case.get("requires_child"):
        child = case.get("child_shutdown")
        if not isinstance(child, Mapping): failures.append("child_shutdown_missing")
        else:
            if child.get("started") is not True: failures.append("child_start_failed")
            if child.get("exitcode") != 0: failures.append("child_nonzero_exit")
            if child.get("terminated_by_runner") is True: failures.append("child_terminated")
            if child.get("residual") is not False: failures.append("child_residual_or_unknown")
            if child.get("shutdown_error") or child.get("release_error") or child.get("close_errors"): failures.append("child_shutdown_error")
    failures = list(dict.fromkeys(failures)); passed_count = sum(item.get("passed") is True for item in assertions)
    case["failures"] = failures; case["assertion_counts"] = {"total": len(assertions), "passed": passed_count, "failed": len(assertions) - passed_count}; case["failure_count"] = len(failures)
    case["status"] = "passed" if assertions and not failures and passed_count == len(assertions) else "failed"; case["passed"] = case["status"] == "passed"; case["finished_at_utc"] = utc_now()
    return case


def _record_cleanup(case: Dict[str, Any], root_text: str) -> None:
    try: removed = bool(root_text) and not pathlib.Path(root_text).exists(); error = None
    except BaseException as exc: removed = False; error = f"{type(exc).__name__}: {exc}"
    case["cleanup"] = {"temporary_root": root_text or None, "temporary_root_removed": removed}
    if error: case["cleanup"]["error"] = error
    _check(case, "temporary_root_removed_after_context_exit", removed, observed=removed, expected=True)


def _run_case(case: Dict[str, Any], prefix: str, body: Any, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    root_text = ""
    try:
        with contextlib.ExitStack() as stack:
            root_text = stack.enter_context(tempfile.TemporaryDirectory(prefix=f"a09a6-{prefix}-"))
            body(pathlib.Path(root_text), stack)
    except BaseException as exc:
        case["status_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if state is not None and state.get("shutdown") is not None: case["child_shutdown"] = state["shutdown"]
        _record_cleanup(case, root_text)
    return _finish_case(case)


def _compact_inventory(snapshot: Mapping[str, Any], baseline: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    stages = snapshot.get("stages", {})
    stage_hashes = {str(name): _hash_bytes(json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()) for name, entries in stages.items()}
    root_names = sorted(str(item.get("name")) for item in snapshot.get("root_entries", []) if isinstance(item, Mapping))
    result: Dict[str, Any] = {"queue_sha256": snapshot.get("queue_sha256"), "stage_hashes": stage_hashes, "root_names": root_names}
    if baseline is not None:
        before_names = set(baseline.get("root_names", [])); after_names = set(root_names); before_stages = baseline.get("stage_hashes", {})
        result["stage_diff"] = sorted(k for k in set(stage_hashes) | set(before_stages) if stage_hashes.get(k) != before_stages.get(k))
        result["root_diff"] = {"added": sorted(after_names - before_names), "removed": sorted(before_names - after_names)}
    return result


def _dir_fingerprint(path: pathlib.Path) -> Dict[str, Any]:
    metadata = approval_commit._safe_directory_metadata(path, label="fingerprinted directory"); entries = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        child_metadata = child.lstat(); entry: Dict[str, Any] = {"name": child.name, "identity": [int(child_metadata.st_dev), int(child_metadata.st_ino)], "mode": int(child_metadata.st_mode), "size": int(child_metadata.st_size)}
        if stat.S_ISREG(child_metadata.st_mode): entry["sha256"] = _hash_file(child)
        entries.append(entry)
    return {"identity": [int(metadata.st_dev), int(metadata.st_ino)], "sha256": _hash_bytes(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()), "entries": entries}


def _remaining(deadline: float, maximum: float) -> float:
    return max(0.0, min(maximum, deadline - time.monotonic()))


def _shutdown_child(state: Dict[str, Any]) -> Dict[str, Any]:
    if state.get("shutdown") is not None: return state["shutdown"]
    release_error = None
    try:
        if state.get("release_event") is not None: state["release_event"].set()
    except BaseException as exc: release_error = f"{type(exc).__name__}: {exc}"
    process = state.get("process")
    if process is None: result: Dict[str, Any] = {"started": False, "pid": None, "exitcode": None, "residual": None}
    else:
        try:
            pid = process.pid
            if pid is None: result = {"started": False, "pid": None, "exitcode": None, "residual": None}
            else:
                process.join(CHILD_JOIN_TIMEOUT_SECONDS); terminated = False; residual = process.is_alive()
                if residual: terminated = True; process.terminate(); process.join(CHILD_TERMINATE_JOIN_TIMEOUT_SECONDS); residual = process.is_alive()
                result = {"started": True, "pid": pid, "exitcode": process.exitcode, "terminated_by_runner": terminated, "residual": residual, "bounded_shutdown": residual is False}
        except BaseException as exc:
            try: residual = process.is_alive()
            except BaseException: residual = None
            result = {"started": process.pid is not None, "pid": process.pid, "exitcode": process.exitcode, "residual": residual, "shutdown_error": f"{type(exc).__name__}: {exc}"}
    if release_error: result["release_error"] = release_error
    close_errors = []
    for connection in state.get("connections", []):
        try: connection.close()
        except BaseException as exc: close_errors.append(f"{type(exc).__name__}: {exc}")
    if close_errors: result["close_errors"] = close_errors
    state["shutdown"] = result; return result


def _case_valid_baseline() -> Dict[str, Any]:
    case = _new_case("valid_approval_baseline")
    def body(root: pathlib.Path, _stack: contextlib.ExitStack) -> None:
        args, approved, frame = approval_transactions.prepare(root); command_id = str(approved["id"])
        case["inventory_before"] = _compact_inventory(approval_custody.inventory(root, command_id)); returned = approval_commit.commit(args); after = approval_custody.inventory(root, command_id)
        owners = approval_custody._owner_stages(after); _check(case, "valid_commit_returns_approved_target", pathlib.Path(returned) == args.target and args.target.is_file(), observed=str(returned), expected=str(args.target)); _check(case, "baseline_has_single_approved_owner", owners == ["approved"], observed=owners, expected=["approved"]); _check(case, "baseline_has_no_pending_owner", "pending" not in owners, observed=owners, expected="no pending")
        committed = json.loads(args.target.read_text(encoding="utf-8-sig")); policy = json.loads(args.policy.read_text(encoding="utf-8-sig")); approval_transactions.validate_approval_receipt(committed, policy, frame); _check(case, "baseline_receipt_validates", True, expected="valid receipt"); case["inventory_after"] = _compact_inventory(after)
    return _run_case(case, "baseline", body)


def _case_parent_lock_precludes_dir_creation() -> Dict[str, Any]:
    case = _new_case("parent_lock_precludes_invalid_dir_creation"); case["requires_child"] = True; state: Dict[str, Any] = {"process": None, "release_event": None, "connections": []}
    def body(root: pathlib.Path, stack: contextlib.ExitStack) -> None:
        args, approved, _frame = approval_transactions.prepare(root); command_id = str(approved["id"]); approved_dir = root / "commands" / "approved"; _check(case, "approved_stage_absent_before_lock", not approved_dir.exists(), observed=approved_dir.exists(), expected=False)
        context = mp.get_context("spawn"); parent_connection, child_connection = context.Pipe(duplex=False); go_event, release_event = context.Event(), context.Event(); process = context.Process(target=approval_custody._approval_worker, args=(approval_custody._commit_args_data(args), go_event, release_event, child_connection), name="a09a6-case-a-child"); state.update({"process": process, "release_event": release_event, "connections": [parent_connection, child_connection]}); stack.callback(_shutdown_child, state); process.start(); deadline = time.monotonic() + CASE_A_HARD_TIMEOUT_SECONDS; transcript = []; ready = approval_custody._wait_for_event(parent_connection, {"ready"}, _remaining(deadline, CHILD_READY_TIMEOUT_SECONDS), transcript); _check(case, "child_ready_handshake", ready is not None, observed=ready, expected="ready")
        with cc.lifecycle_custody(root, timeout_seconds=2.5):
            before_snapshot = approval_custody.inventory(root, command_id); before = _compact_inventory(before_snapshot); case["inventory_before_lock"] = before; go_event.set(); pre_call = approval_custody._wait_for_event(parent_connection, {"commit_started"}, _remaining(deadline, CHILD_READY_TIMEOUT_SECONDS), transcript); _check(case, "child_pre_call_commit_handshake", pre_call is not None and pre_call.get("phase") == "pre_call", observed=pre_call, expected="commit_started phase=pre_call")
            samples, sample_count, changed_count, last, outcome_during = [], 0, 0, None, None; observe_until = min(deadline, time.monotonic() + OBSERVATION_WINDOW_SECONDS)
            while time.monotonic() < observe_until:
                compact = _compact_inventory(approval_custody.inventory(root, command_id), before); sample_count += 1; changed_count += int(bool(compact.get("stage_diff") or compact.get("root_diff", {}).get("added") or compact.get("root_diff", {}).get("removed"))); last = compact; (samples.append(compact) if len(samples) < 2 else None); message = approval_custody._receive(parent_connection, min(0.05, observe_until - time.monotonic()))
                if message is not None:
                    transcript.append(message)
                    if message.get("event") in {"outcome", "worker_error", "pipe_error"}: outcome_during = message; break
            during_snapshot = approval_custody.inventory(root, command_id); during = _compact_inventory(during_snapshot, before)
            if last is not None and last != samples[-1]: samples.append(last)
            case["lock_inventory_observation"] = {"actual_inventory_count": sample_count, "changed_count": changed_count, "samples": samples}; case["inventory_during_lock"] = during; _check(case, "multiple_actual_lock_window_inventories", sample_count >= 2, observed=sample_count, expected=">=2"); _check(case, "all_lock_window_stage_hashes_unchanged", changed_count == 0 and not during.get("stage_diff") and not during.get("root_diff", {}).get("added") and not during.get("root_diff", {}).get("removed"), observed={"changed_samples": changed_count, "final_stage_diff": during.get("stage_diff"), "final_root_diff": during.get("root_diff")}, expected="no stage/root diff"); _check(case, "approved_dir_absent_while_lock_held", not approved_dir.exists(), observed=approved_dir.exists(), expected=False); _check(case, "commit_does_not_complete_while_lock_held", outcome_during is None, observed=outcome_during, expected=None)
        release_event.set(); outcome_after = approval_custody._wait_for_event(parent_connection, {"outcome", "worker_error", "pipe_error"}, _remaining(deadline, CHILD_COMPLETION_TIMEOUT_SECONDS), transcript); final_outcome = outcome_during or outcome_after; case["child_shutdown"] = _shutdown_child(state); final_snapshot = approval_custody.inventory(root, command_id); case["inventory_after"] = _compact_inventory(final_snapshot); case["handshake_transcript"] = transcript; _check(case, "child_commit_succeeds_after_lock_release", final_outcome is not None and final_outcome.get("event") == "outcome" and final_outcome.get("result") == "success", observed=final_outcome, expected={"event": "outcome", "result": "success"}); _check(case, "final_single_owner_is_approved", approval_custody._owner_stages(final_snapshot) == ["approved"], observed=approval_custody._owner_stages(final_snapshot), expected=["approved"])
    return _run_case(case, "case-a", body, state)


def _case_unowned_recovery_dir_blocks_recovery() -> Dict[str, Any]:
    case = _new_case("unowned_recovery_dir_forces_outcome_unknown")
    def body(root: pathlib.Path, _stack: contextlib.ExitStack) -> None:
        args, approved, _frame = approval_transactions.prepare(root); command_id = str(approved["id"]); recovery_dir = root / "commands" / "approval-recovery"; sentinel = recovery_dir / "foreign-owner-sentinel.txt"; original_create = approval_commit._create_approval_recovery_dir; original_replace = approval_commit.os.replace; injected: Dict[str, Any] = {"called": False}; replace_calls = 0
        def precreated_recovery(commands_root: pathlib.Path):
            injected["called"] = True; recovery_dir.mkdir(exist_ok=False); sentinel.write_bytes(b"foreign-owner-sentinel-v1\n"); injected["foreign_before"] = _dir_fingerprint(recovery_dir); injected["sentinel_before"] = sentinel.read_bytes(); return original_create(commands_root)
        def fail_second_replace(source: Any, target: Any) -> None:
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2: raise OSError("simulated second-rename failure before publish")
            original_replace(source, target)
        caught: Optional[BaseException] = None
        with mock.patch.object(approval_commit, "_create_approval_recovery_dir", side_effect=precreated_recovery), mock.patch.object(approval_commit.os, "replace", side_effect=fail_second_replace):
            try: approval_commit.commit(args)
            except BaseException as exc: caught = exc
        foreign_after = _dir_fingerprint(recovery_dir); after = approval_custody.inventory(root, command_id); owners = approval_custody._owner_stages(after); own_temps = sorted((root / "commands" / "approving").glob("*.tmp")); names = [entry["name"] for entry in foreign_after["entries"]]
        _check(case, "second_replace_injection_occurred", replace_calls == 2, observed=replace_calls, expected=2); _check(case, "foreign_recovery_injection_occurred", injected["called"] is True, observed=injected["called"], expected=True); _check(case, "commit_failed_as_outcome_unknown", isinstance(caught, approval_commit.ApprovalOutcomeUnknown), observed=None if caught is None else type(caught).__name__, expected="ApprovalOutcomeUnknown"); _check(case, "foreign_recovery_identity_unchanged", foreign_after["identity"] == injected["foreign_before"]["identity"], observed=foreign_after["identity"], expected=injected["foreign_before"]["identity"]); _check(case, "foreign_recovery_hash_unchanged", foreign_after["sha256"] == injected["foreign_before"]["sha256"], observed=foreign_after["sha256"], expected=injected["foreign_before"]["sha256"]); _check(case, "foreign_sentinel_bytes_unchanged", sentinel.read_bytes() == injected["sentinel_before"], observed=_hash_bytes(sentinel.read_bytes()), expected=_hash_bytes(injected["sentinel_before"])); _check(case, "foreign_dir_contains_only_sentinel", names == [sentinel.name], observed=names, expected=[sentinel.name]); _check(case, "foreign_dir_has_no_ambiguity_claim", not any(name.endswith(".ambiguity.json") or name == f"{command_id}.json" for name in names), observed=names, expected="sentinel only"); _check(case, "no_pending_later_replay", not ("pending" in owners and any(stage != "pending" for stage in owners)), observed=owners, expected="no pending+later"); _check(case, "own_approving_temps_preserved", len(own_temps) >= 2, observed=[path.name for path in own_temps], expected=">=2 .tmp files"); case["inventory_after"] = _compact_inventory(after); case["caught_error"] = None if caught is None else f"{type(caught).__name__}: {caught}"
    return _run_case(case, "case-b", body)


def _case_noop_cleanup_preserves_replacements() -> Dict[str, Any]:
    case = _new_case("no_delete_replaced_and_owned_recovery_dirs")
    def body(root: pathlib.Path, _stack: contextlib.ExitStack) -> None:
        commands = root / "commands"; commands.mkdir(); original = commands / "approval-recovery"; swapped = commands / "approval-recovery-swapped"; original.mkdir(); sentinel = original / "original-owner-sentinel.txt"; sentinel.write_bytes(b"original-sentinel-v1\n"); original_before = _dir_fingerprint(original); sentinel_before = sentinel.read_bytes(); os.replace(original, swapped); moved_sentinel = swapped / sentinel.name; swapped_after = _dir_fingerprint(swapped); replacement = commands / "approval-recovery"; replacement.mkdir(); replacement_before = _dir_fingerprint(replacement); approval_commit._remove_new_empty_recovery_dir(replacement, identity=tuple(original_before["identity"])); replacement_after = _dir_fingerprint(replacement)
        _check(case, "replaced_empty_dir_retained", replacement.is_dir(), observed=replacement.exists(), expected=True); _check(case, "replaced_dir_identity_unchanged", replacement_after["identity"] == replacement_before["identity"], observed=replacement_after["identity"], expected=replacement_before["identity"]); _check(case, "original_sentinel_retained", moved_sentinel.read_bytes() == sentinel_before, observed=_hash_bytes(moved_sentinel.read_bytes()), expected=_hash_bytes(sentinel_before)); _check(case, "replaced_old_dir_identity_preserved", swapped_after["identity"] == original_before["identity"], observed=swapped_after["identity"], expected=original_before["identity"]); _check(case, "replaced_old_dir_retained", swapped.is_dir(), observed=swapped.exists(), expected=True)
        owned = commands / "approval-recovery-owned"; owned.mkdir(); owned_before = _dir_fingerprint(owned); approval_commit._remove_new_empty_recovery_dir(owned, identity=tuple(owned_before["identity"])); owned_after = _dir_fingerprint(owned); _check(case, "owned_empty_dir_conservatively_retained", owned.is_dir(), observed=owned.exists(), expected=True); _check(case, "owned_empty_dir_identity_unchanged", owned_after["identity"] == owned_before["identity"], observed=owned_after["identity"], expected=owned_before["identity"]); case["original_before"] = original_before; case["swapped_after"] = swapped_after; case["replacement_fingerprints"] = {"before": replacement_before, "after": replacement_after}
    return _run_case(case, "case-c", body)


def _module_path_report() -> Dict[str, Any]:
    modules = {"boundary_verifier": sys.modules.get(__name__), "command_custody": cc, "approval_receipt": sys.modules.get("agent.approval_receipt"), "decision_frame_archive": sys.modules.get("agent.decision_frame_archive"), "action_policy": sys.modules.get("agent.action_policy"), "approval_commit": approval_commit, "approval_custody": approval_custody, "approval_transactions": approval_transactions, "approval_frame_binding": sys.modules.get("scripts.verify_approval_frame_binding")}; report = {}
    for name, expected in EXPECTED_MODULES.items():
        module = modules.get(name)
        try: actual = pathlib.Path(module.__file__).resolve() if module is not None and module.__file__ else None; error = None
        except BaseException as exc: actual = None; error = f"{type(exc).__name__}: {exc}"
        expected_path = expected.resolve(); report[name] = {"expected": str(expected_path), "actual": None if actual is None else str(actual), "matches": actual == expected_path, "error": error}
    return report


def _allocate_report_root() -> pathlib.Path:
    parent = PROJECT_ROOT / "reports"; parent.mkdir(parents=True, exist_ok=True); stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for _ in range(20):
        candidate = parent / f"a09a6-boundary-verifier-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:10]}"
        try: candidate.mkdir(exist_ok=False); return candidate
        except FileExistsError: pass
    raise FileExistsError("could not allocate unique boundary-verifier report root")


def _failed_case(name: str, reason: str) -> Dict[str, Any]:
    case = _new_case(name); case["executed"] = False; case["status_error"] = reason; case["cleanup"] = {"temporary_root": None, "temporary_root_removed": False}; _check(case, "case_not_executed", False, observed=reason, expected="case executed"); return _finish_case(case)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.parse_args(argv); started = utc_now(); report_root = _allocate_report_root(); module_paths = _module_path_report(); source_before, source_before_errors = _capture_hashes(SOURCE_FILES); cases = []; fatal_error = None
    try:
        modules_ok = all(value.get("matches") is True for value in module_paths.values()); preflight_ok = modules_ok and not source_before_errors
        if not preflight_ok: cases = [_failed_case("preflight_module_or_source_identity", "module path mismatch or source hash capture failed; cases were not started")]
        else:
            baseline = _case_valid_baseline(); cases.append(baseline); cases.append(_case_parent_lock_precludes_dir_creation() if baseline.get("passed") is True else _failed_case("parent_lock_precludes_invalid_dir_creation", "valid approval baseline failed; race case was not started")); cases.append(_case_unowned_recovery_dir_blocks_recovery() if baseline.get("passed") is True else _failed_case("unowned_recovery_dir_forces_outcome_unknown", "valid approval baseline failed; race case was not started")); cases.append(_case_noop_cleanup_preserves_replacements())
    except BaseException as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"; cases.append(_failed_case("runner_fatal_error", fatal_error))
    source_after, source_after_errors = _capture_hashes(SOURCE_FILES); source_drift = source_before != source_after or source_before_errors != source_after_errors; expected_count = len(EXPECTED_CASE_NAMES); names = [case.get("name") for case in cases]
    cleanup_ok = bool(cases) and all(case.get("cleanup", {}).get("temporary_root_removed") is True for case in cases); child_ok = all(case.get("requires_child") is not True or (case.get("child_shutdown", {}).get("started") is True and case.get("child_shutdown", {}).get("exitcode") == 0 and case.get("child_shutdown", {}).get("terminated_by_runner") is not True and case.get("child_shutdown", {}).get("residual") is False) for case in cases); cases_ok = len(cases) == expected_count and all(names.count(name) == 1 for name in EXPECTED_CASE_NAMES) and all(case.get("passed") is True for case in cases); module_ok = all(value.get("matches") is True for value in module_paths.values()); source_ok = not source_before_errors and not source_after_errors and not source_drift; passed = fatal_error is None and module_ok and source_ok and cleanup_ok and child_ok and cases_ok
    report = {"schema": "oni.a09a6.approval-boundary-safety.v2", "status": "passed" if passed else "failed", "passed": passed, "exit_code": 0 if passed else 1, "started_at_utc": started, "finished_at_utc": utc_now(), "report_root": str(report_root), "offline_only": True, "game_launched": False, "module_paths": module_paths, "source_hashes_before": source_before, "source_hash_errors_before": source_before_errors, "source_hashes_after": source_after, "source_hash_errors_after": source_after_errors, "source_drift_detected": source_drift, "preflight": {"module_paths_match": module_ok, "source_before_captured": not source_before_errors, "cases_started": not (cases and cases[0].get("name") == "preflight_module_or_source_identity")}, "case_counts": {"expected": expected_count, "requested": expected_count, "records": len(cases), "executed": sum(case.get("executed") is True for case in cases), "passed": sum(case.get("passed") is True for case in cases), "failed": sum(case.get("passed") is not True for case in cases)}, "cleanup_gate": {"all_temporary_roots_removed": cleanup_ok}, "normal_child_gate": {"all_required_children_normal_exit": child_ok, "case_a_hard_timeout_seconds": CASE_A_HARD_TIMEOUT_SECONDS}, "fatal_error": fatal_error, "cases": cases}
    path = report_root / "report.json"
    with path.open("x", encoding="utf-8", newline="\n") as handle: handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(path), "exit_code": report["exit_code"]}, sort_keys=True)); return report["exit_code"]


if __name__ == "__main__": raise SystemExit(main())
