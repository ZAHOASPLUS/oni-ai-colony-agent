"""Offline integration regression for approval and lifecycle custody.

This verifier deliberately crosses a real multiprocessing boundary.  The
child calls ``scripts.commit_command_approval.commit`` with an approval fixture
constructed by the existing receipt/frame builders; no approval validator is
replaced and no verifier ``main`` function is called.

The current baseline is expected to fail the lifecycle-lock exclusion and
post-approval lifecycle-inventory cases.  A known defect is still represented
as ``passed: false`` in the report.  ``--expect-known-defect`` only changes the
process exit code for that explicitly requested diagnostic mode.
"""

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
import traceback
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import command_custody as cc  # noqa: E402
from agent.approval_receipt import validate_approval_receipt  # noqa: E402
from scripts import commit_command_approval as approval_commit  # noqa: E402
from scripts import verify_approval_transactions as approval_transactions  # noqa: E402


CASE_NAMES = (
    "valid_approval_baseline",
    "lifecycle_lock_exclusion_race",
    "post_approval_lifecycle_reentry",
)
OBSERVATION_WINDOW_SECONDS = 1.0
CHILD_READY_TIMEOUT_SECONDS = 8.0
CHILD_COMPLETION_TIMEOUT_SECONDS = 12.0
CHILD_JOIN_TIMEOUT_SECONDS = 8.0
CHILD_TERMINATE_JOIN_TIMEOUT_SECONDS = 3.0

# These are the source boundaries whose bytes affect the fixture, receipt
# validation, queue mutation, or lifecycle-custody assertions.  They are read
# before and after the run so a concurrent source edit cannot become evidence.
SOURCE_FILES = (
    PROJECT_ROOT / "agent" / "command_custody.py",
    PROJECT_ROOT / "agent" / "approval_receipt.py",
    PROJECT_ROOT / "agent" / "decision_frame_archive.py",
    PROJECT_ROOT / "agent" / "action_policy.py",
    PROJECT_ROOT / "scripts" / "commit_command_approval.py",
    PROJECT_ROOT / "scripts" / "verify_approval_transactions.py",
    PROJECT_ROOT / "scripts" / "verify_approval_frame_binding.py",
)


