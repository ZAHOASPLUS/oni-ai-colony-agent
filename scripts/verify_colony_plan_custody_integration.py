#!/usr/bin/env python3
"""Bounded offline evidence for colony-plan lifecycle custody boundaries."""

from __future__ import annotations

import argparse
import contextlib
import copy
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
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import action_outcomes  # noqa: E402
from agent import action_policy  # noqa: E402
from agent import command_custody as cc  # noqa: E402
from agent import command_queue as queue  # noqa: E402
from agent import colony_plan as cp  # noqa: E402
from agent import decision  # noqa: E402
from agent import decision_frame_archive  # noqa: E402
from scripts import verify_colony_plan as legacy  # noqa: E402
from scripts import verify_decision_frame as legacy_frame_support  # noqa: E402

OBSERVATION_WINDOW_SECONDS = 0.8
CHILD_READY_TIMEOUT_SECONDS = 3.0
CHILD_COMPLETION_TIMEOUT_SECONDS = 4.0
CHILD_JOIN_TIMEOUT_SECONDS = 3.0
CHILD_TERMINATE_JOIN_TIMEOUT_SECONDS = 1.0
CASE_HARD_TIMEOUT_SECONDS = 20.0
EXPECTED_CASE_NAMES = (
    "valid_plan_lifecycle_baseline",
    "lifecycle_lock_excludes_advance",
    "lifecycle_lock_excludes_abort",
    "stale_quarantine_payload_rejected",
)
SOURCE_FILES = (
    pathlib.Path(__file__).resolve(),
    PROJECT_ROOT / "agent" / "colony_plan.py",
    PROJECT_ROOT / "agent" / "command_custody.py",
    PROJECT_ROOT / "agent" / "command_queue.py",
    PROJECT_ROOT / "agent" / "action_outcomes.py",
    PROJECT_ROOT / "scripts" / "verify_colony_plan.py",
    PROJECT_ROOT / "agent" / "action_policy.py",
    PROJECT_ROOT / "agent" / "decision.py",
    PROJECT_ROOT / "agent" / "decision_frame_archive.py",
    PROJECT_ROOT / "scripts" / "verify_decision_frame.py",
)
EXPECTED_MODULES = {
    "verifier": SOURCE_FILES[0], "colony_plan": SOURCE_FILES[1], "command_custody": SOURCE_FILES[2],
    "command_queue": SOURCE_FILES[3], "action_outcomes": SOURCE_FILES[4], "legacy": SOURCE_FILES[5],
    "action_policy": SOURCE_FILES[6], "decision": SOURCE_FILES[7],
    "decision_frame_archive": SOURCE_FILES[8], "legacy_frame_support": SOURCE_FILES[9],
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _hash_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _json_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _hash_bytes(raw)


def _key(path: pathlib.Path) -> str:
    return os.path.relpath(str(path), str(PROJECT_ROOT)).replace("\\", "/")


def _capture_hashes(paths: Tuple[pathlib.Path, ...]) -> Tuple[Dict[str, Optional[str]], Dict[str, str]]:
    hashes: Dict[str, Optional[str]] = {}
    errors: Dict[str, str] = {}
    for path in paths:
        try:
            hashes[_key(path)] = _hash_file(path)
        except BaseException as exc:
            hashes[_key(path)] = None
            errors[_key(path)] = f"{type(exc).__name__}: {exc}"
    return hashes, errors


def _mtime(metadata: os.stat_result) -> int:
    return int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000)))


def _relative(path: pathlib.Path, root: pathlib.Path) -> str:
    return os.path.relpath(str(path), str(root)).replace("\\", "/")