class SourceDriftError(RuntimeError):
    """A source boundary changed while the isolated regression was running."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_hashes() -> Dict[str, str]:
    return {
        str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"): sha256_file(path)
        for path in SOURCE_FILES
    }


def require_sources_unchanged(expected: Mapping[str, str]) -> None:
    observed = source_hashes()
    if dict(observed) != dict(expected):
        changed = sorted(
            name
            for name in set(expected) | set(observed)
            if expected.get(name) != observed.get(name)
        )
        raise SourceDriftError("source drift detected: " + ", ".join(changed))


def _reparse_point(metadata: os.stat_result) -> bool:
    return bool(int(getattr(metadata, "st_file_attributes", 0)) & 0x0400)


def _safe_kind(path: pathlib.Path, metadata: os.stat_result) -> str:
    if os.path.islink(path) or _reparse_point(metadata):
        return "reparse"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def _record_path(path: pathlib.Path) -> Dict[str, Any]:
    """Record identity and, for regular files, bytes read through the public API."""

    try:
        metadata = os.lstat(path)
    except OSError as exc:
        return {"name": path.name, "error": f"{type(exc).__name__}: {exc}"}
    record: Dict[str, Any] = {
        "name": path.name,
        "kind": _safe_kind(path, metadata),
        "identity": [int(metadata.st_dev), int(metadata.st_ino)],
        "nlink": int(metadata.st_nlink),
        "mtime_ns": int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000))),
    }
    if record["kind"] != "file" or int(metadata.st_nlink) != 1:
        return record
    try:
        raw, identity, mtime_ns = cc.read_stable_bytes(path, label=f"integration/{path.name}")
        record.update(
            {
                "bytes_read": True,
                "byte_length": len(raw),
                "sha256": cc.sha256_bytes(raw),
                "identity": [int(identity[0]), int(identity[1])],
                "mtime_ns": int(mtime_ns),
            }
        )
        try:
            parsed = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError, ValueError):
            record["json"] = "invalid"
        else:
            if isinstance(parsed, dict):
                record["command_id"] = parsed.get("id")
                record["status"] = parsed.get("status")
    except Exception as exc:
        # A concurrent rename/read race is evidence, not a reason to hide the
        # failed observation.  The caller compares this record explicitly.
        record.update(
            {
                "bytes_read": False,
                "read_error": f"{type(exc).__name__}: {exc}",
            }
        )
    return record


def _directory_records(directory: pathlib.Path) -> List[Dict[str, Any]]:
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return [{"directory_error": f"{type(exc).__name__}: {exc}"}]
    return [_record_path(path) for path in children]


def inventory(root: pathlib.Path, command_id: str) -> Dict[str, Any]:
    """Capture queue bytes, hashes, identities, and command stage owners."""

    commands = root / "commands"
    stages: Dict[str, List[Dict[str, Any]]] = {}
    owners: List[Dict[str, Any]] = []
    for stage in cc.COMMAND_STAGES:
        entries = _directory_records(commands / stage)
        stages[stage] = entries
        for entry in entries:
            if entry.get("command_id") == command_id:
                owners.append({"stage": stage, **entry})

    root_entries = _directory_records(commands)
    recovery_entries: Dict[str, List[Dict[str, Any]]] = {}
    for name in (cc.LIFECYCLE_RECOVERY_DIR_NAME, "approval-recovery"):
        recovery_entries[name] = _directory_records(commands / name)

    queue_payload = json.dumps(
        {"stages": stages, "owners": owners},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    pending = next(
        (entry for entry in stages.get("pending", []) if entry.get("name") == f"{command_id}.json"),
        None,
    )
    approved = next(
        (entry for entry in stages.get("approved", []) if entry.get("name") == f"{command_id}.json"),
        None,
    )
    return {
        "stages": stages,
        "stage_owners": owners,
        "pending_source": pending,
        "approved_target": approved,
        "root_entries": root_entries,
        "recovery_entries": recovery_entries,
        "queue_sha256": hashlib.sha256(queue_payload).hexdigest().upper(),
    }


def _owner_stages(snapshot: Mapping[str, Any]) -> List[str]:
    return [str(item.get("stage")) for item in snapshot.get("stage_owners", [])]


def _pending_signature(snapshot: Mapping[str, Any]) -> Any:
    pending = snapshot.get("pending_source")
    if not isinstance(pending, Mapping):
        return pending
    return {
        field: pending.get(field)
        for field in ("name", "byte_length", "sha256", "identity", "mtime_ns", "bytes_read")
    }


def _root_entry_names(snapshot: Mapping[str, Any]) -> List[str]:
    return sorted(
        str(entry.get("name"))
        for entry in snapshot.get("root_entries", [])
        if isinstance(entry, Mapping) and entry.get("name") is not None
    )


def _check(
    case: Dict[str, Any],
    name: str,
    passed: bool,
    *,
    observed: Any = None,
    expected: Any = None,
    detail: Optional[str] = None,
) -> bool:
    assertion: Dict[str, Any] = {"name": name, "passed": bool(passed)}
    if observed is not None:
        assertion["observed"] = observed
    if expected is not None:
        assertion["expected"] = expected
    if detail:
        assertion["detail"] = detail
    case.setdefault("assertions", []).append(assertion)
    if not passed:
        case.setdefault("failures", []).append(name)
    return bool(passed)


def _finish_case(case: Dict[str, Any]) -> Dict[str, Any]:
    assertions = list(case.get("assertions", []))
    passed_count = sum(1 for item in assertions if item.get("passed") is True)
    failed_count = len(assertions) - passed_count
    case["assertion_counts"] = {
        "total": len(assertions),
        "passed": passed_count,
        "failed": failed_count,
    }
    if case.get("blocked") is True:
        case["status"] = "blocked"
    else:
        case["status"] = "passed" if failed_count == 0 and not case.get("error") else "failed"
    case["passed"] = case["status"] == "passed"
    return case


def _new_case(name: str) -> Dict[str, Any]:
    return {"name": name, "started_at_utc": utc_now(), "assertions": [], "failures": []}


def _ensure_fixture_layout(root: pathlib.Path) -> None:
    commands = root / "commands"
    commands.mkdir(parents=True, exist_ok=True)
    for stage in cc.COMMAND_STAGES:
        (commands / stage).mkdir(exist_ok=True)
    (commands / cc.LIFECYCLE_RECOVERY_DIR_NAME).mkdir(exist_ok=True)


def _prepare_fixture(root: pathlib.Path) -> tuple[SimpleNamespace, Dict[str, Any], Dict[str, Any]]:
    # This imports and calls only the fixture preparation function.  It does
    # not invoke verify_approval_transactions.main or any existing verifier.
    args, approved, frame = approval_transactions.prepare(root)
    _ensure_fixture_layout(root)
    return args, approved, frame


def _commit_args_data(args: SimpleNamespace) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    path_fields = {"command", "target", "approved_payload", "policy", "frame"}
    for key, value in vars(args).items():
        result[key] = str(value) if key in path_fields and value is not None else value
    return result


def _namespace_from_data(data: Mapping[str, Any]) -> SimpleNamespace:
    path_fields = {"command", "target", "approved_payload", "policy", "frame"}
    return SimpleNamespace(
        **{
            key: pathlib.Path(value) if key in path_fields and value is not None else value
            for key, value in data.items()
        }
    )


def _approval_worker(
    args_data: Mapping[str, Any],
    go_event: Any,
    release_event: Any,
    connection: Any,
) -> None:
    """Spawn-safe child that calls the production approval commit function."""

    try:
        connection.send({"event": "ready", "phase": "worker_ready_before_go", "pid": os.getpid()})
        if not go_event.wait(CHILD_READY_TIMEOUT_SECONDS):
            connection.send({"event": "worker_error", "type": "EventTimeout", "message": "go event timeout"})
            return
        # This marker is intentionally sent immediately before the call.  It
        # proves only pre-call coordination, never execution inside commit().
        connection.send({"event": "commit_started", "phase": "pre_call", "pid": os.getpid()})
        started = time.monotonic()
        try:
            target = approval_commit.commit(_namespace_from_data(args_data))
        except BaseException as exc:
            connection.send(
                {
                    "event": "outcome",
                    "result": "exception",
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "pid": os.getpid(),
                }
            )
        else:
            connection.send(
                {
                    "event": "outcome",
                    "result": "success",
                    "target": str(target),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "pid": os.getpid(),
                }
            )
        # Keep the child under the runner's own handle until the parent has
        # observed whether the outcome arrived during the held lock window.
        release_event.wait(CHILD_COMPLETION_TIMEOUT_SECONDS)
    except BaseException as exc:
        try:
            connection.send(
                {
                    "event": "worker_error",
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "pid": os.getpid(),
                }
            )
        except BaseException:
            pass
    finally:
        try:
            connection.close()
        except BaseException:
            pass


def _receive(connection: Any, timeout_seconds: float) -> Optional[Dict[str, Any]]:
    if timeout_seconds <= 0 or not connection.poll(timeout_seconds):
        return None
    try:
        value = connection.recv()
    except (EOFError, OSError) as exc:
        return {"event": "pipe_error", "type": type(exc).__name__, "message": str(exc)}
    if not isinstance(value, dict):
        return {"event": "invalid_message", "value_type": type(value).__name__}
    return value


def _wait_for_event(
    connection: Any,
    expected: Iterable[str],
    timeout_seconds: float,
    transcript: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    expected_set = set(expected)
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        message = _receive(connection, min(0.05, remaining))
        if message is None:
            continue
        transcript.append(message)
        if message.get("event") in expected_set:
            return message


def _join_owned_child(process: Optional[mp.Process], label: str) -> Dict[str, Any]:
    """Join/terminate only the child represented by this Process handle."""

    if process is None:
        return {"label": label, "started": False, "residual": False}
    if process.pid is None:
        return {"label": label, "started": False, "residual": False, "start_failed": True}
    process.join(CHILD_JOIN_TIMEOUT_SECONDS)
    terminated = False
    if process.is_alive():
        terminated = True
        process.terminate()
        process.join(CHILD_TERMINATE_JOIN_TIMEOUT_SECONDS)
    return {
        "label": label,
        "started": True,
        "pid": process.pid,
        "exitcode": process.exitcode,
        "terminated_by_runner": terminated,
        "residual": process.is_alive(),
        "bounded_shutdown": not process.is_alive(),
    }


def _temp_cleanup(case: Dict[str, Any], root_text: str) -> None:
    root = pathlib.Path(root_text)
    removed = not root.exists()
    case["cleanup"] = {"temporary_root": root_text, "temporary_root_removed": removed}
    _check(case, "temporary_root_removed_after_context_exit", removed, observed=removed, expected=True)


def _record_exception(case: Dict[str, Any], label: str, exc: BaseException) -> None:
    case["error"] = f"{type(exc).__name__}: {exc}"
    case.setdefault("error_traceback", traceback.format_exc(limit=5))
    _check(case, label, False, observed=case["error"], expected="no exception")


def _case_valid_approval_baseline() -> Dict[str, Any]:
    case = _new_case(CASE_NAMES[0])
    root_text = ""
    try:
        with tempfile.TemporaryDirectory(prefix="oni-a09a3-valid-") as temporary:
            root_text = temporary
            root = pathlib.Path(temporary)
            args, approved, frame = _prepare_fixture(root)
            command_id = str(approved["id"])
            before = inventory(root, command_id)
            returned: Optional[pathlib.Path] = None
            commit_error: Optional[BaseException] = None
            try:
                returned = approval_commit.commit(args)
            except BaseException as exc:
                commit_error = exc
            after = inventory(root, command_id)
            case["inventory_before"] = before
            case["inventory_after"] = after
            case["commit_error"] = None if commit_error is None else f"{type(commit_error).__name__}: {commit_error}"
            _check(case, "actual_commit_api_returned_without_exception", commit_error is None, observed=case["commit_error"], expected=None)
            _check(
                case,
                "actual_commit_api_returned_approved_target",
                returned == args.target,
                observed=None if returned is None else str(returned),
                expected=str(args.target),
            )
            owners = after["stage_owners"]
            owner_stages = _owner_stages(after)
            _check(case, "approved_target_bytes_read_with_hash_and_identity", after["approved_target"] is not None and after["approved_target"].get("bytes_read") is True, observed=after["approved_target"], expected="stable bytes/hash/identity")
            _check(case, "pending_source_is_absent_after_commit", after["pending_source"] is None, observed=after["pending_source"], expected=None)
            _check(case, "same_id_has_exactly_one_stage_owner", len(owners) == 1, observed=owner_stages, expected=["approved"])
            _check(case, "same_id_has_no_pending_and_later_duplicate", not ("pending" in owner_stages and any(stage != "pending" for stage in owner_stages)), observed=owner_stages, expected="no pending+later coexistence")
            if after["approved_target"] is not None and after["approved_target"].get("bytes_read"):
                committed = json.loads(args.target.read_text(encoding="utf-8-sig"))
                policy = json.loads(args.policy.read_text(encoding="utf-8-sig"))
                try:
                    validate_approval_receipt(committed, policy, frame)
                except Exception as exc:
                    _check(case, "committed_receipt_validates_against_fixture_frame", False, observed=f"{type(exc).__name__}: {exc}", expected="valid approval receipt")
                else:
                    _check(case, "committed_receipt_validates_against_fixture_frame", True, observed=True, expected=True)
            else:
                _check(case, "committed_receipt_validates_against_fixture_frame", False, observed="approved target unavailable", expected="valid approval receipt")
    except BaseException as exc:
        _record_exception(case, "baseline_case_completed", exc)
    finally:
        if root_text:
            _temp_cleanup(case, root_text)
    case["finished_at_utc"] = utc_now()
    return _finish_case(case)


def _case_lifecycle_lock_race(baseline_passed: bool) -> Dict[str, Any]:
    case = _new_case(CASE_NAMES[1])
    case["baseline_gate"] = baseline_passed
    if not baseline_passed:
        # Never allow an invalid receipt/frame fixture to reach the real
        # cross-process commit path.  Keep the requested case visible as a
        # non-passing blocked result, with no temporary root or child started.
        case["blocked"] = True
        case["blocked_reason"] = "valid_approval_baseline did not pass"
        case["cleanup"] = {
            "temporary_root": None,
            "temporary_root_removed": True,
            "not_started": True,
        }
        case["child_shutdown"] = {
            "started": False,
            "residual": False,
            "bounded_shutdown": True,
            "not_started": True,
        }
        _check(case, "baseline_fixture_gate_passed_before_race", False, observed=False, expected=True)
        case["finished_at_utc"] = utc_now()
        return _finish_case(case)
    root_text = ""
    process: Optional[mp.Process] = None
    parent_connection: Any = None
    child_connection: Any = None
    release_event: Any = None
    transcript: List[Dict[str, Any]] = []
    outcome_during: Optional[Dict[str, Any]] = None
    outcome_after: Optional[Dict[str, Any]] = None
    join_info: Dict[str, Any] = {"started": False, "residual": False}
    try:
        with contextlib.ExitStack() as stack:
            temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="oni-a09a3-race-"))
            root_text = temporary
            root = pathlib.Path(temporary)
            args, approved, _frame = _prepare_fixture(root)
            command_id = str(approved["id"])
            context = mp.get_context("spawn")
            parent_connection, child_connection = context.Pipe(duplex=False)
            go_event = context.Event()
            release_event = context.Event()
            process = context.Process(
                target=_approval_worker,
                args=(_commit_args_data(args), go_event, release_event, child_connection),
                name="oni-a09a3-approval-child",
            )
            process.start()
            join_info["started"] = True

            def finalize_child() -> None:
                if release_event is not None:
                    release_event.set()
                case["child_shutdown"] = _join_owned_child(process, "approval-race-child")

            # ExitStack invokes this callback before the TemporaryDirectory
            # context is cleaned.  The child is therefore always joined while
            # its own fixture root still exists, including exceptional paths.
            stack.callback(finalize_child)
            ready = _wait_for_event(parent_connection, {"ready"}, CHILD_READY_TIMEOUT_SECONDS, transcript)
            _check(case, "child_ready_handshake_received", ready is not None, observed=ready, expected="ready")
            if ready is not None:
                with cc.lifecycle_custody(root, timeout_seconds=3.0):
                    before = inventory(root, command_id)
                    go_event.set()
                    started = _wait_for_event(parent_connection, {"commit_started"}, CHILD_READY_TIMEOUT_SECONDS, transcript)
                    _check(case, "child_pre_call_commit_handshake_received_after_go", started is not None and started.get("phase") == "pre_call", observed=started, expected="commit_started phase=pre_call")
                    hold_snapshots: List[Dict[str, Any]] = []
                    hold_deadline = time.monotonic() + OBSERVATION_WINDOW_SECONDS
                    while time.monotonic() < hold_deadline:
                        # Sample first so even a very fast violating commit has
                        # at least one deterministic held-lock snapshot.  Every
                        # collected snapshot is asserted below, including any
                        # snapshot containing a read error or partial inventory.
                        hold_snapshots.append(inventory(root, command_id))
                        message = _receive(parent_connection, min(0.05, max(0.0, hold_deadline - time.monotonic())))
                        if message is not None:
                            transcript.append(message)
                            if message.get("event") in {"outcome", "worker_error", "pipe_error"}:
                                if message.get("event") == "outcome":
                                    outcome_during = message
                                break
                    during = inventory(root, command_id)
                    case["inventory_before_lock_window"] = before
                    case["inventory_during_lock_window"] = during
                    case["hold_snapshot_count"] = len(hold_snapshots)
                    case["handshake_transcript_during_lock"] = list(transcript)
                    before_pending = before.get("pending_source")
                    during_pending = during.get("pending_source")
                    source_fields = ("byte_length", "sha256", "identity", "mtime_ns")
                    source_unchanged = before_pending is not None and before_pending == during_pending
                    if before_pending is not None and during_pending is not None:
                        source_unchanged = all(before_pending.get(field) == during_pending.get(field) for field in source_fields)
                    _check(case, "pending_source_bytes_hash_identity_unchanged_while_lock_held", source_unchanged, observed={"before": before_pending, "during": during_pending}, expected="unchanged")
                    _check(case, "all_stage_inventory_unchanged_while_lock_held", before.get("stages") == during.get("stages"), observed={"before_sha256": before.get("queue_sha256"), "during_sha256": during.get("queue_sha256")}, expected="same queue inventory")
                    _check(case, "commands_root_inventory_unchanged_while_lock_held", before.get("root_entries") == during.get("root_entries"), observed={"before": before.get("root_entries"), "during": during.get("root_entries")}, expected="same commands-root inventory")
                    _check(case, "approval_outcome_not_observed_while_lifecycle_lock_held", outcome_during is None, observed=outcome_during, expected=None)
                    for index, held_snapshot in enumerate(hold_snapshots):
                        label = f"hold_snapshot_{index:03d}"
                        _check(
                            case,
                            f"{label}_stages_match_before",
                            held_snapshot.get("stages") == before.get("stages"),
                            observed={"before_queue_sha256": before.get("queue_sha256"), "snapshot_queue_sha256": held_snapshot.get("queue_sha256")},
                            expected="same stage inventory",
                        )
                        _check(
                            case,
                            f"{label}_commands_root_matches_before",
                            held_snapshot.get("root_entries") == before.get("root_entries"),
                            observed={"before_root_names": _root_entry_names(before), "snapshot_root_names": _root_entry_names(held_snapshot)},
                            expected="same commands-root inventory",
                        )
                        _check(
                            case,
                            f"{label}_pending_source_matches_before",
                            held_snapshot.get("pending_source") == before.get("pending_source"),
                            observed={"before": _pending_signature(before), "snapshot": _pending_signature(held_snapshot)},
                            expected="same pending bytes/hash/identity",
                        )
                # The parent has now released the real OS lifecycle lock.  The
                # child may complete, and the result is read from the pipe.
                release_event.set()
                if outcome_during is None:
                    outcome_after = _wait_for_event(parent_connection, {"outcome", "worker_error", "pipe_error"}, CHILD_COMPLETION_TIMEOUT_SECONDS, transcript)
            case["handshake_transcript"] = transcript
            case["outcome_during_lock"] = outcome_during
            case["outcome_after_release"] = outcome_after
            final = inventory(root, command_id)
            case["inventory_after_release"] = final
            final_outcome = outcome_during if outcome_during is not None else outcome_after
            _check(case, "baseline_fixture_gate_passed_before_race", baseline_passed, observed=baseline_passed, expected=True)
            _check(case, "commit_completed_successfully_after_lock_release", final_outcome is not None and final_outcome.get("event") == "outcome" and final_outcome.get("result") == "success", observed=final_outcome, expected="success after release")
            final_stages = _owner_stages(final)
            _check(case, "post_race_command_has_one_approved_owner", final_stages == ["approved"], observed=final_stages, expected=["approved"])
            _check(case, "post_race_command_has_no_pending_later_duplicate", not ("pending" in final_stages and any(stage != "pending" for stage in final_stages)), observed=final_stages, expected="no pending+later coexistence")
            case["known_defect_reproduced"] = bool(
                outcome_during is not None
                or case.get("inventory_before_lock_window", {}).get("stages")
                != case.get("inventory_during_lock_window", {}).get("stages")
                or case.get("inventory_before_lock_window", {}).get("root_entries")
                != case.get("inventory_during_lock_window", {}).get("root_entries")
            )
    except BaseException as exc:
        _record_exception(case, "lifecycle_lock_race_completed", exc)
    finally:
        if release_event is not None:
            release_event.set()
        if process is not None and "child_shutdown" not in case:
            join_info = _join_owned_child(process, "approval-race-child")
        elif "child_shutdown" in case:
            join_info = case["child_shutdown"]
        case["child_shutdown"] = join_info
        _check(case, "child_exited_normally_with_zero_exitcode", join_info.get("exitcode") == 0, observed=join_info.get("exitcode"), expected=0)
        _check(case, "child_was_not_terminated_by_runner", join_info.get("terminated_by_runner") is False, observed=join_info.get("terminated_by_runner"), expected=False)
        _check(case, "child_has_no_residual_process_after_bounded_join", join_info.get("residual") is False, observed=join_info.get("residual"), expected=False)
        if parent_connection is not None:
            try:
                parent_connection.close()
            except BaseException:
                pass
        if child_connection is not None:
            try:
                child_connection.close()
            except BaseException:
                pass
        if root_text:
            _temp_cleanup(case, root_text)
    case["finished_at_utc"] = utc_now()
    return _finish_case(case)


def _case_post_approval_reentry() -> Dict[str, Any]:
    case = _new_case(CASE_NAMES[2])
    root_text = ""
    try:
        with tempfile.TemporaryDirectory(prefix="oni-a09a3-reentry-") as temporary:
            root_text = temporary
            root = pathlib.Path(temporary)
            args, approved, _frame = _prepare_fixture(root)
            command_id = str(approved["id"])
            try:
                approval_commit.commit(args)
                commit_error: Optional[str] = None
            except BaseException as exc:
                commit_error = f"{type(exc).__name__}: {exc}"
            post_commit = inventory(root, command_id)
            case["post_commit_inventory"] = post_commit
            case["commit_error"] = commit_error
            _check(case, "post_approval_commit_succeeded", commit_error is None, observed=commit_error, expected=None)
            private_locks = [
                item
                for item in post_commit.get("root_entries", [])
                if str(item.get("name", "")).startswith(".approval-")
            ]
            case["private_approval_lock_entries"] = private_locks
            _check(case, "no_private_approval_lock_file_left_behind", not private_locks, observed=private_locks, expected=[])
            reentry_error: Optional[str] = None
            reentry_snapshot: Optional[Dict[str, Any]] = None
            try:
                with cc.lifecycle_custody(root, timeout_seconds=3.0):
                    reentry_snapshot = inventory(root, command_id)
            except BaseException as exc:
                reentry_error = f"{type(exc).__name__}: {exc}"
            case["lifecycle_reentry_error"] = reentry_error
            case["lifecycle_reentry_inventory"] = reentry_snapshot
            _check(case, "lifecycle_custody_reenters_after_approval", reentry_error is None, observed=reentry_error, expected=None)
            inventory_error: Optional[str] = None
            recovery_result: Any = None
            try:
                recovery_result = cc.recover_lifecycle_transactions(root)
            except BaseException as exc:
                inventory_error = f"{type(exc).__name__}: {exc}"
            case["lifecycle_inventory_error"] = inventory_error
            case["lifecycle_inventory_result"] = recovery_result
            _check(case, "lifecycle_inventory_read_succeeds_after_approval", inventory_error is None, observed=inventory_error, expected=None)
            owners = _owner_stages(post_commit)
            _check(case, "post_approval_stage_owners_are_single_approved_copy", owners == ["approved"], observed=owners, expected=["approved"])
            _check(case, "post_approval_pending_and_later_do_not_coexist", not ("pending" in owners and any(stage != "pending" for stage in owners)), observed=owners, expected="no pending+later coexistence")
            case["approval_recovery_root_entry"] = next((item for item in post_commit.get("root_entries", []) if item.get("name") == "approval-recovery"), None)
            case["known_defect_reproduced"] = bool(
                inventory_error is not None and "approval-recovery" in inventory_error
            )
    except BaseException as exc:
        _record_exception(case, "post_approval_reentry_completed", exc)
    finally:
        if root_text:
            _temp_cleanup(case, root_text)
    case["finished_at_utc"] = utc_now()
    return _finish_case(case)


def _allocate_report_root(requested: Optional[pathlib.Path]) -> pathlib.Path:
    if requested is not None:
        root = requested.expanduser()
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        root = root.absolute()
        root.mkdir(parents=True, exist_ok=False)
        return root
    parent = PROJECT_ROOT / "reports"
    parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for _ in range(20):
        candidate = parent / f"a09a3-approval-custody-integration-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:10]}"
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not allocate a unique B16-independent report root")


def _write_report(report_root: pathlib.Path, report: Mapping[str, Any]) -> pathlib.Path:
    path = report_root / "report.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-root",
        type=pathlib.Path,
        help="new report directory; if omitted, allocate a unique reports/a09a3-* directory",
    )
    parser.add_argument(
        "--expect-known-defect",
        action="store_true",
        help="diagnostic mode: exit 0 only for the known baseline defect signature; report still has passed=false",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = parse_args(argv)
    started_at = utc_now()
    report_root: Optional[pathlib.Path] = None
    source_before: Dict[str, str] = {}
    source_after: Dict[str, str] = {}
    cases: List[Dict[str, Any]] = []
    source_drift = False
    top_error: Optional[str] = None
    try:
        report_root = _allocate_report_root(arguments.report_root)
        source_before = source_hashes()
        require_sources_unchanged(source_before)
        baseline = _case_valid_approval_baseline()
        cases.append(baseline)
        require_sources_unchanged(source_before)
        race = _case_lifecycle_lock_race(bool(baseline.get("passed")))
        cases.append(race)
        require_sources_unchanged(source_before)
        reentry = _case_post_approval_reentry()
        cases.append(reentry)
        source_after = source_hashes()
        source_drift = source_after != source_before
    except SourceDriftError as exc:
        source_drift = True
        top_error = str(exc)
        cases.append(
            _finish_case(
                {
                    "name": "source_stability_guard",
                    "started_at_utc": started_at,
                    "assertions": [
                        {
                            "name": "protected_sources_unchanged_during_run",
                            "passed": False,
                            "observed": str(exc),
                            "expected": "source hashes unchanged",
                        }
                    ],
                    "failures": ["protected_sources_unchanged_during_run"],
                }
            )
        )
        try:
            source_after = source_hashes()
        except Exception:
            source_after = {}
    except BaseException as exc:
        top_error = f"{type(exc).__name__}: {exc}"
        source_drift = source_drift or isinstance(exc, SourceDriftError)
        if not cases:
            cases.append(
                _finish_case(
                    {
                        "name": "runner_setup",
                        "started_at_utc": started_at,
                        "assertions": [
                            {
                                "name": "runner_completed_without_top_level_exception",
                                "passed": False,
                                "observed": top_error,
                                "expected": "no exception",
                            }
                        ],
                        "failures": ["runner_completed_without_top_level_exception"],
                    }
                )
            )
        try:
            source_after = source_hashes()
        except Exception:
            source_after = {}

    cleanup_ok = bool(cases) and all(
        case.get("cleanup", {}).get("temporary_root_removed") is True
        and case.get("child_shutdown", {}).get("residual") is not True
        for case in cases
        if case.get("name") in CASE_NAMES
    )
    all_requested_cases_passed = len(cases) == len(CASE_NAMES) and all(
        case.get("name") in CASE_NAMES and case.get("passed") is True for case in cases
    )
    known_defect_signature = (
        len(cases) == len(CASE_NAMES)
        and cases[0].get("passed") is True
        and cases[1].get("known_defect_reproduced") is True
        and cases[2].get("known_defect_reproduced") is True
    )
    if source_drift:
        status = "failed"
        exit_code = 1
    elif all_requested_cases_passed and cleanup_ok:
        status = "passed"
        exit_code = 0
    elif arguments.expect_known_defect and known_defect_signature and cleanup_ok:
        status = "known_defect_reproduced"
        exit_code = 0
    else:
        status = "failed"
        exit_code = 1

    finished_at = utc_now()
    report: Dict[str, Any] = {
        "schema": "oni.a09a3.approval-custody-integration.v1",
        "status": status,
        "passed": status == "passed",
        "exit_code": exit_code,
        "offline_only": True,
        "game_launched": False,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "source_hashes_before": source_before,
        "source_hashes_after": source_after,
        "source_drift": source_drift,
        "top_level_error": top_error,
        "case_counts": {
            "requested": len(CASE_NAMES),
            "executed": sum(1 for case in cases if case.get("name") in CASE_NAMES),
            "passed": sum(1 for case in cases if case.get("passed") is True),
            "failed": sum(1 for case in cases if case.get("passed") is False),
            "blocked": sum(1 for case in cases if case.get("status") == "blocked"),
        },
        "cases": cases,
        "cleanup_gate": {
            "temporary_roots_removed": cleanup_ok,
            "child_residuals": [
                case.get("child_shutdown")
                for case in cases
                if case.get("child_shutdown", {}).get("residual") is True
            ],
            "report_written_after_temporary_cleanup": True,
        },
        "diagnostic_mode": {"expect_known_defect": bool(arguments.expect_known_defect)},
    }
    if report_root is None:
        # Allocation itself is intentionally before any fixture mutation.  A
        # failed allocation has no safe report location and is reported below.
        try:
            report_root = _allocate_report_root(arguments.report_root)
        except Exception as exc:
            print(f"could not allocate report root: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    try:
        report_path = _write_report(report_root, report)
    except Exception as exc:
        print(f"could not write report: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"report": str(report_path), "status": status, "exit_code": exit_code}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