def _fingerprint(path: pathlib.Path, root: pathlib.Path) -> Dict[str, Any]:
    """Capture metadata/digest only; reports never contain queue file bytes."""
    result: Dict[str, Any] = {"name": _relative(path, root)}
    try:
        first = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(first.st_mode) or int(first.st_nlink) != 1:
            result["error"] = "not_a_private_regular_file"
            return result
        payload = path.read_bytes()
        second = path.lstat()
        result.update({"bytes": len(payload), "sha256": _hash_bytes(payload),
                       "identity": [int(second.st_dev), int(second.st_ino)], "mtime_ns": _mtime(second)})
        if (result["identity"] != [int(first.st_dev), int(first.st_ino)]
                or result["bytes"] != int(first.st_size) or result["mtime_ns"] != _mtime(first)):
            result["error"] = "file_changed_during_observation"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _json_metadata(path: pathlib.Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except BaseException as exc:
        return {"json_error": type(exc).__name__}
    if not isinstance(value, dict):
        return {"json_error": "not_an_object"}
    return {field: value[field] for field in
            ("id", "status", "plan_id", "revision", "revision_hash", "open_command_id") if field in value}


def _commands_snapshot(root: pathlib.Path) -> Dict[str, Any]:
    stages: Dict[str, Any] = {}
    errors: List[str] = []
    for stage in queue.COMMAND_DIRS:
        directory, entries = root / "commands" / stage, []
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except BaseException as exc:
            children, errors = [], errors + [f"commands/{stage}: {type(exc).__name__}: {exc}"]
        for path in children:
            entry = _fingerprint(path, root)
            if "error" not in entry and path.name.lower().endswith(".json"):
                entry.update(_json_metadata(path))
            if "error" in entry:
                errors.append(f"{entry['name']}: {entry['error']}")
            entries.append(entry)
        stages[stage] = {"count": len(entries), "sha256": _json_digest(entries), "files": entries}
    return {"stages": stages, "stage_counts": {s: v["count"] for s, v in stages.items()},
            "stage_hashes": {s: v["sha256"] for s, v in stages.items()},
            "queue_sha256": _json_digest(stages), "errors": errors}


def _plan_snapshot(root: pathlib.Path) -> Dict[str, Any]:
    plan_root, paths = root / "colony-plans", [root / "colony-plan.json"]
    try:
        paths += [p for p in plan_root.rglob("*") if p.is_file() or p.is_symlink()]
    except BaseException:
        paths.append(plan_root)
    paths = list({str(p): p for p in paths}.values())
    entries, errors, metadata = [], [], {}
    for path in sorted(paths, key=str):
        if not path.exists() and path != root / "colony-plan.json":
            continue
        entry = _fingerprint(path, root)
        if "error" not in entry and path.name.lower().endswith(".json"):
            value = _json_metadata(path)
            entry.update(value)
            metadata[entry["name"]] = value
        if "error" in entry:
            errors.append(f"{entry['name']}: {entry['error']}")
        entries.append(entry)
    pointer = metadata.get("colony-plans/active.json", {})
    plan_id = pointer.get("plan_id")
    current = metadata.get(f"colony-plans/{plan_id}/current.json", {}) if isinstance(plan_id, str) else {}
    return {"files": entries, "file_count": len(entries), "plan_sha256": _json_digest(entries),
            "revision": current.get("revision"), "revision_hash": current.get("revision_hash"),
            "current": {k: current.get(k) for k in ("plan_id", "revision", "revision_hash", "status", "open_command_id")},
            "pointer": {k: pointer.get(k) for k in ("plan_id", "revision", "revision_hash", "status")},
            "errors": errors}


def _snapshot(root: pathlib.Path) -> Dict[str, Any]:
    return {"commands": _commands_snapshot(root), "plan": _plan_snapshot(root)}


def _snapshot_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> Dict[str, Any]:
    bq, aq = before.get("commands", {}), after.get("commands", {})
    changed_stages = [s for s in queue.COMMAND_DIRS if bq.get("stage_hashes", {}).get(s) != aq.get("stage_hashes", {}).get(s)]
    changed_files: List[Dict[str, Any]] = []
    for stage in queue.COMMAND_DIRS:
        bf = {x.get("name"): x for x in bq.get("stages", {}).get(stage, {}).get("files", [])}
        af = {x.get("name"): x for x in aq.get("stages", {}).get(stage, {}).get("files", [])}
        changed_files += [{"stage": stage, "name": n} for n in sorted(set(bf) | set(af)) if bf.get(n) != af.get(n)]
    bp, ap = before.get("plan", {}), after.get("plan", {})
    return {"changed": bool(changed_stages or changed_files or bp != ap), "changed_stages": changed_stages,
            "changed_files": changed_files, "plan_changed": bp != ap,
            "plan_revision_before": bp.get("revision"), "plan_revision_after": ap.get("revision"),
            "plan_hash_before": bp.get("revision_hash"), "plan_hash_after": ap.get("revision_hash"),
            "queue_errors": aq.get("errors", []), "plan_errors": ap.get("errors", [])}


def _records(root: pathlib.Path, stage: Optional[str] = None) -> List[Tuple[str, pathlib.Path, Dict[str, Any]]]:
    result: List[Tuple[str, pathlib.Path, Dict[str, Any]]] = []
    for name in ((stage,) if stage else tuple(queue.COMMAND_DIRS)):
        for path in sorted((root / "commands" / name).glob("*.json"), key=lambda p: p.name):
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            except BaseException:
                continue
            if isinstance(value, dict):
                result.append((name, path, value))
    return result


def _owners(root: pathlib.Path, command_id: str) -> List[Tuple[str, pathlib.Path, Dict[str, Any]]]:
    return [item for item in _records(root) if item[2].get("id") == command_id]


def _one(root: pathlib.Path, stage: str) -> Tuple[pathlib.Path, Dict[str, Any]]:
    records = _records(root, stage)
    if len(records) != 1:
        raise AssertionError(f"expected one {stage} command, found {len(records)}")
    return records[0][1], records[0][2]


def _new_case(name: str, child: bool = False) -> Dict[str, Any]:
    return {"name": name, "executed": True, "requires_child": child, "started_at_utc": utc_now(), "assertions": []}


def _check(case: Dict[str, Any], name: str, passed: bool, observed: Any = None, expected: Any = None) -> None:
    item: Dict[str, Any] = {"name": name, "passed": bool(passed)}
    if observed is not None:
        item["observed"] = observed
    if expected is not None:
        item["expected"] = expected
    case.setdefault("assertions", []).append(item)
    if not passed:
        case.setdefault("failures", []).append(name)


def _finish(case: Dict[str, Any]) -> Dict[str, Any]:
    assertions, failures = list(case.get("assertions", [])), list(dict.fromkeys(case.get("failures", [])))
    if not assertions:
        failures.append("zero_assertions")
    if case.get("status_error"):
        failures.append("setup_or_case_exception")
    if case.get("cleanup", {}).get("temporary_root_removed") is not True:
        failures.append("cleanup_failed")
    if case.get("requires_child"):
        child = case.get("child_shutdown")
        if not isinstance(child, Mapping):
            failures.append("child_shutdown_missing")
        else:
            if child.get("started") is not True:
                failures.append("child_start_failed")
            if child.get("exitcode") != 0:
                failures.append("child_nonzero_exit")
            if child.get("terminated_by_runner") is True:
                failures.append("child_terminated")
            if child.get("residual") is not False:
                failures.append("child_residual_or_unknown")
            if child.get("shutdown_error") or child.get("release_error") or child.get("close_errors"):
                failures.append("child_shutdown_error")
    failures = list(dict.fromkeys(str(x) for x in failures))
    passed_count = sum(x.get("passed") is True for x in assertions)
    case.update({"failures": failures, "assertion_counts": {"total": len(assertions), "passed": passed_count,
                  "failed": len(assertions) - passed_count}, "failure_count": len(failures)})
    case["status"] = "passed" if assertions and not failures and passed_count == len(assertions) else "failed"
    case["passed"], case["finished_at_utc"] = case["status"] == "passed", utc_now()
    return case


def _record_cleanup(case: Dict[str, Any], root_text: str) -> None:
    try:
        removed = bool(root_text) and not pathlib.Path(root_text).exists()
        error = None
    except BaseException as exc:
        removed, error = False, f"{type(exc).__name__}: {exc}"
    case["cleanup"] = {"temporary_root": root_text or None, "temporary_root_removed": removed}
    if error:
        case["cleanup"]["error"] = error
    _check(case, "temporary_root_removed_after_context_exit", removed, removed, True)


def _run_case(case: Dict[str, Any], prefix: str, body: Any, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    root_text = ""
    try:
        with contextlib.ExitStack() as stack:
            root_text = stack.enter_context(tempfile.TemporaryDirectory(prefix=f"a09a7-{prefix}-"))
            body(pathlib.Path(root_text), stack, state if state is not None else {})
    except BaseException as exc:
        case["status_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if state is not None and state.get("shutdown") is not None:
            case["child_shutdown"] = state["shutdown"]
        _record_cleanup(case, root_text)
    return _finish(case)


def _child_worker(bridge_text: str, operation: str, ready: Any, pre_call: Any, proceed: Any, connection: Any) -> None:
    try:
        connection.send({"event": "ready", "pid": os.getpid()})
        ready.set()
        if not proceed.wait(CHILD_READY_TIMEOUT_SECONDS):
            raise RuntimeError("child proceed handshake timed out")
        pre_call.set()
        connection.send({"event": "pre_call", "phase": "pre_call", "operation": operation})
        bridge = pathlib.Path(bridge_text)
        result = cp.advance_colony_plan(bridge) if operation == "advance" else cp.abort_colony_plan(bridge)
        if operation not in {"advance", "abort"}:
            raise ValueError(f"unsupported child operation: {operation}")
        connection.send({"event": "outcome", "result": "success", "operation": operation,
                         "status": result.get("status") if isinstance(result, dict) else None,
                         "revision": result.get("revision") if isinstance(result, dict) else None,
                         "open_command_id": result.get("open_command_id") if isinstance(result, dict) else None})
    except BaseException as exc:
        try:
            connection.send({"event": "worker_error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            raise
    finally:
        try:
            connection.close()
        except BaseException:
            pass


def _shutdown(state: Dict[str, Any]) -> Dict[str, Any]:
    if state.get("shutdown") is not None:
        return state["shutdown"]
    release_error = None
    try:
        if state.get("proceed") is not None:
            state["proceed"].set()
    except BaseException as exc:
        release_error = f"{type(exc).__name__}: {exc}"
    process = state.get("process")
    if process is None or process.pid is None:
        result: Dict[str, Any] = {"started": False, "pid": None if process is None else process.pid,
                                  "exitcode": None if process is None else process.exitcode, "residual": None}
    else:
        terminated, error, residual = False, None, None
        try:
            process.join(CHILD_JOIN_TIMEOUT_SECONDS)
            residual = bool(process.is_alive())
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
        if residual is True:
            terminated = True
            try:
                process.terminate()
                process.join(CHILD_TERMINATE_JOIN_TIMEOUT_SECONDS)
                residual = bool(process.is_alive())
            except BaseException as exc:
                residual, error = None, f"{type(exc).__name__}: {exc}"
        result = {"started": True, "pid": process.pid, "exitcode": process.exitcode,
                  "terminated_by_runner": terminated, "residual": residual}
        if error:
            result["shutdown_error"] = error
    if release_error:
        result["release_error"] = release_error
    close_errors = []
    for connection in state.get("connections", []):
        try:
            connection.close()
        except BaseException as exc:
            close_errors.append(f"{type(exc).__name__}: {exc}")
    if close_errors:
        result["close_errors"] = close_errors
    state["shutdown"] = result
    return result


def _spawn_child(root: pathlib.Path, operation: str, stack: contextlib.ExitStack, state: Dict[str, Any]) -> None:
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    ready, pre_call, proceed = context.Event(), context.Event(), context.Event()
    process = context.Process(target=_child_worker, args=(str(root), operation, ready, pre_call, proceed, child),
                               name=f"a09a7-{operation}-child")
    state.update({"process": process, "ready": ready, "pre_call": pre_call, "proceed": proceed,
                  "parent": parent, "connections": [parent]})
    # Registered after TemporaryDirectory: LIFO cleanup joins the child before temp deletion.
    stack.callback(_shutdown, state)
    try:
        process.start()
    finally:
        try:
            child.close()
        except BaseException:
            pass


def _receive(connection: Any, deadline: float) -> Optional[Dict[str, Any]]:
    while time.monotonic() < deadline:
        try:
            if connection.poll(min(0.05, max(0.0, deadline - time.monotonic()))):
                value = connection.recv()
                return value if isinstance(value, dict) else {"event": "pipe_error", "value_type": type(value).__name__}
        except BaseException as exc:
            return {"event": "pipe_error", "error": f"{type(exc).__name__}: {exc}"}
    return None


def _drain(connection: Any) -> List[Dict[str, Any]]:
    result = []
    while True:
        try:
            if not connection.poll(0):
                return result
            value = connection.recv()
        except BaseException as exc:
            result.append({"event": "pipe_error", "error": f"{type(exc).__name__}: {exc}"})
            return result
        result.append(value if isinstance(value, dict) else {"event": "pipe_error", "value_type": type(value).__name__})


def _fixture(root: pathlib.Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    initial = legacy.state(priority=2)
    legacy.write_live_state(root, initial)
    queue.ensure_command_dirs(root)
    draft = cp.create_colony_plan(root, legacy.priority_definition(initial, [4]))
    return initial, draft


def _legal_stale_variant(command: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    variant = copy.deepcopy(command)
    args = dict(variant.get("args") or {})
    priority = 3 if args.get("priority") != 3 else 2
    args["priority"], variant["args"] = priority, args
    return variant, priority


def _case_baseline() -> Dict[str, Any]:
    case = _new_case("valid_plan_lifecycle_baseline")

    def body(root: pathlib.Path, _stack: contextlib.ExitStack, _state: Dict[str, Any]) -> None:
        initial = legacy.state(priority=2)
        legacy.write_live_state(root, initial)
        queue.ensure_command_dirs(root)
        draft = cp.create_colony_plan(root, legacy.priority_definition(initial, [4]))
        active = cp.advance_colony_plan(root)
        path, pending = _one(root, "pending")
        _check(case, "status_after_create_is_draft", draft.get("status") == "draft", draft.get("status"), "draft")
        _check(case, "status_after_advance_is_active", active.get("status") == "active", active.get("status"), "active")
        owners = _owners(root, str(pending.get("id")))
        _check(case, "advance_has_unique_pending_requires_approval", len(owners) == 1 and owners[0][0] == "pending" and pending.get("requires_approval") is True, {"owners": [x[0] for x in owners], "requires_approval": pending.get("requires_approval")}, "one pending/true")
        aborted = cp.abort_colony_plan(root, str(active["plan_id"]))
        qpath, quarantined = _one(root, "quarantine")
        after_owners = _owners(root, str(pending.get("id")))
        source_matches = all(quarantined.get(k) == pending.get(k) for k in ("id", "source", "source_snapshot_id", "state_hash"))
        _check(case, "status_after_abort_is_aborted", aborted.get("status") == "aborted", aborted.get("status"), "aborted")
        _check(case, "quarantine_matches_id_and_source", source_matches, source_matches, True)
        _check(case, "quarantine_reason_is_exact", quarantined.get("quarantine_reason") == "colony_plan_aborted", quarantined.get("quarantine_reason"), "colony_plan_aborted")
        _check(case, "quarantine_has_unique_owner", len(after_owners) == 1 and after_owners[0][0] == "quarantine" and after_owners[0][1] == qpath, [x[0] for x in after_owners], ["quarantine"])
        _check(case, "pending_disappeared", not list((root / "commands" / "pending").glob("*.json")), len(list((root / "commands" / "pending").glob("*.json"))), 0)
        _check(case, "status_sequence_is_draft_active_aborted", [draft.get("status"), active.get("status"), aborted.get("status")] == ["draft", "active", "aborted"], [draft.get("status"), active.get("status"), aborted.get("status")], ["draft", "active", "aborted"])
        case["compact"] = {"plan_id": aborted.get("plan_id"), "revision": aborted.get("revision"), "command_id": pending.get("id")}

    return _run_case(case, "baseline", body)


def _case_lock(operation: str) -> Dict[str, Any]:
    name = "lifecycle_lock_excludes_advance" if operation == "advance" else "lifecycle_lock_excludes_abort"
    case, state = _new_case(name, True), {}

    def body(root: pathlib.Path, stack: contextlib.ExitStack, shared: Dict[str, Any]) -> None:
        _initial, draft = _fixture(root)
        if operation == "abort":
            active = cp.advance_colony_plan(root, str(draft["plan_id"]))
            _check(case, "abort_fixture_is_active", active.get("status") == "active", active.get("status"), "active")
        else:
            _check(case, "advance_fixture_is_draft_only", draft.get("status") == "draft", draft.get("status"), "draft")
        deadline = time.monotonic() + CASE_HARD_TIMEOUT_SECONDS
        during: List[Dict[str, Any]] = []
        transcript: List[Dict[str, Any]] = []
        with cc.lifecycle_custody(root, timeout_seconds=3.0):
            before = _snapshot(root)
            _spawn_child(root, operation, stack, shared)
            parent = shared["parent"]
            ready = shared["ready"].wait(CHILD_READY_TIMEOUT_SECONDS)
            ready_message = _receive(parent, min(deadline, time.monotonic() + CHILD_READY_TIMEOUT_SECONDS))
            _check(case, "child_ready_handshake", ready and ready_message and ready_message.get("event") == "ready", ready_message, "ready")
            if not ready or not ready_message or ready_message.get("event") != "ready":
                raise AssertionError("child ready handshake failed")
            shared["proceed"].set()
            pre = shared["pre_call"].wait(CHILD_READY_TIMEOUT_SECONDS)
            pre_message = _receive(parent, min(deadline, time.monotonic() + CHILD_READY_TIMEOUT_SECONDS))
            transcript.append(pre_message or {"event": "pre_call_timeout"})
            _check(case, "child_pre_call_handshake", pre and pre_message and pre_message.get("phase") == "pre_call", pre_message, "pre_call before API")
            if not pre or not pre_message or pre_message.get("phase") != "pre_call":
                raise AssertionError("child pre-call handshake failed")
            samples, sample_count = [], 0
            sample_changed_count, sample_error_count = 0, 0
            stop = min(deadline, time.monotonic() + OBSERVATION_WINDOW_SECONDS)
            while time.monotonic() < stop:
                current, diff = _snapshot(root), None
                diff = _snapshot_diff(before, current)
                sample_count += 1
                sample_changed_count += int(diff.get("changed") is True)
                sample_error_count += int(bool(diff.get("queue_errors") or diff.get("plan_errors")))
                if len(samples) < 4:
                    samples.append(diff)
                messages = _drain(parent)
                transcript += messages
                during += [m for m in messages if m.get("event") in {"outcome", "worker_error", "pipe_error"}]
                remaining = stop - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.05, remaining))
            locked = _snapshot(root)
            locked_diff = _snapshot_diff(before, locked)
            sample_count += 1
            sample_changed_count += int(locked_diff.get("changed") is True)
            sample_error_count += int(bool(locked_diff.get("queue_errors") or locked_diff.get("plan_errors")))
            messages = _drain(parent)
            transcript += messages
            during += [m for m in messages if m.get("event") in {"outcome", "worker_error", "pipe_error"}]
            case["lock_window"] = {"sample_count": sample_count, "sample_changed_count": sample_changed_count,
                                    "sample_error_count": sample_error_count, "samples": samples,
                                    "before": before, "final": {"snapshot": locked, "diff": locked_diff},
                                    "transcript_events": [m.get("event") for m in transcript]}
            _check(case, "multiple_actual_lock_window_samples", sample_count >= 2, sample_count, ">=2")
            _check(case, "every_lock_window_sample_unchanged", sample_changed_count == 0 and sample_error_count == 0 and not locked_diff.get("changed") and not locked_diff.get("queue_errors") and not locked_diff.get("plan_errors"), {"sample_changed_count": sample_changed_count, "sample_error_count": sample_error_count, "final": locked_diff}, "all samples no queue/plan diff")
            _check(case, "no_child_outcome_while_locked", not during, during, [])
        outcome = _receive(parent, min(deadline, time.monotonic() + CHILD_COMPLETION_TIMEOUT_SECONDS))
        case["handshake_transcript"] = transcript + ([outcome] if outcome else [])
        case["child_shutdown"] = _shutdown(shared)
        final_outcome = during[0] if during else outcome
        _check(case, "child_succeeds_after_lock_release", isinstance(final_outcome, dict) and final_outcome.get("event") == "outcome" and final_outcome.get("result") == "success", final_outcome, {"event": "outcome", "result": "success"})
        if operation == "advance":
            plan, pending = cp.load_active_plan(root), _records(root, "pending")
            pid = pending[0][2].get("id") if len(pending) == 1 else None
            owners = _owners(root, str(pid)) if pid else []
            _check(case, "released_advance_is_active", isinstance(final_outcome, dict) and final_outcome.get("status") == "active", None if not isinstance(final_outcome, dict) else final_outcome.get("status"), "active")
            _check(case, "released_advance_has_unique_pending", len(pending) == 1 and pending[0][2].get("requires_approval") is True, len(pending), "one pending/true")
            _check(case, "released_advance_points_to_pending", plan.get("status") == "active" and plan.get("open_command_id") == pid and len(owners) == 1 and owners[0][0] == "pending", {"status": plan.get("status"), "open_command_id": plan.get("open_command_id"), "owners": [x[0] for x in owners]}, "active with one pending")
        else:
            plan, quarantine = cp.load_active_plan(root), _records(root, "quarantine")
            qid = quarantine[0][2].get("id") if len(quarantine) == 1 else None
            owners = _owners(root, str(qid)) if qid else []
            _check(case, "released_abort_is_aborted", isinstance(final_outcome, dict) and final_outcome.get("status") == "aborted", None if not isinstance(final_outcome, dict) else final_outcome.get("status"), "aborted")
            _check(case, "released_abort_has_unique_quarantine", len(quarantine) == 1 and len(owners) == 1 and owners[0][0] == "quarantine", {"count": len(quarantine), "owners": [x[0] for x in owners]}, "one quarantine")
            _check(case, "released_abort_has_no_pending", not list((root / "commands" / "pending").glob("*.json")), len(list((root / "commands" / "pending").glob("*.json"))), 0)
            _check(case, "released_abort_plan_is_terminal", plan.get("status") == "aborted" and plan.get("open_command_id") is None, {"status": plan.get("status"), "open_command_id": plan.get("open_command_id")}, {"status": "aborted", "open_command_id": None})

    return _run_case(case, operation, body, state)


def _case_stale() -> Dict[str, Any]:
    case = _new_case("stale_quarantine_payload_rejected")

    def body(root: pathlib.Path, _stack: contextlib.ExitStack, _state: Dict[str, Any]) -> None:
        _initial, draft = _fixture(root)
        active = cp.advance_colony_plan(root, str(draft["plan_id"]))
        pending_path, pending_a = _one(root, "pending")
        _check(case, "stale_A_baseline_is_active", active.get("status") == "active", active.get("status"), "active")
        if active.get("status") != "active":
            raise AssertionError("stale A baseline did not become active")
        pending_a, plan_before = copy.deepcopy(pending_a), _snapshot(root)["plan"]
        stale_b, new_priority = _legal_stale_variant(pending_a)
        old_priority = (pending_a.get("args") or {}).get("priority")

        control_root = root / "positive-control"
        _control_initial, control_draft = _fixture(control_root)
        control_active = cp.advance_colony_plan(control_root, str(control_draft["plan_id"]))
        control_path, control_a = _one(control_root, "pending")
        control_b, control_priority = _legal_stale_variant(control_a)
        queue.write_json_atomic(control_path, control_b)
        control_expected = copy.deepcopy(control_b)
        control_target: Optional[pathlib.Path] = None
        control_error: Optional[BaseException] = None
        try:
            control_target = cp._quarantine_pending_command(control_root, control_path, control_expected, str(control_active["plan_id"]))
        except (cp.ColonyPlanError, cc.CommandCustodyError) as exc:
            control_error = exc
        control_quarantine = _records(control_root, "quarantine")
        control_owners = _owners(control_root, str(control_b.get("id")))
        positive_ok = (control_active.get("status") == "active" and control_error is None and control_target is not None and len(control_quarantine) == 1
                       and len(control_owners) == 1 and control_owners[0][0] == "quarantine"
                       and not list((control_root / "commands" / "pending").glob("*.json"))
                       and control_priority == new_priority)
        _check(case, "fresh_expected_body_quarantine_positive_control", positive_ok,
               {"error": None if control_error is None else type(control_error).__name__,
                "quarantine_count": len(control_quarantine), "owners": [x[0] for x in control_owners],
                "priority": control_priority}, True)
        if not positive_ok:
            raise AssertionError("fresh expected B positive control failed; stale A was not attempted")

        # Test setup only: replace the pending envelope with the original writer.
        queue.write_json_atomic(pending_path, stale_b)
        stale_bytes, stale_meta = pending_path.read_bytes(), _fingerprint(pending_path, root)
        same_other = {k: v for k, v in stale_b.items() if k != "args"} == {k: v for k, v in pending_a.items() if k != "args"}
        valid_b = (stale_b.get("schema") == pending_a.get("schema") and stale_b.get("status") == pending_a.get("status")
                   and stale_b.get("id") == pending_a.get("id") and stale_b.get("source") == pending_a.get("source")
                   and same_other and isinstance(new_priority, int) and not isinstance(new_priority, bool) and 0 <= new_priority <= 5)
        _check(case, "replacement_fixture_is_valid_same_id_body", valid_b, {"old_priority": old_priority, "new_priority": new_priority, "same_other": same_other}, True)
        if not valid_b:
            raise AssertionError("stale B replacement fixture was not schema-valid")
        caught: Optional[BaseException] = None
        try:
            cp._quarantine_pending_command(root, pending_path, pending_a, str(active["plan_id"]))
        except (cp.ColonyPlanError, cc.CommandCustodyError) as exc:
            caught = exc
        _check(case, "stale_expected_source_is_rejected", caught is not None, None if caught is None else type(caught).__name__, "ColonyPlanError or CommandCustodyError")
        after_bytes = pending_path.read_bytes() if pending_path.exists() else b""
        after_meta = _fingerprint(pending_path, root) if pending_path.exists() else {}
        _check(case, "replacement_bytes_remain_complete", after_bytes == stale_bytes, _hash_bytes(after_bytes), _hash_bytes(stale_bytes))
        _check(case, "replacement_hash_identity_remain", after_meta.get("sha256") == stale_meta.get("sha256") and after_meta.get("identity") == stale_meta.get("identity"), {"sha256": after_meta.get("sha256"), "identity": after_meta.get("identity")}, {"sha256": stale_meta.get("sha256"), "identity": stale_meta.get("identity")})
        owners = _owners(root, str(pending_a.get("id")))
        _check(case, "stale_id_has_no_quarantine_owner", not any(x[0] == "quarantine" for x in owners), [x[0] for x in owners], "no quarantine")
        plan_after = _snapshot(root)["plan"]
        _check(case, "stale_rejection_does_not_abort_plan", plan_after == plan_before and plan_after.get("current", {}).get("status") != "aborted", {"unchanged": plan_after == plan_before, "status": plan_after.get("current", {}).get("status")}, "unchanged/not aborted")
        case["compact"] = {"plan_id": active.get("plan_id"), "command_id": pending_a.get("id"), "replacement": {"bytes": stale_meta.get("bytes"), "sha256": stale_meta.get("sha256"), "identity": stale_meta.get("identity")}}

    return _run_case(case, "stale", body)


def _blocked(name: str, reason: str) -> Dict[str, Any]:
    case = _new_case(name)
    case.update({"executed": False, "blocked": True, "status_error": reason,
                 "cleanup": {"temporary_root": None, "temporary_root_removed": True, "not_started": True}})
    _check(case, "case_blocked_without_fixture", False, reason, "case started only after baseline")
    return _finish(case)


def _module_paths() -> Dict[str, Any]:
    modules = {"verifier": sys.modules.get(__name__), "colony_plan": cp, "command_custody": cc, "command_queue": queue,
               "action_outcomes": action_outcomes, "legacy": legacy, "action_policy": action_policy, "decision": decision,
               "decision_frame_archive": decision_frame_archive, "legacy_frame_support": legacy_frame_support}
    result = {}
    for name, expected in EXPECTED_MODULES.items():
        module = modules.get(name)
        try:
            actual = pathlib.Path(module.__file__).resolve() if module is not None and module.__file__ else None
            error = None
        except BaseException as exc:
            actual, error = None, f"{type(exc).__name__}: {exc}"
        result[name] = {"expected": str(expected.resolve()), "actual": None if actual is None else str(actual),
                        "matches": actual == expected.resolve(), "error": error}
    return result


def _allocate_report_root() -> pathlib.Path:
    parent = PROJECT_ROOT / "reports"
    parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for _ in range(20):
        candidate = parent / f"a09a7-plan-custody-verifier-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        try:
            candidate.mkdir(exist_ok=False)
            return candidate
        except FileExistsError:
            pass
    raise FileExistsError("could not allocate unique plan-custody report root")


def main(argv: Optional[List[str]] = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    mp.freeze_support()
    report_root, started = _allocate_report_root(), utc_now()
    module_paths, source_before, source_before_errors = _module_paths(), *_capture_hashes(SOURCE_FILES)
    cases: List[Dict[str, Any]] = []
    fatal_error = None
    try:
        preflight_ok = all(x.get("matches") is True for x in module_paths.values()) and not source_before_errors
        if not preflight_ok:
            cases = [_blocked(name, "module path mismatch or source hash capture failed; all cases were blocked") for name in EXPECTED_CASE_NAMES]
        else:
            baseline = _case_baseline()
            cases.append(baseline)
            if baseline.get("passed") is True:
                cases += [_case_lock("advance"), _case_lock("abort"), _case_stale()]
            else:
                cases += [_blocked(name, "valid_plan_lifecycle_baseline failed; case was not started") for name in EXPECTED_CASE_NAMES[1:]]
    except BaseException as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
    for name in EXPECTED_CASE_NAMES:
        if not any(case.get("name") == name for case in cases):
            cases.append(_blocked(name, f"runner fatal error or missing case: {fatal_error or 'unknown'}"))
    source_after, source_after_errors = _capture_hashes(SOURCE_FILES)
    source_drift = source_before != source_after or source_before_errors != source_after_errors
    names, expected_count = [case.get("name") for case in cases], len(EXPECTED_CASE_NAMES)
    module_ok = all(x.get("matches") is True for x in module_paths.values())
    cleanup_ok = bool(cases) and all(x.get("cleanup", {}).get("temporary_root_removed") is True for x in cases)
    child_ok = all(x.get("requires_child") is not True or (x.get("child_shutdown", {}).get("started") is True
        and x.get("child_shutdown", {}).get("exitcode") == 0 and x.get("child_shutdown", {}).get("terminated_by_runner") is not True
        and x.get("child_shutdown", {}).get("residual") is False) for x in cases)
    names_ok = len(cases) == expected_count and all(names.count(name) == 1 for name in EXPECTED_CASE_NAMES)
    cases_ok = names_ok and all(x.get("passed") is True for x in cases)
    source_ok = not source_before_errors and not source_after_errors and not source_drift
    passed = fatal_error is None and module_ok and source_ok and cleanup_ok and child_ok and cases_ok
    report = {"schema": "oni.a09a7.colony-plan-custody-integration.v1", "status": "passed" if passed else "failed",
              "passed": passed, "exit_code": 0 if passed else 1, "started_at_utc": started, "finished_at_utc": utc_now(),
              "report_root": str(report_root), "offline_only": True, "live_admission": False, "game_launched": False,
              "network_accessed": False, "expected_cases": list(EXPECTED_CASE_NAMES), "module_paths": module_paths,
              "source_hashes_before": source_before, "source_hash_errors_before": source_before_errors,
              "source_hashes_after": source_after, "source_hash_errors_after": source_after_errors, "source_drift_detected": source_drift,
              "preflight": {"module_paths_match": module_ok, "source_before_captured": not source_before_errors,
                            "cases_started": any(x.get("executed") is True for x in cases) and module_ok and not source_before_errors},
              "case_counts": {"expected": expected_count, "requested": expected_count, "records": len(cases),
                              "executed": sum(x.get("executed") is True for x in cases), "blocked": sum(x.get("blocked") is True for x in cases),
                              "passed": sum(x.get("passed") is True for x in cases), "failed": sum(x.get("passed") is not True for x in cases)},
              "cleanup_gate": {"all_temporary_roots_removed": cleanup_ok}, "normal_child_gate": {"all_required_children_normal_exit": child_ok, "case_hard_timeout_seconds": CASE_HARD_TIMEOUT_SECONDS},
              "fatal_error": fatal_error, "cases": cases}
    path = report_root / "report.json"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(path), "exit_code": report["exit_code"]}, sort_keys=True))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
