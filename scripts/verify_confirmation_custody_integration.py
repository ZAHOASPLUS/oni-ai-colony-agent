#!/usr/bin/env python3
"""Offline custody regression verifier for confirmation commit and recovery.

This verifier intentionally imports the current workspace implementation.  It
does not copy, text-exec, patch, or emulate the production consumer.  Every
case owns a fresh TemporaryDirectory; the only persistent output is a unique
report directory under ``reports/``.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import queue
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = pathlib.Path(__file__).resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# These are deliberate real imports.  The runtime attestation below checks
# every imported implementation/fixture path before a case is allowed to run.
from agent import action_outcomes as action_outcomes_module
from agent import colony_plan as colony_plan_module
from agent import command_custody as command_custody_module
from agent import command_queue as command_queue_module
from agent.action_outcomes import (
    ACTION_OUTCOMES_SCHEMA,
    CONFIRMATION_CONFIRMED,
    ActionOutcomeError,
    build_action_outcomes,
    command_intent_hash,
    commit_confirmed_outcomes,
    confirmed_command_commit_blockers,
    read_action_outcome,
    recover_staged_confirmed_outcomes,
    stage_confirmed_outcomes_commit,
    validate_action_outcomes,
)
from agent.command_custody import (
    CommandCustodyConflict,
    CommandCustodyUnknown,
    LIFECYCLE_LOCK_NAME,
    lifecycle_custody,
)
from scripts import verify_action_outcomes as outcome_fixture
from scripts import verify_confirmation_transactions as confirmation_fixture


ACTION_ID = "ONI_A09A9_VERIFIER_AUTHOR_20260907_01"
CASE_NAMES = (
    "normal_commit_control",
    "commit_waits_for_lifecycle_lock",
    "recover_waits_for_lifecycle_lock",
    "stale_source_replacement_preserved",
)
WORKER_MODULE_NAMES = (
    "agent.action_outcomes",
    "agent.command_custody",
    "agent.command_queue",
    "agent.colony_plan",
    "scripts.verify_action_outcomes",
    "scripts.verify_confirmation_transactions",
)
WORKER_OPERATIONS = frozenset({"commit", "recover"})
WORKER_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
WORKER_MARKER_RE = re.compile(r"^\.oni-a09a9-worker-[0-9a-f]{32}\.json$")
MAX_CHILD_LINE = 4096
MAX_CHILD_EVENTS = 64
MAX_CHILD_OUTPUT_BYTES = 32 * 1024
MAX_CHILD_STDERR_BYTES = 8 * 1024
LOCK_SAMPLE_SECONDS = 0.50
LOCK_SAMPLE_INTERVAL_SECONDS = 0.02
CHILD_READY_TIMEOUT_SECONDS = 5.0
CHILD_POST_RELEASE_TIMEOUT_SECONDS = 20.0
_REPARSE_POINT = 0x0400

AUTHOR_BASELINE_SOURCE_HASHES = {
    "agent/action_outcomes.py": "C0FAB71E21F1E49CBE76927BF6CF4E16A7C0179BEDB290CC1EA11B4EA56060D7",
    "agent/command_custody.py": "6C0A0B995587CB18E243E4A96C56517FFE8A735923BCC5CEEDE750BA19E91763",
    "agent/command_queue.py": "4992E0605DCE7821AE8776E38AB30A2E1C805DD15F23C988E25488EFE4E8DEA1",
    "agent/colony_plan.py": "0E5B3D174699A1CC54F42CA749838D766176069C4B5F43AD2AD20B0D8D047ADD",
    "scripts/verify_confirmation_transactions.py": "157E191A5030BF0D64742AAA5575F7E845EDBAEFA7C5E4F338A3F442A7B09AC9",
    "scripts/verify_action_outcomes.py": "E2566D8B169D2815404CD3141A6BE5A5342BD6BF96DB396BEDE2F225B95C3152",
}

MODULE_PATHS = {
    "agent.action_outcomes": (action_outcomes_module, "agent/action_outcomes.py"),
    "agent.command_custody": (command_custody_module, "agent/command_custody.py"),
    "agent.command_queue": (command_queue_module, "agent/command_queue.py"),
    "agent.colony_plan": (colony_plan_module, "agent/colony_plan.py"),
    "scripts.verify_action_outcomes": (
        outcome_fixture,
        "scripts/verify_action_outcomes.py",
    ),
    "scripts.verify_confirmation_transactions": (
        confirmation_fixture,
        "scripts/verify_confirmation_transactions.py",
    ),
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _relative(path: pathlib.Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _read_json(path: pathlib.Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _write_json_create_only(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _module_attestation() -> Dict[str, Any]:
    modules: Dict[str, Any] = {}
    for name, (module, relative_path) in MODULE_PATHS.items():
        expected = (PROJECT_ROOT / relative_path).resolve()
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            raise AssertionError(f"{name} has no __file__")
        actual = pathlib.Path(module_file).resolve()
        # Keep this explicit: a shadow copy or text-exec replacement is not a
        # valid verifier execution.
        assert actual == expected, f"{name} imported from unexpected path: {actual}"
        modules[name] = {
            "expected_path": str(expected),
            "actual_path": str(actual),
            "sha256": _sha256_file(actual),
        }
    return modules


def _source_hash_snapshot() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for relative_path in AUTHOR_BASELINE_SOURCE_HASHES:
        path = PROJECT_ROOT / relative_path
        result[relative_path] = _sha256_file(path) if path.is_file() else "MISSING"
    for relative_path in (
        "scripts/verify_confirmation_custody_integration.py",
        "docs/a09a9-confirmation-custody-verifier-20260907.md",
    ):
        path = PROJECT_ROOT / relative_path
        result[relative_path] = _sha256_file(path) if path.is_file() else "MISSING"
    return result


def _author_baseline_comparison(snapshot: Mapping[str, str]) -> Dict[str, Dict[str, str]]:
    drift: Dict[str, Dict[str, str]] = {}
    for relative_path, expected in AUTHOR_BASELINE_SOURCE_HASHES.items():
        actual = snapshot.get(relative_path, "MISSING")
        if actual != expected:
            drift[relative_path] = {"expected": expected, "actual": actual}
    return drift


class CaseFailure(AssertionError):
    """A case assertion carrying evidence collected before the failure."""

    def __init__(self, message: str, details: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.details = copy.deepcopy(dict(details or {}))


class CaseContext:
    def __init__(self, name: str):
        self.name = name
        self.details: Dict[str, Any] = {}
        self.assertion_count = 0

    def note(self, **values: Any) -> None:
        self.details.update(values)

    def require(self, condition: bool, message: str) -> None:
        self.assertion_count += 1
        if not condition:
            raise CaseFailure(message, self.details)


def _identity(metadata: os.stat_result) -> List[int]:
    return [int(metadata.st_dev), int(metadata.st_ino)]


def _mtime_ns(metadata: os.stat_result) -> int:
    return int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000)))


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT
    )


def _stable_file_record(path: pathlib.Path, relative_path: str) -> Dict[str, Any]:
    before = os.lstat(path)
    if _is_reparse(before):
        raise RuntimeError(f"protected path is a symlink/reparse point: {path}")
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"protected file is not regular: {path}")
    raw = path.read_bytes()
    after = os.lstat(path)
    if (
        _identity(before) != _identity(after)
        or _mtime_ns(before) != _mtime_ns(after)
        or int(before.st_size) != int(after.st_size)
    ):
        raise RuntimeError(f"protected file changed while sampled: {path}")
    return {
        "relative": relative_path,
        "kind": "file",
        "identity": _identity(after),
        "mtime_ns": _mtime_ns(after),
        "size": len(raw),
        "sha256": _sha256_bytes(raw),
    }


def _lifecycle_lock_record(path: pathlib.Path, relative_path: str) -> Dict[str, Any]:
    metadata = os.lstat(path)
    if _is_reparse(metadata):
        raise RuntimeError(f"lifecycle lock is a symlink/reparse point: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"lifecycle lock is not a regular file: {path}")
    if int(metadata.st_nlink) != 1:
        raise RuntimeError(f"lifecycle lock is hard-linked: {path}")
    return {
        "relative": relative_path,
        "kind": "lifecycle-lock",
        "identity": _identity(metadata),
        "mtime_ns": _mtime_ns(metadata),
        "size": int(metadata.st_size),
        "nlink": int(metadata.st_nlink),
        "reparse": False,
    }


def _tree_records(
    path: pathlib.Path,
    relative_path: str,
    lifecycle_lock_path: Optional[pathlib.Path] = None,
) -> List[Dict[str, Any]]:
    metadata = os.lstat(path)
    if _is_reparse(metadata):
        raise RuntimeError(f"protected tree contains a symlink/reparse point: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"protected tree root is not a directory: {path}")
    records: List[Dict[str, Any]] = [
        {
            "relative": relative_path,
            "kind": "directory",
            "identity": _identity(metadata),
            "mtime_ns": _mtime_ns(metadata),
        }
    ]
    lock_seen = False
    with os.scandir(path) as entries:
        children = sorted(entries, key=lambda item: item.name)
        for entry in children:
            child = pathlib.Path(entry.path)
            child_relative = f"{relative_path}/{entry.name}"
            child_metadata = os.lstat(child)
            if lifecycle_lock_path is not None and child == lifecycle_lock_path:
                lock_seen = True
                records.append(_lifecycle_lock_record(child, child_relative))
                continue
            if _is_reparse(child_metadata):
                raise RuntimeError(f"protected tree contains a symlink/reparse point: {child}")
            if stat.S_ISDIR(child_metadata.st_mode):
                records.extend(_tree_records(child, child_relative, lifecycle_lock_path))
            elif stat.S_ISREG(child_metadata.st_mode):
                records.append(_stable_file_record(child, child_relative))
            else:
                raise RuntimeError(f"protected tree contains unsupported material: {child}")
    if lifecycle_lock_path is not None and path == lifecycle_lock_path.parent and not lock_seen:
        raise RuntimeError(f"lifecycle lock is absent from protected command tree: {lifecycle_lock_path}")
    return records


def _protected_snapshot(root: pathlib.Path, session_path: pathlib.Path) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []
    lifecycle_lock_path = root / "commands" / LIFECYCLE_LOCK_NAME
    records.extend(_tree_records(root / "commands", "commands", lifecycle_lock_path))
    for label, path in (
        ("action-outcomes.json", root / "action-outcomes.json"),
        ("session-action-outcomes.json", session_path),
        ("action-outcomes-commit.json", root / "action-outcomes-commit.json"),
    ):
        if path.exists():
            records.append(_stable_file_record(path, label))
        else:
            records.append({"relative": label, "kind": "absent"})
    records.sort(key=lambda item: str(item.get("relative")))
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"records": records, "digest": _sha256_bytes(encoded.encode("utf-8"))}


def _changed_records(before: Mapping[str, Any], after: Mapping[str, Any]) -> List[str]:
    left = {str(item["relative"]): item for item in before.get("records", [])}
    right = {str(item["relative"]): item for item in after.get("records", [])}
    return sorted(
        key
        for key in set(left) | set(right)
        if left.get(key) != right.get(key)
    )


def _safe_session_path(root: pathlib.Path, document: Mapping[str, Any]) -> pathlib.Path:
    source = document.get("source") if isinstance(document.get("source"), Mapping) else {}
    session_id = source.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise AssertionError("confirmation document has no session id")
    return root / "sessions" / session_id / "action-outcomes.json"


def _certificate_paths(root: pathlib.Path, document: Mapping[str, Any]) -> Tuple[pathlib.Path, pathlib.Path]:
    session_path = _safe_session_path(root, document)
    return root / "action-outcomes.json", session_path


def _command_locations(root: pathlib.Path, command_id: str) -> List[Tuple[str, pathlib.Path, Dict[str, Any]]]:
    found: List[Tuple[str, pathlib.Path, Dict[str, Any]]] = []
    for stage in command_queue_module.COMMAND_DIRS:
        directory = root / "commands" / stage
        metadata = os.lstat(directory)
        if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"command stage is not a plain directory: {directory}")
        try:
            with os.scandir(directory) as directory_entries:
                entries = sorted(directory_entries, key=lambda item: item.name)
        except OSError as exc:
            raise RuntimeError(f"command stage enumeration failed: {directory}: {exc}") from exc
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            path = pathlib.Path(entry.path)
            file_metadata = os.lstat(path)
            if _is_reparse(file_metadata) or not stat.S_ISREG(file_metadata.st_mode):
                raise RuntimeError(f"command record is not a plain file: {path}")
            try:
                value = _read_json(path)
            except Exception as exc:
                raise RuntimeError(f"command record read/JSON failure: {path}: {exc}") from exc
            if value.get("id") == command_id:
                found.append((stage, path, value))
    return found


def _inspect_final_state(
    root: pathlib.Path,
    document: Mapping[str, Any],
    command_id: str,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "command_id": command_id,
        "locations": [],
        "done_count": 0,
        "total_count": 0,
        "canonical_done": False,
        "canonical_confirming_exists": False,
        "confirming_exists": False,
        "certificate_valid": False,
        "certificate_blockers": ["not_checked"],
        "source_exists": False,
        "errors": [],
    }
    try:
        root_certificate, session_certificate = _certificate_paths(root, document)
        root_document = _read_json(root_certificate)
        session_document = _read_json(session_certificate)
        validate_action_outcomes(root_document)
        validate_action_outcomes(session_document)
        result["certificate_valid"] = root_document == session_document == dict(document)
        result["certificate_paths"] = [str(root_certificate), str(session_certificate)]
        outcomes = root_document.get("outcomes")
        outcome = next(
            (
                item
                for item in outcomes or []
                if isinstance(item, Mapping) and item.get("command_id") == command_id
            ),
            None,
        )
        if isinstance(outcome, Mapping):
            result["outcome_status"] = outcome.get("confirmation_status")
            result["outcome_blockers"] = list(outcome.get("blockers") or [])
        else:
            result["outcome_status"] = None
            result["outcome_blockers"] = ["action_outcome_missing"]
        locations = _command_locations(root, command_id)
        result["locations"] = [
            {"stage": stage, "path": str(path), "status": value.get("status")}
            for stage, path, value in locations
        ]
        result["done_count"] = sum(stage == "done" for stage, _, _ in locations)
        result["total_count"] = len(locations)
        canonical_done = root / "commands" / "done" / f"{command_id}.json"
        canonical_confirming = root / "commands" / "confirming" / f"{command_id}.json"
        result["canonical_done_path"] = str(canonical_done)
        result["canonical_confirming_path"] = str(canonical_confirming)
        result["canonical_done"] = (
            len(locations) == 1
            and locations[0][0] == "done"
            and locations[0][1] == canonical_done
        )
        result["canonical_confirming_exists"] = os.path.lexists(str(canonical_confirming))
        result["confirming_exists"] = any(stage == "confirming" for stage, _, _ in locations)
        result["source_exists"] = result["canonical_confirming_exists"]
        if isinstance(outcome, Mapping) and result["total_count"] == 1 and result["canonical_done"]:
            done = next(value for stage, _, value in locations if stage == "done")
            result["certificate_blockers"] = confirmed_command_commit_blockers(done, outcome)
        else:
            result["certificate_blockers"] = ["unique_done_or_outcome_missing"]
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


def _assert_final_state(
    context: CaseContext,
    root: pathlib.Path,
    document: Mapping[str, Any],
    command_id: str,
) -> Dict[str, Any]:
    inspected = _inspect_final_state(root, document, command_id)
    context.note(final_state=inspected)
    context.require(not inspected["errors"], "final custody inspection raised an error")
    context.require(inspected["certificate_valid"], "root/session certificates are not identical and valid")
    context.require(inspected.get("outcome_status") == CONFIRMATION_CONFIRMED, "certificate is not confirmed")
    context.require(inspected.get("outcome_blockers") == [], "confirmed certificate contains blockers")
    context.require(inspected["total_count"] == 1, "command does not have exactly one total owner")
    context.require(inspected["done_count"] == 1, "command does not have exactly one done owner")
    context.require(inspected["canonical_done"] is True, "done owner is not the canonical command path")
    context.require(not inspected["confirming_exists"], "confirming source still exists after commit")
    context.require(not inspected["canonical_confirming_exists"], "canonical confirming source still exists after commit")
    context.require(inspected["certificate_blockers"] == [], "done command is not the certificate commit")
    return inspected


def _case_normal_commit_control(context: CaseContext, root: pathlib.Path) -> Dict[str, Any]:
    command_id = "cmd-a09a9-normal-commit-control"
    document, outcome = confirmation_fixture.published_fixture(root, command_id)
    context.note(command_id=command_id, published_outcomes_hash=document.get("outcomes_hash"))
    context.require(outcome.get("confirmation_status") == CONFIRMATION_CONFIRMED, "fixture was not confirmed")
    context.require(outcome.get("blockers") == [], "fixture certificate contains blockers")
    marker = stage_confirmed_outcomes_commit(root, document)
    context.require(marker.get("schema") == "oni.action-outcomes-commit.v1", "commit journal schema is wrong")
    first = recover_staged_confirmed_outcomes(root)
    second = recover_staged_confirmed_outcomes(root)
    context.note(first_recovery=first, second_recovery=second)
    context.require(first == [command_id], "normal recovery did not commit the command")
    context.require(second == [command_id], "normal recovery re-entry was not idempotent")
    inspected = _assert_final_state(context, root, document, command_id)
    loaded, blockers = read_action_outcome(root, command_id)
    context.require(loaded is not None and blockers == [], "published action outcome could not be read")
    context.require(loaded.get("confirmation_status") == CONFIRMATION_CONFIRMED, "loaded certificate changed status")
    return {
        "command_id": command_id,
        "recovery_results": [first, second],
        "final_location": inspected["locations"],
    }


def _new_worker_ledger(operation: str, command_id: str) -> Dict[str, Any]:
    return {
        "operation": operation,
        "command_id": command_id,
        "phase": "created",
        "command": None,
        "marker": None,
        "marker_path": None,
        "marker_token": None,
        "marker_handle": None,
        "marker_handle_owned": False,
        "marker_handle_closed": False,
        "marker_write_handle": None,
        "marker_write_handle_owned": False,
        "marker_write_handle_closed": False,
        "marker_write_handle_registered": False,
        "marker_write_handle_close_error": None,
        "marker_write_handle_residual": False,
        "marker_read_handle": None,
        "marker_read_handle_owned": False,
        "marker_read_handle_closed": False,
        "marker_read_handle_close_error": None,
        "marker_read_handle_registered": False,
        "marker_read_handle_residual": False,
        "marker_read_handle_observations": [],
        "marker_creation_owned": False,
        "marker_identity": None,
        "marker_identity_registered": False,
        "marker_expected_content_sha256": None,
        "marker_content_sha256": None,
        "marker_binding_identity": None,
        "marker_binding_mtime_ns": None,
        "marker_binding_size": None,
        "marker_binding_observed": False,
        "marker_binding_content_checked": False,
        "marker_binding_error": None,
        "marker_binding_errors": [],
        "marker_binding_path_before": None,
        "marker_binding_path_after": None,
        "marker_binding_handle_before": None,
        "marker_binding_handle_after": None,
        "marker_handle_error": None,
        "marker_unlink_effect": "unattempted",
        "marker_unlink_effect_history": [],
        "marker_unlink_error": None,
        "marker_presence_checked": False,
        "marker_presence_state": "unobserved",
        "marker_presence_error": None,
        "marker_present_after": None,
        "process": None,
        "process_owned": False,
        "pid": None,
        "stdin": None,
        "stdout": None,
        "stderr": None,
        "stdout_reader": None,
        "stderr_reader": None,
        "readers": {"stdout": None, "stderr": None},
        "reader_registered": {"stdout": False, "stderr": False},
        "reader_start_attempted": {"stdout": False, "stderr": False},
        "reader_start_returned": {"stdout": False, "stderr": False},
        "reader_started": {"stdout": False, "stderr": False},
        "reader_start_errors": {"stdout": [], "stderr": []},
        "reader_start_unknown": {"stdout": False, "stderr": False},
        "reader_thread_lookup_errors": [],
        "reader_state_lookup_errors": [],
        "reader_eof_errors": [],
        "reader_aliveness_errors": [],
        "reader_lookup_error_cursors": {
            "thread": 0,
            "state": 0,
            "eof": 0,
            "aliveness": 0,
        },
        "ready": False,
        "go_attempted": False,
        "go_sent": False,
        "go_uncertain": False,
        "primary_error": None,
        "cleanup_errors": [],
        "cleanup_error": None,
        "cleanup_attempt_errors": [],
        "cleanup_attempt_forced_kill": False,
        "cleanup_attempt_residual_process": None,
        "cleanup_attempt_outcome": None,
        "forced_kill_history": [],
        "events": [],
        "stderr_lines": [],
        "stderr_bytes": 0,
        "observed_output_bytes": 0,
        "handshake_stage": "created",
        "pipe_overflow": False,
        "overflow_reason": None,
    }


_READER_LIFECYCLE_FIELDS = (
    "reader_registered",
    "reader_start_attempted",
    "reader_start_returned",
    "reader_started",
)


def _remember_reader_lifecycle_provenance(
    ledger: Dict[str, Any],
) -> Dict[str, Dict[str, bool]]:
    """Persist which lifecycle fields existed before ledger defaulting."""

    raw_provenance = ledger.get("reader_lifecycle_explicit")
    had_provenance = isinstance(raw_provenance, Mapping)
    provenance: Dict[str, Any] = (
        dict(raw_provenance) if had_provenance else {}
    )
    for label in ("stdout", "stderr"):
        raw_label = provenance.get(label)
        label_provenance: Dict[str, bool] = (
            dict(raw_label) if isinstance(raw_label, Mapping) else {}
        )
        for field in _READER_LIFECYCLE_FIELDS:
            if field in label_provenance:
                label_provenance[field] = label_provenance[field] is True
                continue
            if had_provenance:
                label_provenance[field] = False
                continue
            states = ledger.get(field)
            label_provenance[field] = isinstance(states, Mapping) and isinstance(
                states.get(label), bool
            )
        provenance[label] = label_provenance
    ledger["reader_lifecycle_explicit"] = provenance
    return provenance


def _ensure_worker_ledger(
    ledger: Optional[Dict[str, Any]], operation: str, command_id: str
) -> Dict[str, Any]:
    if ledger is None:
        ledger = _new_worker_ledger(operation, command_id)
    else:
        _remember_reader_lifecycle_provenance(ledger)
        defaults = _new_worker_ledger(operation, command_id)
        for key, value in defaults.items():
            ledger.setdefault(key, value)
        ledger["operation"] = operation
        ledger["command_id"] = command_id
    if "reader_lifecycle_explicit" not in ledger:
        _remember_reader_lifecycle_provenance(ledger)
    return ledger


def _explicit_reader_lifecycle(
    ledger: Mapping[str, Any],
    label: str,
    provenance: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, bool]]:
    """Return only lifecycle facts present before ledger defaults are applied."""

    evidence: Dict[str, bool] = {}
    label_provenance = None
    if provenance is not None:
        candidate = provenance.get(label)
        if not isinstance(candidate, Mapping):
            return None
        label_provenance = candidate
    for field in _READER_LIFECYCLE_FIELDS:
        if label_provenance is not None and label_provenance.get(field) is not True:
            return None
        states = ledger.get(field)
        if not isinstance(states, Mapping):
            return None
        value = states.get(label)
        if not isinstance(value, bool):
            return None
        evidence[field] = value
    return evidence


def _worker_error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _remember_worker_error(worker: Dict[str, Any], exc: BaseException) -> str:
    error = _worker_error_text(exc)
    if worker.get("primary_error") is None:
        worker["primary_error"] = error
    worker["last_error"] = error
    return error


def _capture_marker_binding(worker: Dict[str, Any], marker: pathlib.Path) -> bool:
    expected_identity = worker.get("marker_identity")
    if not worker.get("marker_creation_owned") or not worker.get("marker_identity_registered"):
        worker["marker_binding_error"] = "marker ownership or identity was not registered"
        return False
    max_marker_bytes = MAX_CHILD_LINE * 16
    worker["marker_binding_observed"] = False
    worker["marker_binding_error"] = None
    binding_errors = worker.get("marker_binding_errors")
    if not isinstance(binding_errors, list):
        binding_errors = []
        worker["marker_binding_errors"] = binding_errors

    def fail(exc: BaseException) -> bool:
        message = _worker_error_text(exc)
        binding_errors.append(message)
        worker["marker_binding_error"] = message
        return False

    def metadata_record(metadata: os.stat_result) -> Dict[str, Any]:
        return {
            "identity": list(_identity(metadata)),
            "type": int(stat.S_IFMT(metadata.st_mode)),
            "regular": bool(stat.S_ISREG(metadata.st_mode)),
            "reparse": bool(_is_reparse(metadata)),
            "size": int(metadata.st_size),
            "mtime_ns": _mtime_ns(metadata),
        }

    def records_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        return all(left.get(key) == right.get(key) for key in (
            "identity", "type", "regular", "reparse", "size", "mtime_ns"
        ))

    read_handle = worker.get("marker_read_handle")
    if read_handle is not None and worker.get("marker_read_handle_closed"):
        observations = worker.get("marker_read_handle_observations")
        if not isinstance(observations, list):
            observations = []
            worker["marker_read_handle_observations"] = observations
        observations.append(
            {
                "closed": True,
                "close_error": worker.get("marker_read_handle_close_error"),
                "residual": worker.get("marker_read_handle_residual") is True,
            }
        )
        worker["marker_read_handle"] = None
        worker["marker_read_handle_owned"] = False
        worker["marker_read_handle_closed"] = False
        worker["marker_read_handle_close_error"] = None
        worker["marker_read_handle_residual"] = False
        read_handle = None
    if read_handle is None:
        try:
            read_handle = marker.open("rb")
        except Exception as exc:
            return fail(exc)
        # Register the actual opened rb handle before any fallible fstat/seek/read.
        worker["marker_read_handle"] = read_handle
        worker["marker_read_handle_owned"] = True
        worker["marker_read_handle_registered"] = True
        worker["marker_read_handle_closed"] = False
        worker["marker_read_handle_close_error"] = None
        worker["marker_read_handle_residual"] = False
    elif (
        worker.get("marker_read_handle_owned") is not True
        or worker.get("marker_read_handle_registered") is not True
    ):
        return fail(RuntimeError("marker read handle was present without registered ownership"))

    try:
        metadata_before = os.lstat(marker)
        path_before = metadata_record(metadata_before)
        if path_before["reparse"] or not path_before["regular"]:
            return fail(RuntimeError("marker path is not a plain regular file"))
        if path_before["size"] > max_marker_bytes:
            return fail(RuntimeError("marker content exceeded bounded observation size"))
        if path_before["identity"] != list(expected_identity):
            return fail(RuntimeError("marker identity changed while binding content"))

        metadata_before_open = os.fstat(read_handle.fileno())
        handle_before = metadata_record(metadata_before_open)
        if handle_before["reparse"] or not handle_before["regular"]:
            return fail(RuntimeError("marker read handle is not a plain regular file"))
        if handle_before["size"] > max_marker_bytes:
            return fail(RuntimeError("marker read handle content exceeded bounded observation size"))
        if not records_match(path_before, handle_before):
            return fail(RuntimeError("marker read handle identity/metadata does not match path"))

        try:
            read_handle.seek(0)
        except Exception as exc:
            return fail(exc)
        raw: Optional[bytes] = None
        read_error: Optional[BaseException] = None
        try:
            raw = read_handle.read(max_marker_bytes + 1)
        except Exception as exc:
            read_error = exc
        after_open: Optional[Dict[str, Any]] = None
        after_open_error: Optional[BaseException] = None
        try:
            after_open = metadata_record(os.fstat(read_handle.fileno()))
        except Exception as exc:
            after_open_error = exc
        observation_errors: List[str] = []
        if read_error is not None:
            message = _worker_error_text(read_error)
            binding_errors.append(message)
            observation_errors.append(message)
        if after_open_error is not None:
            message = _worker_error_text(after_open_error)
            binding_errors.append(message)
            observation_errors.append(message)
        if read_error is not None or after_open_error is not None:
            worker["marker_binding_error"] = "; ".join(observation_errors)
            return False
        assert raw is not None and after_open is not None
        if not isinstance(raw, bytes):
            return fail(TypeError("marker rb read did not return bytes"))
        if after_open["reparse"] or not after_open["regular"]:
            return fail(RuntimeError("marker read handle changed to a non-regular file"))
        if after_open["size"] > max_marker_bytes:
            return fail(RuntimeError("marker read handle content exceeded bounded observation size"))

        metadata_after = os.lstat(marker)
        path_after = metadata_record(metadata_after)
        if path_after["reparse"] or not path_after["regular"]:
            return fail(RuntimeError("marker path changed to a non-regular file"))
        if path_after["size"] > max_marker_bytes:
            return fail(RuntimeError("marker content exceeded bounded observation size"))
        if (
            path_before["identity"] != path_after["identity"]
            or handle_before["identity"] != after_open["identity"]
            or not records_match(path_before, path_after)
            or not records_match(handle_before, after_open)
            or not records_match(path_after, after_open)
            or path_after["identity"] != list(expected_identity)
            or len(raw) != path_after["size"]
            or len(raw) > max_marker_bytes
        ):
            return fail(RuntimeError("marker identity or metadata changed while reading content"))
    except Exception as exc:
        return fail(exc)

    assert raw is not None and path_after is not None and after_open is not None
    content_sha256 = _sha256_bytes(raw)
    if worker.get("marker_binding_identity") is None:
        worker["marker_binding_identity"] = list(path_after["identity"])
        worker["marker_binding_mtime_ns"] = path_after["mtime_ns"]
        worker["marker_binding_size"] = path_after["size"]
        worker["marker_content_sha256"] = content_sha256
    else:
        if (
            worker.get("marker_binding_identity") != list(path_after["identity"])
            or worker.get("marker_binding_mtime_ns") != path_after["mtime_ns"]
            or worker.get("marker_binding_size") != path_after["size"]
            or worker.get("marker_content_sha256") != content_sha256
        ):
            worker["marker_binding_error"] = "marker binding changed since first observation"
            return False

    if worker.get("marker_write_complete") is not True:
        worker["marker_binding_content_checked"] = True
    if worker.get("marker_write_complete") is True and content_sha256 != worker.get(
        "marker_expected_content_sha256"
    ):
        worker["marker_binding_error"] = "complete marker content differs from its encoded payload"
        return False

    worker["marker_binding_observed"] = True
    worker["marker_binding_error"] = None
    worker["marker_binding"] = {
        "identity": list(path_after["identity"]),
        "content_sha256": content_sha256,
        "size": len(raw),
        "read_handle_identity": list(handle_before["identity"]),
        "read_handle_mtime_ns": after_open["mtime_ns"],
        "binding_mtime_ns": worker.get("marker_binding_mtime_ns"),
        "path_before": path_before,
        "path_after": path_after,
        "handle_before": handle_before,
        "handle_after": after_open,
    }
    return True


def _write_marker_create_only(
    marker: pathlib.Path, payload: Mapping[str, Any], worker: Dict[str, Any]
) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    encoded_text = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    worker["marker_expected_content_sha256"] = _sha256_bytes(
        encoded_text.encode("utf-8")
    )
    worker["marker_write_complete"] = False
    handle: Any = None
    try:
        handle = marker.open("x", encoding="utf-8", newline="\n")
        # Keep the exclusive text writer separate from every later rb reader.
        worker["marker_write_handle"] = handle
        worker["marker_handle"] = handle
        worker["marker_write_handle_owned"] = True
        worker["marker_write_handle_registered"] = True
        worker["marker_write_handle_closed"] = False
        worker["marker_write_handle_close_error"] = None
        worker["marker_write_handle_residual"] = False
        worker["marker_handle_owned"] = True
        worker["marker_handle_closed"] = False
        worker["marker_creation_owned"] = True
        try:
            metadata = os.fstat(handle.fileno())
        except Exception as exc:
            worker["marker_identity_error"] = _worker_error_text(exc)
            raise
        worker["marker_identity"] = _identity(metadata)
        worker["marker_identity_registered"] = True
        written = handle.write(encoded_text)
        if written != len(encoded_text):
            raise OSError("marker write was partial")
        handle.flush()
        worker["marker_write_complete"] = True
    except Exception:
        if handle is not None:
            try:
                handle.close()
                worker["marker_write_handle_closed"] = True
                worker["marker_handle_closed"] = True
                worker["marker_write_handle_owned"] = False
                worker["marker_handle_owned"] = False
            except Exception as close_exc:
                error = _worker_error_text(close_exc)
                worker["marker_write_handle_close_error"] = error
                worker["marker_handle_close_error"] = error
                worker["marker_write_handle_residual"] = True
                worker["marker_handle_error"] = error
            if worker.get("marker_write_handle_closed"):
                _capture_marker_binding(worker, marker)
                if worker.get("marker_binding_error"):
                    worker["marker_handle_error"] = worker.get("marker_binding_error")
        raise
    try:
        handle.close()
        worker["marker_write_handle_closed"] = True
        worker["marker_handle_closed"] = True
        worker["marker_write_handle_owned"] = False
        worker["marker_handle_owned"] = False
    except Exception as close_exc:
        error = _worker_error_text(close_exc)
        worker["marker_write_handle_close_error"] = error
        worker["marker_handle_close_error"] = error
        worker["marker_write_handle_residual"] = True
        worker["marker_handle_error"] = error
        raise
    if not _capture_marker_binding(worker, marker):
        raise RuntimeError(
            worker.get("marker_binding_error") or "marker content binding failed"
        )


def _reader_thread_alive(
    reader: Any,
    *,
    errors: Optional[List[str]] = None,
    thread_lookup_errors: Optional[List[str]] = None,
    label: Optional[str] = None,
) -> Optional[bool]:
    try:
        thread = getattr(reader, "thread", None)
    except Exception as exc:
        target = thread_lookup_errors if thread_lookup_errors is not None else errors
        if target is not None:
            target.append(f"{label or 'reader'} thread lookup failed: {_worker_error_text(exc)}")
        return None
    if thread is None:
        return None
    try:
        return bool(thread.is_alive())
    except Exception as exc:
        if errors is not None:
            errors.append(f"{label or 'reader'} is_alive failed: {_worker_error_text(exc)}")
        return None


def _reader_state(
    reader: Any,
    field: str,
    missing: Any = None,
    *,
    errors: Optional[List[str]] = None,
    label: Optional[str] = None,
) -> Any:
    if reader is None:
        return missing
    try:
        return getattr(reader, field, missing)
    except Exception as exc:
        if errors is not None:
            errors.append(f"{label or 'reader'} .{field} lookup failed: {_worker_error_text(exc)}")
        return missing


def _reader_thread_started(
    reader: Any,
    *,
    errors: Optional[List[str]] = None,
    label: Optional[str] = None,
) -> Optional[bool]:
    missing = object()
    try:
        start_state = getattr(reader, "thread_start_state", missing)
    except Exception as exc:
        if errors is not None:
            errors.append(f"{label or 'reader'} thread_start_state lookup failed: {_worker_error_text(exc)}")
        return None
    if start_state == "unknown":
        return None
    if start_state == "started":
        return True
    if start_state == "not_started":
        return False
    try:
        value = getattr(reader, "thread_started", None)
    except Exception as exc:
        if errors is not None:
            errors.append(f"{label or 'reader'} thread_started lookup failed: {_worker_error_text(exc)}")
        return None
    if isinstance(value, bool):
        return value
    try:
        thread = getattr(reader, "thread", None)
    except Exception as exc:
        if errors is not None:
            errors.append(f"{label or 'reader'} thread lookup failed: {_worker_error_text(exc)}")
        return None
    if thread is None:
        return False
    for name in ("started", "start_returned", "start_called"):
        try:
            state = getattr(thread, name, None)
        except Exception as exc:
            if errors is not None:
                errors.append(
                    f"{label or 'reader'} thread.{name} lookup failed: {_worker_error_text(exc)}"
                )
            return None
        if isinstance(state, bool) and state:
            return True
    try:
        ident = getattr(thread, "ident", None)
    except Exception as exc:
        if errors is not None:
            errors.append(f"{label or 'reader'} thread.ident lookup failed: {_worker_error_text(exc)}")
        return None
    return ident is not None


class _PipeReader:
    def __init__(self, stream: Any, label: str):
        self._stream = stream
        self.label = label
        self.items: queue.Queue[str] = queue.Queue(maxsize=128)
        self.overflow = False
        self.error: Optional[str] = None
        self.eof = False
        self.lines_read = 0
        self.bytes_read = 0
        self.dropped_lines = 0
        self.start_attempted = False
        self.start_returned = False
        self.thread_started = False
        self.thread_start_state: Optional[str] = "not_attempted"
        self.thread_start_error: Optional[str] = None
        self.thread_start_observation_errors: List[str] = []
        self.thread = threading.Thread(target=self._read, name=f"oni-a09a9-{label}", daemon=True)

    def start(self) -> None:
        self.start_attempted = True
        try:
            self.thread.start()
        except Exception as exc:
            self.thread_start_error = _worker_error_text(exc)
            self.start_returned = False
            observation_errors: List[str] = []
            alive: Optional[bool] = None
            ident: Any = None
            try:
                alive = bool(self.thread.is_alive())
            except Exception as observation_exc:
                observation_errors.append(
                    f"thread.is_alive failed: {_worker_error_text(observation_exc)}"
                )
            try:
                ident = getattr(self.thread, "ident", None)
            except Exception as observation_exc:
                observation_errors.append(
                    f"thread.ident lookup failed: {_worker_error_text(observation_exc)}"
                )
            self.thread_start_observation_errors = observation_errors
            if observation_errors:
                self.thread_started = None
                self.thread_start_state = "unknown"
            else:
                self.thread_started = bool(alive) or ident is not None
                self.thread_start_state = (
                    "started" if self.thread_started else "not_started"
                )
            raise
        self.start_returned = True
        self.thread_started = True
        self.thread_start_state = "started"

    def _read(self) -> None:
        try:
            while True:
                line = self._stream.readline(MAX_CHILD_LINE + 1)
                if not line:
                    return
                self.lines_read += 1
                self.bytes_read += len(line.encode("utf-8", errors="replace"))
                if len(line) > MAX_CHILD_LINE:
                    self.overflow = True
                    line = line[:MAX_CHILD_LINE]
                try:
                    self.items.put_nowait(line.rstrip("\r\n"))
                except queue.Full:
                    self.overflow = True
                    self.dropped_lines += 1
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.overflow = True
        finally:
            self.eof = True

    def drain(self) -> List[str]:
        result: List[str] = []
        while True:
            try:
                result.append(self.items.get_nowait())
            except queue.Empty:
                return result


def _pump_worker(worker: Dict[str, Any]) -> None:
    stdout_reader: Optional[_PipeReader] = worker.get("stdout_reader")
    stderr_reader: Optional[_PipeReader] = worker.get("stderr_reader")
    if stdout_reader is not None:
        for line in stdout_reader.drain():
            encoded_size = len(line.encode("utf-8", errors="replace")) + 1
            worker["observed_output_bytes"] += encoded_size
            if worker["observed_output_bytes"] > MAX_CHILD_OUTPUT_BYTES:
                worker["pipe_overflow"] = True
                worker["overflow_reason"] = "combined child stdout/stderr output exceeded bound"
                continue
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                value = {"stage": "protocol_error", "message": line[:512]}
            if not isinstance(value, dict):
                value = {"stage": "protocol_error", "message": "non-object stdout"}
            if len(worker["events"]) >= MAX_CHILD_EVENTS:
                worker["pipe_overflow"] = True
                worker["overflow_reason"] = "child event count exceeded bound"
                continue
            worker["events"].append(value)
            stage = value.get("stage")
            if isinstance(stage, str):
                worker["handshake_stage"] = stage
    if stderr_reader is not None:
        for line in stderr_reader.drain():
            encoded_size = len(line.encode("utf-8", errors="replace")) + 1
            worker["observed_output_bytes"] += encoded_size
            worker["stderr_bytes"] += encoded_size
            if worker["observed_output_bytes"] > MAX_CHILD_OUTPUT_BYTES:
                worker["pipe_overflow"] = True
                worker["overflow_reason"] = "combined child stdout/stderr output exceeded bound"
                continue
            if worker["stderr_bytes"] > MAX_CHILD_STDERR_BYTES:
                worker["pipe_overflow"] = True
                worker["overflow_reason"] = "child stderr output exceeded bound"
                continue
            if len(worker["stderr_lines"]) >= MAX_CHILD_EVENTS:
                worker["pipe_overflow"] = True
                worker["overflow_reason"] = "child stderr line count exceeded bound"
                continue
            worker["stderr_lines"].append(line[:MAX_CHILD_LINE])
    if (stdout_reader is not None and stdout_reader.overflow) or (
        stderr_reader is not None and stderr_reader.overflow
    ):
        worker["pipe_overflow"] = True
        if worker.get("overflow_reason") is None:
            worker["overflow_reason"] = "child pipe reader overflow"


def _worker_protocol(worker: Mapping[str, Any]) -> Dict[str, Any]:
    events = list(worker.get("events") or [])
    stages = [item.get("stage") for item in events if isinstance(item, Mapping)]
    errors: List[str] = []
    if stages != ["ready", "calling", "result"]:
        errors.append(f"unexpected worker stage sequence: {stages!r}")
    if any(stage in {"error", "protocol_error", "worker_error"} for stage in stages):
        errors.append("worker emitted an error or protocol-error event")
    ready = events[0] if len(events) > 0 and isinstance(events[0], Mapping) else {}
    calling = events[1] if len(events) > 1 and isinstance(events[1], Mapping) else {}
    result = events[2] if len(events) > 2 and isinstance(events[2], Mapping) else {}
    if calling.get("operation") != worker.get("operation"):
        errors.append("calling operation does not match the requested operation")
    if result.get("command_id") != worker.get("command_id"):
        errors.append("result command id does not match the requested command")
    if not isinstance(result.get("result"), list):
        errors.append("result payload is not a list")
    child_attestation = ready.get("module_attestation")
    if not isinstance(child_attestation, Mapping):
        errors.append("ready event did not attest child module paths")
    else:
        for name, (_module, relative_path) in MODULE_PATHS.items():
            expected_path = str((PROJECT_ROOT / relative_path).resolve())
            entry = child_attestation.get(name)
            if not isinstance(entry, Mapping):
                errors.append(f"child module attestation is missing: {name}")
                continue
            if entry.get("expected_path") != expected_path or entry.get("actual_path") != expected_path:
                errors.append(f"child module path attestation drifted: {name}")
    return {
        "valid": not errors and not bool(worker.get("pipe_overflow")),
        "stages": stages,
        "errors": errors,
        "event_count": len(events),
    }


def _wait_for_worker_stage(worker: Dict[str, Any], stage: str, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _pump_worker(worker)
        if any(item.get("stage") == stage for item in worker["events"]):
            return True
        if worker["process"].poll() is not None:
            return False
        time.sleep(0.01)
    _pump_worker(worker)
    return any(item.get("stage") == stage for item in worker["events"])


def _worker_marker(
    root: pathlib.Path,
    operation: str,
    command_id: str,
    ledger: Optional[Dict[str, Any]] = None,
) -> Tuple[pathlib.Path, str]:
    worker = _ensure_worker_ledger(ledger, operation, command_id)
    token = secrets.token_hex(16)
    marker = root / f".oni-a09a9-worker-{token}.json"
    payload = {
        "schema": "oni.a09a9.custody-worker-marker.v1",
        "root": str(root.resolve()),
        "operation": operation,
        "command_id": command_id,
        "token": token,
    }
    worker["phase"] = "marker_create"
    worker["marker"] = marker
    worker["marker_path"] = marker
    worker["marker_token"] = token
    try:
        _write_marker_create_only(marker, payload, worker)
    except Exception as exc:
        worker["phase"] = "marker_create_failed"
        _remember_worker_error(worker, exc)
        raise
    worker["phase"] = "marker_bound"
    return marker, token


def _spawn_worker(
    root: pathlib.Path,
    operation: str,
    command_id: str,
    ledger: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    worker = _ensure_worker_ledger(ledger, operation, command_id)
    try:
        marker, token = _worker_marker(root, operation, command_id, worker)
    except Exception as exc:
        worker["phase"] = "marker_failed"
        _remember_worker_error(worker, exc)
        raise
    command = [
        str(pathlib.Path(sys.executable).resolve()),
        "-B",
        str(SCRIPT_PATH),
        "--worker",
        operation,
        "--root",
        str(root.resolve()),
        "--marker",
        str(marker.resolve()),
        "--token",
        token,
    ]
    worker["command"] = command
    worker["phase"] = "executable_validation"
    try:
        if not pathlib.Path(command[0]).is_file() or pathlib.Path(command[2]).resolve() != SCRIPT_PATH:
            raise RuntimeError("worker executable or script path validation failed")
    except Exception as exc:
        worker["phase"] = "executable_validation_failed"
        _remember_worker_error(worker, exc)
        raise
    worker["phase"] = "popen"
    try:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            shell=False,
        )
    except Exception as exc:
        worker["phase"] = "popen_failed"
        _remember_worker_error(worker, exc)
        raise
    worker["process"] = process
    worker["process_owned"] = True
    worker["phase"] = "process_registered"
    for stream_name in ("stdin", "stdout", "stderr"):
        try:
            worker[stream_name] = getattr(process, stream_name)
        except Exception as exc:
            worker[f"{stream_name}_access_error"] = _worker_error_text(exc)
            worker["phase"] = f"{stream_name}_registration_failed"
            _remember_worker_error(worker, exc)
            raise
    try:
        worker["pid"] = int(getattr(process, "pid"))
    except Exception as exc:
        worker["phase"] = "pid_registration_failed"
        _remember_worker_error(worker, exc)
        raise
    if worker.get("stdin") is None:
        exc = RuntimeError("worker stdin is unavailable")
        worker["phase"] = "stdin_validation_failed"
        _remember_worker_error(worker, exc)
        raise exc
    for stream_name in ("stdout", "stderr"):
        if worker.get(stream_name) is None:
            exc = RuntimeError(f"worker {stream_name} pipe is unavailable")
            worker["phase"] = f"{stream_name}_validation_failed"
            _remember_worker_error(worker, exc)
            raise exc
    for label in ("stdout", "stderr"):
        reader_key = f"{label}_reader"
        worker["phase"] = f"{label}_reader_construct"
        try:
            reader = _PipeReader(worker[label], label)
            worker[reader_key] = reader
            worker["readers"][label] = reader
            worker["reader_registered"][label] = True
        except Exception as exc:
            worker["phase"] = f"{label}_reader_construct_failed"
            _remember_worker_error(worker, exc)
            raise
        worker["reader_start_attempted"][label] = True
        worker["phase"] = f"{label}_reader_start"
        try:
            reader.start()
        except Exception as exc:
            start_errors = worker["reader_start_errors"].setdefault(label, [])
            if not isinstance(start_errors, list):
                start_errors = []
                worker["reader_start_errors"][label] = start_errors
            start_errors.append(_worker_error_text(exc))
            observation_errors = _reader_state(
                reader,
                "thread_start_observation_errors",
                [],
                errors=worker["reader_state_lookup_errors"],
                label=f"{label} reader",
            )
            if isinstance(observation_errors, list):
                start_errors.extend(str(item) for item in observation_errors)
            start_state = _reader_state(
                reader,
                "thread_start_state",
                None,
                errors=worker["reader_state_lookup_errors"],
                label=f"{label} reader",
            )
            worker["reader_start_unknown"][label] = start_state == "unknown"
            worker["reader_started"][label] = _reader_thread_started(
                reader,
                errors=worker["reader_thread_lookup_errors"],
                label=f"{label} reader",
            )
            start_returned = _reader_state(
                reader,
                "start_returned",
                None,
                errors=worker["reader_state_lookup_errors"],
                label=f"{label} reader",
            )
            worker["reader_start_returned"][label] = (
                start_returned if isinstance(start_returned, bool) else None
            )
            worker["phase"] = f"{label}_reader_start_failed"
            _remember_worker_error(worker, exc)
            raise
        start_returned = _reader_state(
            reader,
            "start_returned",
            True,
            errors=worker["reader_state_lookup_errors"],
            label=f"{label} reader",
        )
        worker["reader_start_returned"][label] = (
            start_returned if isinstance(start_returned, bool) else None
        )
        worker["reader_started"][label] = _reader_thread_started(
            reader,
            errors=worker["reader_thread_lookup_errors"],
            label=f"{label} reader",
        )
        worker["reader_start_unknown"][label] = (
            worker["reader_started"][label] is None
        )
    worker["phase"] = "spawned"
    worker["handshake_stage"] = "spawned"
    return worker


def _send_worker_go(worker: Dict[str, Any]) -> None:
    if worker.get("go_attempted"):
        raise RuntimeError("worker go was already attempted")
    worker["go_attempted"] = True
    worker["phase"] = "go_attempted"
    process: Optional[subprocess.Popen[str]] = worker.get("process")
    stdin = worker.get("stdin")
    if stdin is None and process is not None:
        try:
            stdin = process.stdin
            worker["stdin"] = stdin
        except Exception as exc:
            worker["go_uncertain"] = True
            _remember_worker_error(worker, exc)
            raise
    if stdin is None:
        exc = RuntimeError("worker stdin is unavailable")
        worker["go_uncertain"] = True
        _remember_worker_error(worker, exc)
        raise exc
    try:
        worker["go_write_attempted"] = True
        stdin.write("go\n")
        worker["go_write_succeeded"] = True
        worker["go_flush_attempted"] = True
        stdin.flush()
        worker["go_flush_succeeded"] = True
    except Exception as exc:
        worker["go_uncertain"] = True
        worker["go_error"] = _worker_error_text(exc)
        _remember_worker_error(worker, exc)
        raise
    worker["go_sent"] = True
    try:
        stdin.close()
        worker["stdin_closed"] = True
    except Exception as exc:
        worker["go_close_error"] = _worker_error_text(exc)
        _remember_worker_error(worker, exc)
        raise
    worker["phase"] = "go_sent"


def _sample_while_locked(
    worker: Dict[str, Any],
    root: pathlib.Path,
    session_path: pathlib.Path,
    baseline: Mapping[str, Any],
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "baseline": copy.deepcopy(dict(baseline)),
        "baseline_digest": baseline.get("digest"),
        "baseline_captured_before_go": True,
        "sample_count": 0,
        "sample_duration_seconds": 0.0,
        "changed_sample_count": 0,
        "changed_paths": [],
        "sample_errors": [],
        "samples": [],
        "completed_before_release": False,
        "early_exit": None,
        "calling_seen": False,
    }
    sample_start = time.monotonic()
    sample_end = sample_start + LOCK_SAMPLE_SECONDS
    while time.monotonic() < sample_end:
        _pump_worker(worker)
        result["calling_seen"] = result["calling_seen"] or any(
            item.get("stage") == "calling" for item in worker["events"]
        )
        if worker["process"].poll() is not None:
            result["completed_before_release"] = True
            if result["early_exit"] is None:
                result["early_exit"] = {
                    "observed": True,
                    "returncode": worker["process"].poll(),
                    "observed_at": now_iso(),
                }
        sample_record: Dict[str, Any] = {
            "observed_at": now_iso(),
            "digest": None,
            "changed": False,
            "changed_paths": [],
            "error": None,
            "returncode": worker["process"].poll(),
        }
        try:
            current = _protected_snapshot(root, session_path)
            result["sample_count"] += 1
            sample_record["digest"] = current["digest"]
            if current != baseline:
                sample_record["changed"] = True
                result["changed_sample_count"] += 1
                paths = _changed_records(baseline, current)
                sample_record["changed_paths"] = paths
                result["changed_paths"] = sorted(
                    set(result["changed_paths"]) | set(paths)
                )[:64]
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            sample_record["error"] = error
            result["sample_errors"].append(error)
        result["samples"].append(sample_record)
        time.sleep(LOCK_SAMPLE_INTERVAL_SECONDS)
    _pump_worker(worker)
    result["calling_seen"] = result["calling_seen"] or any(
        item.get("stage") == "calling" for item in worker["events"]
    )
    result["completed_before_release"] = result["completed_before_release"] or (
        worker["process"].poll() is not None
    )
    if worker["process"].poll() is not None and result["early_exit"] is None:
        result["early_exit"] = {
            "observed": True,
            "returncode": worker["process"].poll(),
            "observed_at": now_iso(),
        }
    result["sample_duration_seconds"] = time.monotonic() - sample_start
    try:
        final_sample = _protected_snapshot(root, session_path)
        final_changed = final_sample != baseline
        final_paths = _changed_records(baseline, final_sample)
        if final_changed:
            result["changed_sample_count"] += 1
            result["changed_paths"] = sorted(
                set(result["changed_paths"]) | set(final_paths)
            )[:64]
        result["sample_digest_after"] = final_sample.get("digest")
        result["samples"].append(
            {
                "observed_at": now_iso(),
                "digest": final_sample.get("digest"),
                "changed": final_changed,
                "changed_paths": final_paths,
                "error": None,
                "returncode": worker["process"].poll(),
                "final_sample": True,
            }
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        result["sample_errors"].append(error)
        result["samples"].append(
            {
                "observed_at": now_iso(),
                "digest": None,
                "changed": False,
                "changed_paths": [],
                "error": error,
                "returncode": worker["process"].poll(),
                "final_sample": True,
            }
        )
        result["sample_digest_after"] = None
    return result


def _finish_worker(worker: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if worker is None:
        return {
            "returncode": None,
            "pid": None,
            "forced_kill": False,
            "residual_process": False,
            "cleanup_error": None,
            "cleanup_errors": ["worker ledger was missing"],
            "primary_error": "worker ledger was missing",
            "protocol_valid": False,
        }
    lifecycle_provenance = _remember_reader_lifecycle_provenance(worker)
    explicit_process_owned = worker.get("process_owned") is True
    explicit_reader_lifecycle = {
        label: _explicit_reader_lifecycle(worker, label, lifecycle_provenance)
        for label in ("stdout", "stderr")
    }
    worker = _ensure_worker_ledger(
        worker,
        str(worker.get("operation") or "unknown"),
        str(worker.get("command_id") or "unknown"),
    )
    errors: List[str] = []

    def add_error(message: str) -> None:
        errors.append(str(message))

    def record_exception(prefix: str, exc: BaseException) -> None:
        add_error(f"{prefix}: {_worker_error_text(exc)}")

    def observe_marker_presence(path: Optional[pathlib.Path]) -> Optional[bool]:
        worker["marker_presence_checked"] = True
        worker["marker_present_after"] = None
        if path is None:
            message = "marker path is missing"
            worker["marker_presence_state"] = "unknown"
            worker["marker_presence_error"] = message
            add_error(f"owned marker final presence check failed: {message}")
            return None
        try:
            os.lstat(path)
        except FileNotFoundError:
            worker["marker_presence_state"] = "absent"
            worker["marker_presence_error"] = None
            worker["marker_present_after"] = False
            return False
        except Exception as exc:
            message = _worker_error_text(exc)
            worker["marker_presence_state"] = "unknown"
            worker["marker_presence_error"] = message
            record_exception("owned marker final presence check failed", exc)
            return None
        worker["marker_presence_state"] = "present"
        worker["marker_presence_error"] = None
        worker["marker_present_after"] = True
        return True

    def record_marker_unlink_effect(effect: str, error: Optional[str] = None) -> None:
        history = worker.get("marker_unlink_effect_history")
        if not isinstance(history, list):
            history = []
            worker["marker_unlink_effect_history"] = history
        history.append(effect)
        worker["marker_unlink_effect"] = effect
        worker["marker_unlink_error"] = error

    def error_list(key: str) -> List[str]:
        value = worker.get(key)
        if not isinstance(value, list):
            value = []
            worker[key] = value
        return value

    reader_thread_lookup_errors = error_list("reader_thread_lookup_errors")
    reader_state_lookup_errors = error_list("reader_state_lookup_errors")
    reader_eof_errors = error_list("reader_eof_errors")
    reader_aliveness_errors = error_list("reader_aliveness_errors")
    reader_lookup_error_cursors = worker.get("reader_lookup_error_cursors")
    if not isinstance(reader_lookup_error_cursors, dict):
        reader_lookup_error_cursors = {}
        worker["reader_lookup_error_cursors"] = reader_lookup_error_cursors
    for cursor_name in ("thread", "state", "eof", "aliveness"):
        cursor = reader_lookup_error_cursors.get(cursor_name)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            reader_lookup_error_cursors[cursor_name] = 0
    cleanup_history = list(error_list("cleanup_errors"))
    reader_start_errors = worker.get("reader_start_errors")
    if not isinstance(reader_start_errors, dict):
        reader_start_errors = {"stdout": [], "stderr": []}
        worker["reader_start_errors"] = reader_start_errors
    for label in ("stdout", "stderr"):
        if not isinstance(reader_start_errors.get(label), list):
            reader_start_errors[label] = []
    reader_start_unknown = worker.get("reader_start_unknown")
    if not isinstance(reader_start_unknown, dict):
        reader_start_unknown = {"stdout": False, "stderr": False}
        worker["reader_start_unknown"] = reader_start_unknown

    def safe_pump() -> None:
        try:
            _pump_worker(worker)
        except Exception as exc:
            record_exception("worker pump failed", exc)

    process = worker.get("process")
    process_owned = process is not None and worker.get("process_owned") is not False
    forced_kill = False
    returncode: Optional[int] = None
    process_poll_error: Optional[str] = None
    process_wait_iterations = 0
    reader_wait_iterations = 0

    for label in ("stdout", "stderr"):
        for message in reader_start_errors[label]:
            formatted = f"{label} reader start error: {message}"
            if formatted not in cleanup_history:
                add_error(formatted)

    stdin = worker.get("stdin")
    if stdin is None and process_owned:
        try:
            stdin = getattr(process, "stdin", None)
            worker["stdin"] = stdin
        except Exception as exc:
            record_exception("stdin lookup failed", exc)
    if stdin is not None and not worker.get("stdin_closed"):
        worker["stdin_close_attempted"] = True
        try:
            stdin.close()
            worker["stdin_closed"] = True
        except Exception as exc:
            record_exception("stdin close failed", exc)

    def poll_process() -> Optional[int]:
        nonlocal process_poll_error
        if not process_owned:
            return None
        try:
            value = process.poll()
            process_poll_error = None
            return value
        except Exception as exc:
            process_poll_error = _worker_error_text(exc)
            record_exception("process poll failed", exc)
            return None

    if process_owned:
        process_deadline: Optional[float]
        try:
            process_deadline = time.monotonic() + CHILD_POST_RELEASE_TIMEOUT_SECONDS
        except Exception as exc:
            process_deadline = None
            record_exception("process deadline clock failed", exc)
        current = poll_process()
        while (
            current is None
            and process_deadline is not None
            and process_poll_error is None
        ):
            try:
                if time.monotonic() >= process_deadline:
                    break
            except Exception as exc:
                record_exception("process deadline clock failed", exc)
                break
            process_wait_iterations += 1
            safe_pump()
            try:
                time.sleep(0.02)
            except Exception as exc:
                record_exception("process wait sleep failed", exc)
                break
            current = poll_process()
        current = poll_process()
        returncode = current
    if process_owned and returncode is None and process_poll_error is None:
        forced_kill = True
        worker["phase"] = "forced_kill"
        try:
            process.kill()
            worker["kill_attempted"] = True
        except Exception as exc:
            record_exception("forced kill failed", exc)
        try:
            process.wait(timeout=2.0)
        except Exception as exc:
            record_exception("forced kill wait failed", exc)
        returncode = poll_process()

    readers = {
        "stdout": worker.get("stdout_reader"),
        "stderr": worker.get("stderr_reader"),
    }
    try:
        reader_deadline = time.monotonic() + 2.0
    except Exception as exc:
        reader_deadline = None
        record_exception("reader deadline clock failed", exc)
    while reader_deadline is not None:
        try:
            if time.monotonic() >= reader_deadline:
                break
        except Exception as exc:
            record_exception("reader deadline clock failed", exc)
            break
        readers_done = True
        for label, reader in readers.items():
            if reader is None:
                if process_owned:
                    readers_done = False
                continue
            eof = _reader_state(
                reader,
                "eof",
                None,
                errors=reader_eof_errors,
                label=f"{label} reader",
            )
            alive = _reader_thread_alive(
                reader,
                errors=reader_aliveness_errors,
                thread_lookup_errors=reader_thread_lookup_errors,
                label=f"{label} reader",
            )
            if eof is not True or alive is not False:
                readers_done = False
        if readers_done:
            break
        reader_wait_iterations += 1
        safe_pump()
        try:
            time.sleep(0.01)
        except Exception as exc:
            record_exception("reader wait sleep failed", exc)
            break
    for label, reader in readers.items():
        if reader is None and process_owned:
            add_error(f"{label} reader was not registered")
    safe_pump()

    reader_status: Dict[str, Dict[str, Any]] = {}
    for label, reader in readers.items():
        if reader is None:
            lifecycle = explicit_reader_lifecycle.get(label)
            no_reader_start_proven = (
                explicit_process_owned
                and lifecycle is not None
                and all(value is False for value in lifecycle.values())
            )
            reader_status[label] = {
                "registered": False,
                "started": None,
                "alive": None,
                "eof": None,
                "lifecycle_evidence": lifecycle,
                "start_state_unknown": lifecycle is None,
                "safe_to_close": no_reader_start_proven,
            }
            continue
        error_counts_before = (
            len(reader_thread_lookup_errors),
            len(reader_state_lookup_errors),
            len(reader_eof_errors),
            len(reader_aliveness_errors),
        )
        started = _reader_thread_started(
            reader,
            errors=reader_thread_lookup_errors,
            label=f"{label} reader",
        )
        reader_start_unknown[label] = started is None
        try:
            thread = getattr(reader, "thread", None)
            thread_state_known = True
        except Exception as exc:
            thread = None
            thread_state_known = False
            reader_thread_lookup_errors.append(
                f"{label} reader thread lookup failed: {_worker_error_text(exc)}"
            )
        alive_before = _reader_thread_alive(
            reader,
            errors=reader_aliveness_errors,
            label=f"{label} reader",
            thread_lookup_errors=reader_thread_lookup_errors,
        )
        if started and thread is not None:
            try:
                thread.join(timeout=0.2)
            except Exception as exc:
                record_exception(f"{label} reader join failed", exc)
        elif started is None:
            add_error(f"{label} reader start state was unknown")
        elif started and thread is None:
            add_error(f"{label} reader was marked started without a thread")
        elif alive_before is True:
            add_error(f"{label} reader is alive without a recorded start")
        alive_after = _reader_thread_alive(
            reader,
            errors=reader_aliveness_errors,
            label=f"{label} reader",
            thread_lookup_errors=reader_thread_lookup_errors,
        )
        eof = _reader_state(
            reader,
            "eof",
            None,
            errors=reader_eof_errors,
            label=f"{label} reader",
        )
        error_counts_after = (
            len(reader_thread_lookup_errors),
            len(reader_state_lookup_errors),
            len(reader_eof_errors),
            len(reader_aliveness_errors),
        )
        uncertain = error_counts_after != error_counts_before
        if alive_after is not False:
            add_error(f"{label} reader remained alive or its state was unknown after bounded drain")
        if eof is not True:
            add_error(f"{label} reader did not reach EOF")
        reader_status[label] = {
            "registered": True,
            "started": started,
            "alive": alive_after,
            "eof": eof,
            "thread_state_known": thread_state_known,
            "start_state_unknown": started is None,
            "safe_to_close": (
                alive_after is False
                and started is not None
                and not uncertain
            ),
        }

    for label, reader in readers.items():
        stream = worker.get(label)
        status = reader_status.get(label, {})
        alive = status.get("alive")
        if reader is None:
            if stream is not None:
                if status.get("safe_to_close") is not True:
                    add_error(
                        f"{label} pipe remained open because reader ownership/start state was not proven"
                    )
                else:
                    try:
                        stream.close()
                        worker[f"{label}_closed"] = True
                    except Exception as exc:
                        record_exception(f"{label} pipe close failed", exc)
            continue
        if alive is not False or status.get("safe_to_close") is not True:
            add_error(f"{label} pipe remained open because its reader state is not proven inactive")
            continue
        if stream is None:
            continue
        try:
            stream.close()
            worker[f"{label}_closed"] = True
        except Exception as exc:
            record_exception(f"{label} pipe close failed", exc)

    marker = worker.get("marker") or worker.get("marker_path")
    marker_unlink_effect = worker.get("marker_unlink_effect")
    if marker_unlink_effect not in ("unattempted", "unknown", "removed"):
        marker_unlink_effect = "unknown"
        worker["marker_unlink_effect"] = marker_unlink_effect
        worker["marker_unlink_error"] = "invalid marker unlink effect state"
    marker_cleanup: Dict[str, Any] = {
        "owned": worker.get("marker_creation_owned") is True,
        "handle_registered": worker.get("marker_read_handle_registered") is True,
        "handle_closed": worker.get("marker_read_handle_closed") is True,
        "write_handle_registered": worker.get("marker_write_handle_registered") is True,
        "write_handle_closed": worker.get("marker_write_handle_closed") is True,
        "write_handle_residual": worker.get("marker_write_handle_residual") is True,
        "identity_verified": False,
        "content_verified": False,
        "unlink_effect": marker_unlink_effect,
        "unlink_effect_history": list(worker.get("marker_unlink_effect_history") or []),
        "removed": marker_unlink_effect == "removed",
        "presence_checked": worker.get("marker_presence_checked") is True,
        "presence_state": worker.get("marker_presence_state"),
        "present_after": worker.get("marker_present_after"),
    }
    if marker_cleanup["owned"]:
        write_handle = worker.get("marker_write_handle")
        if write_handle is not None and not worker.get("marker_write_handle_closed"):
            try:
                write_handle.close()
                worker["marker_write_handle_closed"] = True
                worker["marker_write_handle_owned"] = False
                worker["marker_write_handle_residual"] = False
                worker["marker_handle_closed"] = True
                worker["marker_handle_owned"] = False
            except Exception as exc:
                worker["marker_write_handle_close_error"] = _worker_error_text(exc)
                worker["marker_write_handle_residual"] = True
                record_exception("owned marker write-handle close failed", exc)
        marker_cleanup["write_handle_closed"] = worker.get("marker_write_handle_closed") is True
        marker_cleanup["write_handle_residual"] = worker.get("marker_write_handle_residual") is True
        if worker.get("marker_write_handle_registered") and not marker_cleanup["write_handle_closed"]:
            add_error("owned marker write-handle was not closed")

        marker_handle = worker.get("marker_read_handle")
        if marker_handle is not None and not worker.get("marker_read_handle_closed"):
            try:
                marker_handle.close()
                worker["marker_read_handle_closed"] = True
                worker["marker_read_handle_owned"] = False
                worker["marker_read_handle_residual"] = False
            except Exception as exc:
                worker["marker_read_handle_close_error"] = _worker_error_text(exc)
                worker["marker_read_handle_residual"] = True
                record_exception("owned marker read-handle close failed", exc)
        marker_cleanup["handle_closed"] = worker.get("marker_read_handle_closed") is True
        if not worker.get("marker_read_handle_registered"):
            add_error("owned marker read-handle was not registered")
        if not marker_cleanup["write_handle_closed"]:
            pass
        elif not marker_cleanup["handle_closed"]:
            add_error("owned marker read-handle was not closed")
        elif marker_unlink_effect == "removed":
            marker_cleanup["removed"] = True
            marker_cleanup["present_after"] = observe_marker_presence(marker)
            marker_cleanup["presence_checked"] = worker.get("marker_presence_checked") is True
            marker_cleanup["presence_state"] = worker.get("marker_presence_state")
            if marker_cleanup["present_after"] is True:
                add_error("owned marker reappeared after successful cleanup")
        elif not worker.get("marker_identity_registered") or worker.get("marker_identity") is None:
            add_error("owned marker has no registered identity")
        elif not worker.get("marker_binding_observed") or not worker.get("marker_content_sha256"):
            add_error("owned marker has no observed content binding")
        elif marker is None:
            add_error("owned marker path is missing")
        else:
            retry_presence: Optional[bool] = True
            if marker_unlink_effect == "unknown":
                retry_presence = observe_marker_presence(marker)
                marker_cleanup["presence_checked"] = worker.get("marker_presence_checked") is True
                marker_cleanup["presence_state"] = worker.get("marker_presence_state")
                marker_cleanup["present_after"] = worker.get("marker_present_after")
            if retry_presence is not True:
                add_error("owned marker unlink effect remains unknown")
            elif not _capture_marker_binding(worker, marker):
                add_error(
                    worker.get("marker_binding_error")
                    or "owned marker identity/content binding failed"
                )
                failed_binding_handle = worker.get("marker_read_handle")
                if failed_binding_handle is not None and not worker.get("marker_read_handle_closed"):
                    try:
                        failed_binding_handle.close()
                        worker["marker_read_handle_closed"] = True
                        worker["marker_read_handle_owned"] = False
                        worker["marker_read_handle_residual"] = False
                    except Exception as exc:
                        worker["marker_read_handle_close_error"] = _worker_error_text(exc)
                        worker["marker_read_handle_residual"] = True
                        record_exception("failed marker binding handle close failed", exc)
                marker_cleanup["handle_closed"] = worker.get("marker_read_handle_closed") is True
            else:
                marker_cleanup["identity_verified"] = True
                marker_cleanup["content_verified"] = True
                rebound_handle = worker.get("marker_read_handle")
                if rebound_handle is not None and not worker.get("marker_read_handle_closed"):
                    try:
                        rebound_handle.close()
                        worker["marker_read_handle_closed"] = True
                        worker["marker_read_handle_owned"] = False
                        worker["marker_read_handle_residual"] = False
                    except Exception as exc:
                        worker["marker_read_handle_close_error"] = _worker_error_text(exc)
                        worker["marker_read_handle_residual"] = True
                        record_exception("rebound marker read-handle close failed", exc)
                marker_cleanup["handle_closed"] = worker.get("marker_read_handle_closed") is True
                if marker_cleanup["handle_closed"] and not worker.get("marker_read_handle_residual"):
                    try:
                        marker.unlink()
                        record_marker_unlink_effect("removed")
                        marker_cleanup["removed"] = True
                    except Exception as exc:
                        record_marker_unlink_effect("unknown", _worker_error_text(exc))
                        record_exception("owned marker cleanup failed", exc)
                    marker_cleanup["unlink_effect"] = worker.get("marker_unlink_effect")
                    marker_cleanup["unlink_effect_history"] = list(
                        worker.get("marker_unlink_effect_history") or []
                    )
                    marker_cleanup["present_after"] = observe_marker_presence(marker)
                    marker_cleanup["presence_checked"] = worker.get("marker_presence_checked") is True
                    marker_cleanup["presence_state"] = worker.get("marker_presence_state")
                    if marker_cleanup["present_after"] is True:
                        add_error("owned marker remained after cleanup")
                else:
                    add_error("owned marker rebound read-handle was not safely closed")

    reader_final: Dict[str, Dict[str, Any]] = {}
    reader_total_bytes = 0
    for label, reader in readers.items():
        final = {
            "eof": _reader_state(
                reader,
                "eof",
                None,
                errors=reader_eof_errors,
                label=f"{label} reader",
            ),
            "error": _reader_state(
                reader,
                "error",
                "reader not registered",
                errors=reader_state_lookup_errors,
                label=f"{label} reader",
            ),
            "lines_read": _reader_state(
                reader,
                "lines_read",
                None,
                errors=reader_state_lookup_errors,
                label=f"{label} reader",
            ),
            "bytes_read": _reader_state(
                reader,
                "bytes_read",
                None,
                errors=reader_state_lookup_errors,
                label=f"{label} reader",
            ),
            "dropped_lines": _reader_state(
                reader,
                "dropped_lines",
                None,
                errors=reader_state_lookup_errors,
                label=f"{label} reader",
            ),
        }
        reader_final[label] = final
        value = final["bytes_read"]
        if isinstance(value, int) and not isinstance(value, bool):
            reader_total_bytes += value
    if reader_total_bytes > MAX_CHILD_OUTPUT_BYTES:
        worker["pipe_overflow"] = True
        worker["overflow_reason"] = "child reader output exceeded bound"
    for label, recorded in (
        ("thread", reader_thread_lookup_errors),
        ("state", reader_state_lookup_errors),
        ("eof", reader_eof_errors),
        ("aliveness", reader_aliveness_errors),
    ):
        cursor = reader_lookup_error_cursors[label]
        if cursor > len(recorded):
            cursor = 0
        if len(recorded) > cursor:
            add_error(f"reader {label} lookup errors were recorded")
            cursor = len(recorded)
        reader_lookup_error_cursors[label] = cursor
    worker["observed_output_bytes"] = max(
        int(worker.get("observed_output_bytes", 0)), reader_total_bytes
    )
    try:
        protocol = _worker_protocol(worker)
    except Exception as exc:
        record_exception("worker protocol summary failed", exc)
        protocol = {
            "valid": False,
            "stages": [],
            "errors": [_worker_error_text(exc)],
            "event_count": 0,
        }
    returncode = poll_process() if process_owned else None
    residual_process: Optional[bool]
    if not process_owned:
        residual_process = False
    elif process_poll_error is not None:
        residual_process = None
    else:
        residual_process = returncode is None
    attempt_errors = list(errors)
    attempt_outcome = "cleaned" if not attempt_errors else "cleanup_incomplete"
    cumulative_errors = list(cleanup_history)
    cumulative_errors.extend(attempt_errors)
    forced_kill_history = worker.get("forced_kill_history")
    if not isinstance(forced_kill_history, list):
        forced_kill_history = []
        worker["forced_kill_history"] = forced_kill_history
    forced_kill_history.append(bool(forced_kill))
    forced_kill_ever = any(bool(value) for value in forced_kill_history)
    worker["cleanup_errors"] = cumulative_errors
    worker["cleanup_error"] = cumulative_errors[0] if cumulative_errors else None
    worker["cleanup_attempt_errors"] = attempt_errors
    worker["cleanup_attempt_forced_kill"] = forced_kill
    worker["cleanup_attempt_residual_process"] = residual_process
    worker["cleanup_attempt_outcome"] = attempt_outcome
    worker["phase"] = "cleaned" if not cumulative_errors else "cleanup_incomplete"
    readers_alive = {
        "stdout_reader": reader_status.get("stdout", {}).get("alive"),
        "stderr_reader": reader_status.get("stderr", {}).get("alive"),
    }
    return {
        "returncode": returncode,
        "pid": worker.get("pid"),
        "forced_kill": forced_kill_ever,
        "forced_kill_history": list(forced_kill_history),
        "residual_process": residual_process,
        "cleanup_error": cumulative_errors[0] if cumulative_errors else None,
        "cleanup_errors": cumulative_errors,
        "cleanup_attempt_errors": attempt_errors,
        "cleanup_attempt_forced_kill": forced_kill,
        "cleanup_attempt_residual_process": residual_process,
        "cleanup_attempt_outcome": attempt_outcome,
        "primary_error": worker.get("primary_error"),
        "reader_eof": {
            "stdout": reader_final["stdout"]["eof"],
            "stderr": reader_final["stderr"]["eof"],
        },
        "reader_errors": {
            "stdout": reader_final["stdout"]["error"],
            "stderr": reader_final["stderr"]["error"],
        },
        "reader_alive": readers_alive,
        "reader_lines": {
            "stdout": reader_final["stdout"]["lines_read"],
            "stderr": reader_final["stderr"]["lines_read"],
        },
        "reader_bytes": {
            "stdout": reader_final["stdout"]["bytes_read"],
            "stderr": reader_final["stderr"]["bytes_read"],
        },
        "dropped_lines": {
            "stdout": reader_final["stdout"]["dropped_lines"],
            "stderr": reader_final["stderr"]["dropped_lines"],
        },
        "events": worker["events"],
        "stderr_lines": worker["stderr_lines"],
        "stderr_tail": worker["stderr_lines"][-8:],
        "handshake_stage": worker.get("handshake_stage"),
        "pipe_overflow": worker.get("pipe_overflow", False),
        "overflow_reason": worker.get("overflow_reason"),
        "observed_output_bytes": worker.get("observed_output_bytes", 0),
        "protocol": protocol,
        "protocol_valid": protocol["valid"],
        "protocol_errors": protocol["errors"],
        "process_wait_iterations": process_wait_iterations,
        "reader_wait_iterations": reader_wait_iterations,
        "ownership": {
            "phase": worker.get("phase"),
            "marker": {
                "path_registered": marker is not None,
                "token_registered": worker.get("marker_token") is not None,
                "creation_owned": worker.get("marker_creation_owned") is True,
                "identity_registered": worker.get("marker_identity_registered") is True,
                "binding_observed": worker.get("marker_binding_observed") is True,
                "handle_registered": worker.get("marker_read_handle_registered") is True,
                "handle_closed": worker.get("marker_read_handle_closed") is True,
                "cleanup": marker_cleanup,
            },
            "process_owned": process_owned,
            "reader_lookup_errors": list(reader_thread_lookup_errors),
            "reader_state_lookup_errors": list(reader_state_lookup_errors),
            "reader_eof_errors": list(reader_eof_errors),
            "reader_aliveness_errors": list(reader_aliveness_errors),
            "reader_lookup_error_cursors": dict(reader_lookup_error_cursors),
            "pid_registered": worker.get("pid") is not None,
            "reader_registered": dict(worker.get("reader_registered") or {}),
            "reader_start_attempted": dict(worker.get("reader_start_attempted") or {}),
            "reader_start_returned": dict(worker.get("reader_start_returned") or {}),
            "reader_started": dict(worker.get("reader_started") or {}),
            "reader_start_errors": {
                label: list(reader_start_errors[label])
                for label in ("stdout", "stderr")
            },
            "reader_start_unknown": dict(reader_start_unknown),
            "reader_lifecycle_evidence": {
                label: explicit_reader_lifecycle.get(label)
                for label in ("stdout", "stderr")
            },
            "reader_status": reader_status,
            "ready": worker.get("ready") is True,
            "go_attempted": worker.get("go_attempted") is True,
            "go_sent": worker.get("go_sent") is True,
            "go_uncertain": worker.get("go_uncertain") is True,
        },
        "worker_result": next(
            (
                item.get("result")
                for item in reversed(worker["events"])
                if item.get("stage") == "result"
            ),
            None,
        ),
        "worker_error": next(
            (
                {
                    "type": item.get("type"),
                    "message": item.get("message"),
                }
                for item in reversed(worker["events"])
                if item.get("stage") == "error"
            ),
            None,
        ),
    }


def _verify_lifecycle_lock_header(root: pathlib.Path) -> Dict[str, Any]:
    """Read the known lock header only after the held interval has ended."""

    path = root / "commands" / LIFECYCLE_LOCK_NAME
    record = _stable_file_record(path, f"commands/{LIFECYCLE_LOCK_NAME}")
    expected = getattr(command_custody_module, "_LOCK_HEADER", None)
    if not isinstance(expected, bytes) or path.read_bytes() != expected:
        raise RuntimeError(f"lifecycle lock header is not the runtime header: {path}")
    record["header_sha256"] = _sha256_bytes(expected)
    record["header_verified"] = True
    return record


def _run_locked_worker(
    root: pathlib.Path,
    operation: str,
    command_id: str,
    document: Mapping[str, Any],
) -> Dict[str, Any]:
    worker: Dict[str, Any] = _new_worker_ledger(operation, command_id)
    sample: Dict[str, Any] = {}
    launch_error: Optional[str] = None
    lock_header: Optional[Dict[str, Any]] = None
    lock_header_error: Optional[str] = None
    try:
        with lifecycle_custody(root, timeout_seconds=5.0):
            worker["phase"] = "lifecycle_lock_held"
            worker = _spawn_worker(root, operation, command_id, worker)
            worker["phase"] = "waiting_ready"
            ready = _wait_for_worker_stage(worker, "ready", CHILD_READY_TIMEOUT_SECONDS)
            worker["ready"] = ready is True
            sample["ready_seen"] = ready
            sample["handshake_before_api"] = worker.get("handshake_stage")
            if ready:
                worker["phase"] = "baseline_capture"
                session_path = _safe_session_path(root, document)
                baseline = _protected_snapshot(root, session_path)
                sample["baseline_capture_before_go"] = True
                worker["phase"] = "sending_go"
                _send_worker_go(worker)
                sample.update(_sample_while_locked(worker, root, session_path, baseline))
            else:
                worker["phase"] = "ready_timeout"
                sample.update(
                    {
                        "baseline_capture_before_go": False,
                        "sample_count": 0,
                        "sample_duration_seconds": 0.0,
                        "changed_sample_count": 0,
                        "changed_paths": [],
                        "sample_errors": ["worker did not reach ready stage"],
                        "samples": [],
                        "completed_before_release": worker["process"].poll() is not None,
                        "early_exit": None,
                        "calling_seen": False,
                    }
                )
    except Exception as exc:
        launch_error = _worker_error_text(exc)
        worker["phase"] = "launch_failed"
        _remember_worker_error(worker, exc)
    finally:
        cleanup = _finish_worker(worker)
    try:
        lock_header = _verify_lifecycle_lock_header(root)
    except Exception as exc:
        lock_header_error = f"{type(exc).__name__}: {exc}"
    result = {
        "operation": operation,
        "command_id": command_id,
        "launch_error": launch_error,
        "sample": sample,
        "cleanup": cleanup,
        "lock_header": lock_header,
        "lock_header_error": lock_header_error,
        "child_command": worker.get("command"),
    }
    return result


def _exact_stream_map(
    value: Any,
    expected: Mapping[str, Any],
    value_checker: Optional[Callable[[Any], bool]] = None,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    if set(value.keys()) != set(expected.keys()):
        return False
    for key, expected_value in expected.items():
        actual = value.get(key)
        if value_checker is None:
            if actual != expected_value:
                return False
        elif not value_checker(actual):
            return False
    return True


def _is_positive_integer(value: Any, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_explicit_none(value: Any, key: str) -> bool:
    return isinstance(value, Mapping) and key in value and value[key] is None


def _is_valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _worker_evidence_valid(
    worker: Any,
    cleanup: Any,
    expected_operation: str,
    expected_command_id: str,
) -> bool:
    if not isinstance(worker, Mapping) or not isinstance(cleanup, Mapping):
        return False
    events = cleanup.get("events")
    if not isinstance(events, list) or len(events) != 3:
        return False
    if any(not isinstance(event, Mapping) for event in events):
        return False
    stages = [event.get("stage") for event in events]
    if stages != ["ready", "calling", "result"]:
        return False
    ready, calling, result = events
    if worker.get("operation") != expected_operation:
        return False
    if worker.get("command_id") != expected_command_id:
        return False
    if calling.get("operation") != worker.get("operation"):
        return False
    if result.get("command_id") != worker.get("command_id"):
        return False
    expected_result = [expected_command_id]
    if result.get("result") != expected_result or cleanup.get("worker_result") != expected_result:
        return False
    attestation = ready.get("module_attestation")
    if not isinstance(attestation, Mapping) or set(attestation) != set(WORKER_MODULE_NAMES):
        return False
    for name in WORKER_MODULE_NAMES:
        entry = attestation.get(name)
        if not isinstance(entry, Mapping) or set(entry) != {"expected_path", "actual_path", "sha256"}:
            return False
        expected_path = entry.get("expected_path")
        actual_path = entry.get("actual_path")
        if not _is_nonempty_string(expected_path) or not _is_nonempty_string(actual_path):
            return False
        if expected_path != actual_path or not _is_valid_sha256(entry.get("sha256")):
            return False
    protocol = cleanup.get("protocol")
    if not isinstance(protocol, Mapping):
        return False
    if protocol.get("valid") is not True:
        return False
    if protocol.get("stages") != stages or protocol.get("errors") != []:
        return False
    if not _is_positive_integer(protocol.get("event_count"), 1) or protocol.get("event_count") != 3:
        return False
    return cleanup.get("protocol_valid") is True and cleanup.get("protocol_errors") == []


def _is_canonical_done_state(state: Any, command_id: str) -> bool:
    if not isinstance(state, Mapping):
        return False
    canonical_done_path = state.get("canonical_done_path")
    if not _is_nonempty_string(canonical_done_path):
        return False
    locations = state.get("locations")
    if not isinstance(locations, list) or len(locations) != 1:
        return False
    first = locations[0]
    if not isinstance(first, Mapping):
        return False
    if first.get("stage") != "done":
        return False
    if not _is_nonempty_string(first.get("path")) or first.get("path") != canonical_done_path:
        return False
    if first.get("status") != CONFIRMATION_CONFIRMED:
        return False
    return (
        state.get("errors") == []
        and state.get("command_id") == command_id
        and state.get("certificate_valid") is True
        and state.get("outcome_status") == CONFIRMATION_CONFIRMED
        and state.get("outcome_blockers") == []
        and _is_positive_integer(state.get("total_count"), 1)
        and state.get("total_count") == 1
        and _is_positive_integer(state.get("done_count"), 1)
        and state.get("done_count") == 1
        and state.get("canonical_done") is True
        and state.get("canonical_confirming_exists") is False
        and state.get("confirming_exists") is False
        and state.get("source_exists") is False
        and state.get("certificate_blockers") == []
    )


def _lock_case(
    context: CaseContext,
    root: pathlib.Path,
    operation: str,
    command_id: str,
) -> Dict[str, Any]:
    document, outcome = confirmation_fixture.published_fixture(root, command_id)
    marker = stage_confirmed_outcomes_commit(root, document)
    context.note(
        command_id=command_id,
        operation=operation,
        published_outcomes_hash=document.get("outcomes_hash"),
        journal_hash=marker.get("transaction_hash"),
    )
    context.require(outcome.get("confirmation_status") == CONFIRMATION_CONFIRMED, "lock fixture was not confirmed")
    context.require(outcome.get("blockers") == [], "lock fixture certificate contains blockers")
    run_info = _run_locked_worker(root, operation, command_id, document)
    context.note(worker=run_info)
    sample = run_info.get("sample") or {}
    cleanup = run_info.get("cleanup") or {}
    context.require(sample.get("ready_seen") is True, "worker did not prove the pre-call ready handshake")
    context.require(sample.get("sample_duration_seconds", 0.0) >= 0.3, "lock observation window was too short")
    context.require(sample.get("sample_count", 0) >= 5, "lock observation did not collect five samples")
    context.require(sample.get("baseline_captured_before_go") is True, "protected baseline was not captured before go")
    context.require(len(sample.get("samples") or []) >= sample.get("sample_count", 0), "held-period samples were not preserved")
    context.require(sample.get("sample_errors") == [], "a protected-tree sample raised an error")
    context.require(sample.get("changed_sample_count") == 0, "protected fixture changed while parent held the lock")
    context.require(sample.get("calling_seen") is True, "worker never reached the API-call handshake")
    context.require(sample.get("completed_before_release") is False, "child completed before the parent released the lock")
    context.require(run_info.get("launch_error") is None, "worker launch or handshake raised an error")
    context.require(run_info.get("lock_header_error") is None, "lifecycle lock header verification failed")
    context.require((run_info.get("lock_header") or {}).get("header_verified") is True, "lifecycle lock header was not verified")
    context.require(cleanup.get("forced_kill") is False, "worker required forced termination")
    context.require(cleanup.get("residual_process") is False, "worker process remained after cleanup")
    context.require(cleanup.get("cleanup_error") is None, "worker cleanup was incomplete")
    context.require(cleanup.get("returncode") == 0, "worker did not exit normally")
    context.require(cleanup.get("protocol_valid") is True, "worker protocol was not strict ready-calling-result")
    context.require(cleanup.get("protocol_errors") == [], "worker protocol contained an error")
    context.require(cleanup.get("worker_error") is None, "worker emitted an error")
    context.require(cleanup.get("stderr_lines") == [], "worker wrote to stderr")
    context.require(_exact_stream_map(cleanup.get("reader_eof"), {"stdout": True, "stderr": True}, lambda value: value is True), "worker pipes did not reach EOF")
    context.require(_exact_stream_map(cleanup.get("reader_errors"), {"stdout": None, "stderr": None}, lambda value: value is None), "worker reader raised an error")
    context.require(
        _exact_stream_map(
            cleanup.get("reader_alive"),
            {"stdout_reader": False, "stderr_reader": False},
            lambda value: value is False,
        ),
        "worker reader remained alive",
    )
    context.require(_is_positive_integer(cleanup.get("dropped_lines", {}).get("stdout", 0), 0), "worker stdout lines were dropped")
    context.require(_is_positive_integer(cleanup.get("dropped_lines", {}).get("stderr", 0), 0), "worker stderr lines were dropped")
    context.require(cleanup.get("dropped_lines") == {"stdout": 0, "stderr": 0}, "worker dropped lines mismatch")
    context.require(cleanup.get("pipe_overflow") is False, "worker output exceeded a hard bound")
    context.require(cleanup.get("worker_result") == [command_id], "worker result did not contain the command id")
    _assert_final_state(context, root, document, command_id)
    return {"command_id": command_id, "operation": operation}


def _case_commit_waits_for_lifecycle_lock(context: CaseContext, root: pathlib.Path) -> Dict[str, Any]:
    return _lock_case(context, root, "commit", "cmd-a09a9-commit-lock")


def _case_recover_waits_for_lifecycle_lock(context: CaseContext, root: pathlib.Path) -> Dict[str, Any]:
    return _lock_case(context, root, "recover", "cmd-a09a9-recover-lock")


def _atomic_replace_bytes(path: pathlib.Path, payload: bytes) -> None:
    temporary: Optional[pathlib.Path] = None
    try:
        descriptor, raw_name = tempfile.mkstemp(
            prefix=f".{path.stem}.a09a9-",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary = pathlib.Path(raw_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _stat_evidence(path: pathlib.Path) -> Dict[str, Any]:
    metadata = os.lstat(path)
    return {
        "identity": _identity(metadata),
        "mtime_ns": _mtime_ns(metadata),
        "size": int(metadata.st_size),
    }


def _case_stale_source_replacement_preserved(context: CaseContext, root: pathlib.Path) -> Dict[str, Any]:
    command_id = "cmd-a09a9-stale-source"
    document_a, outcome_a = confirmation_fixture.published_fixture(root, command_id)
    stage_confirmed_outcomes_commit(root, document_a)
    source_path = root / "commands" / "confirming" / f"{command_id}.json"
    source_a_raw = source_path.read_bytes()
    command_a = _read_json(source_path)
    a_intent = command_intent_hash(command_a)
    certificate_paths = _certificate_paths(root, document_a)
    certificate_before = {str(path): path.read_bytes() for path in certificate_paths}
    history_path = root / "sessions" / str(command_a["session_id"]) / "state.jsonl"
    history_before = history_path.read_bytes()

    control_cleanup: Dict[str, Any] = {"removed": False, "error": None}
    b_raw: bytes
    b_intent: str
    b_control_details: Dict[str, Any]
    attack_started = False
    control = tempfile.TemporaryDirectory(prefix="oni-a09a9-stale-control-")
    control_root = pathlib.Path(control.name)
    try:
        source_b, witness_b, command_b = outcome_fixture.fixture(command_id)
        command_b["expires_at"] = (
            dt.datetime.fromisoformat(str(command_a["expires_at"])) + dt.timedelta(seconds=1)
        ).isoformat(timespec="milliseconds")
        command_b["approval_receipt"] = outcome_fixture.make_approval_receipt(command_b)
        outcome_fixture.write_session_history(control_root, source_b, command_b)
        outcome_fixture.write_command(control_root, command_b)
        document_b = build_action_outcomes(
            control_root,
            witness_b,
            outcome_fixture.health_for(witness_b),
        )
        outcome_b = outcome_fixture.assert_status(document_b, CONFIRMATION_CONFIRMED)
        outcome_fixture.write_json_atomic(control_root / "action-outcomes.json", document_b)
        session_b_path = _safe_session_path(control_root, document_b)
        outcome_fixture.write_json_atomic(session_b_path, document_b)
        stage_confirmed_outcomes_commit(control_root, document_b)
        b_source_path = control_root / "commands" / "confirming" / f"{command_id}.json"
        b_raw = b_source_path.read_bytes()
        b_intent = command_intent_hash(_read_json(b_source_path))
        b_first = recover_staged_confirmed_outcomes(control_root)
        b_second = recover_staged_confirmed_outcomes(control_root)
        b_final = _assert_final_state(context, control_root, document_b, command_id)
        b_control_details = {
            "status": "passed",
            "intent_hash": b_intent,
            "raw_sha256": _sha256_bytes(b_raw),
            "build_status": outcome_b.get("confirmation_status"),
            "build_blockers": outcome_b.get("blockers"),
            "first_recovery": b_first,
            "second_recovery": b_second,
            "final_state": b_final,
            "attack_started": False,
        }
        context.require(outcome_b.get("confirmation_status") == CONFIRMATION_CONFIRMED, "B control was not confirmed")
        context.require(outcome_b.get("blockers") == [], "B control certificate contains blockers")
        context.require(b_first == [command_id] and b_second == [command_id], "B control commit was not idempotent")
    except Exception as exc:
        b_control_details = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "attack_started": False,
        }
        context.note(b_control=b_control_details)
        raise
    finally:
        try:
            control.cleanup()
            control_cleanup["removed"] = not control_root.exists()
        except Exception as exc:
            control_cleanup["error"] = f"{type(exc).__name__}: {exc}"
    context.note(b_control=b_control_details, b_control_cleanup=control_cleanup)
    context.require(control_cleanup["removed"] and control_cleanup["error"] is None, "B control TemporaryDirectory cleanup failed")
    context.require(a_intent != b_intent, "A and B command intents are not different")
    context.require(_sha256_bytes(source_a_raw) != _sha256_bytes(b_raw), "A and B source bytes are not different")
    attack_started = True

    hook_state: Dict[str, Any] = {
        "calls": 0,
        "a_snapshot_returned": False,
        "replacement_after_scan": False,
        "b_identity_after_replace": None,
        "b_mtime_after_replace": None,
        "b_sha256_after_replace": None,
        "error": None,
    }
    original_locations = action_outcomes_module._command_locations

    def one_shot_locations(bridge_dir: pathlib.Path) -> Dict[str, List[Tuple[str, pathlib.Path, Dict[str, Any]]]]:
        observed = original_locations(bridge_dir)
        hook_state["calls"] += 1
        if hook_state["calls"] != 1:
            raise AssertionError("stale-source hook fired more than once")
        items = observed.get(command_id) or []
        confirming = [item for item in items if item[0] == "confirming"]
        if len(confirming) != 1 or confirming[0][1].resolve() != source_path.resolve():
            raise AssertionError("A snapshot did not identify the canonical confirming source")
        observed_command = confirming[0][2]
        if command_intent_hash(observed_command) != a_intent:
            raise AssertionError("A snapshot did not contain the A intent")
        returned_a = copy.deepcopy(observed)
        hook_state["a_snapshot_returned"] = True
        hook_state["scan_finished_ns"] = time.monotonic_ns()
        replacement_started = time.monotonic_ns()
        _atomic_replace_bytes(source_path, b_raw)
        replacement_finished = time.monotonic_ns()
        b_stat = _stat_evidence(source_path)
        hook_state["replacement_after_scan"] = replacement_started >= hook_state["scan_finished_ns"]
        hook_state["replacement_finished_ns"] = replacement_finished
        hook_state["b_identity_after_replace"] = b_stat["identity"]
        hook_state["b_mtime_after_replace"] = b_stat["mtime_ns"]
        hook_state["b_sha256_after_replace"] = _sha256_bytes(source_path.read_bytes())
        return returned_a

    consumer_result: Optional[List[str]] = None
    consumer_exception: Optional[BaseException] = None
    action_outcomes_module._command_locations = one_shot_locations
    try:
        # The hook only changes _command_locations.  The real consumer,
        # certificate validator, payload builder, and filesystem operations run.
        hook_state["scan_started_ns"] = time.monotonic_ns()
        consumer_result = commit_confirmed_outcomes(
            root,
            document_a,
            publication_paths=certificate_paths,
        )
    except BaseException as exc:
        consumer_exception = exc
    finally:
        action_outcomes_module._command_locations = original_locations
        hook_state["hook_restored"] = action_outcomes_module._command_locations is original_locations
    context.note(hook=hook_state)

    source_after_exists = source_path.exists()
    source_after_raw = source_path.read_bytes() if source_after_exists else b""
    source_after_stat = _stat_evidence(source_path) if source_after_exists else None
    done_path = root / "commands" / "done" / f"{command_id}.json"
    done_exists = done_path.exists()
    certificate_after = {str(path): path.read_bytes() for path in certificate_paths if path.exists()}
    history_after = history_path.read_bytes() if history_path.exists() else b""
    allowed_exception = consumer_exception is None or isinstance(
        consumer_exception,
        (CommandCustodyConflict, CommandCustodyUnknown, ActionOutcomeError),
    )
    b_preserved = (
        source_after_exists
        and source_after_raw == b_raw
        and source_after_stat is not None
        and source_after_stat["identity"] == hook_state.get("b_identity_after_replace")
        and source_after_stat["mtime_ns"] == hook_state.get("b_mtime_after_replace")
        and _sha256_bytes(source_after_raw) == hook_state.get("b_sha256_after_replace")
    )
    evidence = {
        "attack_started": attack_started,
        "consumer_result": consumer_result,
        "consumer_exception": (
            None
            if consumer_exception is None
            else {"type": type(consumer_exception).__name__, "message": str(consumer_exception)}
        ),
        "allowed_exception": allowed_exception,
        "source_after_exists": source_after_exists,
        "source_after_identity": source_after_stat.get("identity") if source_after_stat else None,
        "source_after_mtime_ns": source_after_stat.get("mtime_ns") if source_after_stat else None,
        "source_after_sha256": _sha256_bytes(source_after_raw) if source_after_exists else None,
        "b_preserved_at_original_source": b_preserved,
        "done_exists": done_exists,
        "certificate_unchanged": certificate_after == certificate_before,
        "history_unchanged": history_after == history_before,
        "hook_restored": hook_state.get("hook_restored") is True,
        "hook_calls": hook_state.get("calls"),
        "replacement_after_scan": hook_state.get("replacement_after_scan") is True,
        "a_intent_hash": a_intent,
        "b_intent_hash": b_intent,
        "a_raw_sha256": _sha256_bytes(source_a_raw),
        "b_raw_sha256": _sha256_bytes(b_raw),
    }
    context.note(stale_attack=evidence)
    context.require(hook_state.get("calls") == 1, "stale-source hook did not fire exactly once")
    context.require(hook_state.get("a_snapshot_returned") is True, "hook did not return the A snapshot")
    context.require(evidence["replacement_after_scan"], "B replacement did not occur after the A scan")
    context.require(evidence["hook_restored"], "stale-source hook was not restored")
    context.require(allowed_exception, "consumer raised an unapproved exception type")
    context.require(consumer_result is None or command_id not in consumer_result, "consumer reported the stale command as committed")
    context.require(evidence["certificate_unchanged"], "published A certificate was changed by the stale-source attack")
    context.require(evidence["history_unchanged"], "published A acknowledgement history was changed")
    context.require(evidence["b_preserved_at_original_source"], "B source bytes/identity were not preserved")
    context.require(not done_exists, "stale A was published to done and B was lost")
    return {
        "command_id": command_id,
        "positive_control": b_control_details,
        "attack": evidence,
    }


def _run_case(
    name: str,
    callback: Callable[[CaseContext, pathlib.Path], Dict[str, Any]],
) -> Dict[str, Any]:
    context = CaseContext(name)
    context.require(True, "case entered")
    started = time.monotonic()
    case_started_at = now_iso()
    status = "failed"
    error: Optional[str] = None
    temporary: Optional[tempfile.TemporaryDirectory[str]] = None
    root: Optional[pathlib.Path] = None
    cleanup: Dict[str, Any] = {
        "path": None,
        "allocated": False,
        "removed": True,
        "error": None,
    }
    try:
        temporary = tempfile.TemporaryDirectory(prefix=f"oni-a09a9-{name}-")
        root = pathlib.Path(temporary.name)
        cleanup["path"] = str(root)
        cleanup["allocated"] = True
        cleanup["removed"] = False
        returned = callback(context, root)
        if isinstance(returned, Mapping):
            context.note(**dict(returned))
        if context.assertion_count <= 1:
            raise CaseFailure("case body did not execute a non-zero assertion set", context.details)
        status = "passed"
    except CaseFailure as exc:
        error = str(exc)
        context.note(**exc.details)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    if temporary is not None and root is not None:
        try:
            temporary.cleanup()
            cleanup["removed"] = not root.exists()
            if not cleanup["removed"]:
                cleanup["error"] = "TemporaryDirectory remained"
        except Exception as exc:
            cleanup["error"] = f"{type(exc).__name__}: {exc}"
    if cleanup["error"] is not None or not cleanup["removed"]:
        status = "failed"
        error = error or "case TemporaryDirectory cleanup failed"
    return {
        "name": name,
        "status": status,
        "executed": True,
        "started_at": case_started_at,
        "finished_at": now_iso(),
        "duration_seconds": round(time.monotonic() - started, 6),
        "assertion_count": context.assertion_count,
        "body_assertion_count": max(0, context.assertion_count - 1),
        "error": error,
        "details": context.details,
        "temp_cleanup": cleanup,
    }


def _blocked_case(name: str, blocked_by: str, reason: str) -> Dict[str, Any]:
    return {
        "name": name,
        "status": "failed",
        "executed": False,
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "duration_seconds": 0.0,
        "assertion_count": 0,
        "body_assertion_count": 0,
        "error": f"blocked by {blocked_by}: {reason}",
        "details": {
            "classification": "blocked_by_prior_case",
            "blocked_by": blocked_by,
            "blocked_reason": reason,
        },
        "temp_cleanup": {"path": None, "allocated": False, "removed": True, "error": None},
    }


def _unique_report_path(requested: Optional[pathlib.Path]) -> pathlib.Path:
    if requested is not None:
        path = requested.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    reports = PROJECT_ROOT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        utc = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        nonce = secrets.token_hex(8)
        directory = reports / f"a09a9-confirmation-custody-verifier-{utc}-pid-{os.getpid()}-nonce-{nonce}"
        try:
            directory.mkdir(parents=False, exist_ok=False)
            return directory / "report.json"
        except FileExistsError:
            continue
    raise RuntimeError("could not allocate a unique report directory")


def _write_report(path: pathlib.Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _gate(report: Mapping[str, Any]) -> Dict[str, Any]:
    cases = list(report.get("cases") or [])
    expected_names = list(CASE_NAMES)
    actual_names = [str(item.get("name")) for item in cases]
    passed_count = sum(item.get("status") == "passed" for item in cases)
    failed_count = sum(item.get("status") == "failed" for item in cases)
    body_assertion_values = [item.get("body_assertion_count") for item in cases]
    body_assertions = sum(
        value for value in body_assertion_values if _is_positive_integer(value, 0)
    )
    per_case_body_assertions = bool(cases) and all(
        item.get("executed") is True
        and _is_positive_integer(item.get("body_assertion_count"), 1)
        for item in cases
    )
    exception_count = sum(bool(item.get("error")) for item in cases)
    case_nullable_safety = bool(cases) and all(
        _has_explicit_none(item, "error")
        and isinstance(item.get("temp_cleanup"), Mapping)
        and _has_explicit_none(item.get("temp_cleanup"), "error")
        for item in cases
    )
    fixture_allocation_failures = sum(
        1
        for item in cases
        if item.get("executed") is True
        and (
            not isinstance(item.get("temp_cleanup"), Mapping)
            or item.get("temp_cleanup").get("allocated") is not True
        )
    )
    cleanup_failures = sum(
        1
        for item in cases
        if not isinstance(item.get("temp_cleanup"), Mapping)
        or not _has_explicit_none(item.get("temp_cleanup"), "error")
        or item.get("temp_cleanup").get("removed") is not True
    )
    forced_kills = sum(
        bool(((item.get("details") or {}).get("worker") or {}).get("cleanup", {}).get("forced_kill"))
        for item in cases
    )
    residual_processes = sum(
        bool(((item.get("details") or {}).get("worker") or {}).get("cleanup", {}).get("residual_process"))
        for item in cases
    )
    def _case_by_name(name: str) -> Dict[str, Any]:
        return next((item for item in cases if item.get("name") == name), {})

    def _worker_cleanup(case: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        worker = (case.get("details") or {}).get("worker")
        if not isinstance(worker, Mapping):
            return None
        cleanup = worker.get("cleanup")
        if isinstance(cleanup, Mapping):
            return dict(cleanup)
        return None

    normal = _case_by_name("normal_commit_control")
    stale = _case_by_name("stale_source_replacement_preserved")
    normal_details = normal.get("details") or {}
    stale_details = stale.get("details") or {}
    normal_final = normal_details.get("final_state")
    normal_recovery = normal_details.get("recovery_results")
    b_control = stale_details.get("b_control") or {}
    b_control_cleanup = stale_details.get("b_control_cleanup") or {}
    normal_command_id = "cmd-a09a9-normal-commit-control"
    stale_command_id = "cmd-a09a9-stale-source"

    lock_case_requirements = [
        ("commit_waits_for_lifecycle_lock", "commit", "cmd-a09a9-commit-lock"),
        ("recover_waits_for_lifecycle_lock", "recover", "cmd-a09a9-recover-lock"),
    ]
    lock_case_workers: List[Dict[str, Any]] = []
    lock_case_cleanups: List[Dict[str, Any]] = []
    lock_case_binding_ok = True
    for case_name, expected_operation, expected_command_id in lock_case_requirements:
        case = _case_by_name(case_name)
        worker = (case.get("details") or {}).get("worker")
        cleanup = _worker_cleanup(case)
        if isinstance(worker, Mapping):
            lock_case_workers.append(dict(worker))
        if isinstance(cleanup, Mapping):
            lock_case_cleanups.append(dict(cleanup))
        lock_case_binding_ok = (
            lock_case_binding_ok
            and isinstance(worker, Mapping)
            and worker.get("operation") == expected_operation
            and worker.get("command_id") == expected_command_id
            and cleanup is not None
            and cleanup.get("worker_result") == [expected_command_id]
            and _is_positive_integer(cleanup.get("returncode"), 0)
            and cleanup.get("returncode") == 0
            and _is_positive_integer(cleanup.get("pid"), 1)
            and cleanup.get("forced_kill") is False
            and cleanup.get("residual_process") is False
            and _has_explicit_none(cleanup, "cleanup_error")
            and _has_explicit_none(cleanup, "worker_error")
            and _has_explicit_none(worker, "launch_error")
            and _has_explicit_none(worker, "lock_header_error")
            and isinstance(worker.get("lock_header"), Mapping)
            and worker.get("lock_header").get("header_verified") is True
            and _worker_evidence_valid(worker, cleanup, expected_operation, expected_command_id)
            and cleanup.get("stderr_lines") == []
            and cleanup.get("pipe_overflow") is False
            and _exact_stream_map(cleanup.get("reader_eof"), {"stdout": True, "stderr": True}, lambda value: value is True)
            and _exact_stream_map(
                cleanup.get("dropped_lines"),
                {"stdout": 0, "stderr": 0},
                lambda value: _is_positive_integer(value, 0) and value == 0,
            )
            and _exact_stream_map(cleanup.get("reader_errors"), {"stdout": None, "stderr": None}, lambda value: value is None)
            and _exact_stream_map(
                cleanup.get("reader_alive"),
                {"stdout_reader": False, "stderr_reader": False},
                lambda value: value is False,
            )
        )

    worker_records = lock_case_workers
    cleanup_records = lock_case_cleanups
    normal_final_state_ok = _is_canonical_done_state(normal_final, normal_command_id)
    b_final_state_ok = _is_canonical_done_state(b_control.get("final_state"), stale_command_id)
    normal_recovery_ok = (
        isinstance(normal_recovery, list)
        and normal_recovery == [[normal_command_id], [normal_command_id]]
    )
    b_positive_control = (
        b_control.get("status") == "passed"
        and b_control.get("attack_started") is False
        and b_control.get("build_status") == CONFIRMATION_CONFIRMED
        and b_control.get("build_blockers") == []
        and b_control.get("first_recovery") == [stale_command_id]
        and b_control.get("second_recovery") == [stale_command_id]
        and b_final_state_ok
        and isinstance(b_control_cleanup, Mapping)
        and b_control_cleanup.get("removed") is True
        and _has_explicit_none(b_control_cleanup, "error")
    )
    positive_control = b_positive_control
    worker_evidence_complete = (
        len(worker_records) == 2
        and len(cleanup_records) == 2
        and all(
            _worker_evidence_valid(worker, cleanup, expected_operation, expected_command_id)
            and _is_positive_integer(cleanup.get("pid"), 1)
            and _is_positive_integer(cleanup.get("returncode"), 0)
            and cleanup.get("returncode") == 0
            and cleanup.get("forced_kill") is False
            and cleanup.get("residual_process") is False
            and _has_explicit_none(cleanup, "cleanup_error")
            and _has_explicit_none(cleanup, "worker_error")
            and _has_explicit_none(worker, "launch_error")
            and _has_explicit_none(worker, "lock_header_error")
            and isinstance(worker.get("lock_header"), Mapping)
            and worker.get("lock_header").get("header_verified") is True
            and cleanup.get("stderr_lines") == []
            and cleanup.get("pipe_overflow") is False
            and _exact_stream_map(cleanup.get("reader_eof"), {"stdout": True, "stderr": True}, lambda value: value is True)
            and _exact_stream_map(cleanup.get("reader_errors"), {"stdout": None, "stderr": None}, lambda value: value is None)
            and _exact_stream_map(
                cleanup.get("reader_alive"),
                {"stdout_reader": False, "stderr_reader": False},
                lambda value: value is False,
            )
            and _exact_stream_map(
                cleanup.get("dropped_lines"),
                {"stdout": 0, "stderr": 0},
                lambda value: _is_positive_integer(value, 0) and value == 0,
            )
            for (_, expected_operation, expected_command_id), worker, cleanup in zip(
                lock_case_requirements, worker_records, cleanup_records
            )
        )
    )
    worker_protocol_failures = (
        sum(
            not _worker_evidence_valid(worker, cleanup, expected_operation, expected_command_id)
            for (_, expected_operation, expected_command_id), worker, cleanup in zip(
                lock_case_requirements, worker_records, cleanup_records
            )
        )
        if len(worker_records) == len(cleanup_records) == 2
        else 1
    )
    worker_output_failures = sum(
        1
        for cleanup in cleanup_records
        if cleanup.get("pipe_overflow") is True
        or bool(cleanup.get("stderr_lines"))
    )
    hash_drift = report.get("hash_drift") or {}
    checks = {
        "exact_case_names": actual_names == expected_names and len(actual_names) == len(expected_names),
        "exact_case_count": len(cases) == 4,
        "nonzero_body_assertions": per_case_body_assertions,
        "case_nullable_safety": case_nullable_safety,
        "no_fixture_allocation_failure": fixture_allocation_failures == 0,
        "normal_control_passed": normal.get("status") == "passed",
        "normal_final_state_passed": normal_final_state_ok,
        "normal_recovery_idempotent": normal_recovery_ok,
        "b_positive_control_passed": positive_control,
        "b_control_passed": b_positive_control,
        "all_cases_passed": passed_count == 4 and failed_count == 0 and all(
            item.get("status") == "passed" for item in cases
        ),
        "no_case_exception": exception_count == 0,
        "strict_case_execution": len(cases) == 4 and all(
            item.get("executed") is True
            and _is_positive_integer(item.get("body_assertion_count"), 1)
            for item in cases
        ),
        "no_skip": all(item.get("executed") is True for item in cases) if cases else False,
        "commit_lock_worker_bound": (
            len(lock_case_workers) >= 1
            and any(
                (item.get("operation") == "commit" and item.get("command_id") == "cmd-a09a9-commit-lock")
                for item in lock_case_workers
            )
        ),
        "recover_lock_worker_bound": (
            len(lock_case_workers) >= 2
            and any(
                (item.get("operation") == "recover" and item.get("command_id") == "cmd-a09a9-recover-lock")
                for item in lock_case_workers
            )
        ),
        "lock_case_worker_binding_complete": lock_case_binding_ok,
        "worker_evidence_complete": worker_evidence_complete,
        "worker_binding_ok": lock_case_binding_ok and len(cleanup_records) == len(lock_case_workers) == 2,
        "no_worker_protocol_failure": worker_protocol_failures == 0,
        "no_worker_output_failure": worker_output_failures == 0,
        "no_hash_drift": not hash_drift,
        "no_forced_kill": forced_kills == 0,
        "no_residual_process": residual_processes == 0,
        "cleanup_complete": cleanup_failures == 0,
    }
    passed = all(checks.values())
    return {
        "status": "passed" if passed else "failed",
        "expected_case_names": expected_names,
        "actual_case_names": actual_names,
        "expected_case_count": 4,
        "actual_case_count": len(cases),
        "passed_count": passed_count,
        "failed_count": failed_count,
        "body_assertion_count": body_assertions,
        "fixture_allocation_failure_count": fixture_allocation_failures,
        "case_exception_count": exception_count,
        "cleanup_failure_count": cleanup_failures,
        "forced_kill_count": forced_kills,
        "residual_process_count": residual_processes,
        "worker_count": len(worker_records),
        "worker_protocol_failure_count": worker_protocol_failures,
        "worker_output_failure_count": worker_output_failures,
        "checks": checks,
    }


def run(report_path: pathlib.Path) -> int:
    started_at = now_iso()
    hashes_before = _source_hash_snapshot()
    module_attestation: Dict[str, Any] = {}
    precheck_error: Optional[str] = None
    try:
        module_attestation = _module_attestation()
        author_baseline_before = _author_baseline_comparison(hashes_before)
    except Exception as exc:
        author_baseline_before = {"module_attestation": {"error": f"{type(exc).__name__}: {exc}"}}
        precheck_error = f"module/source attestation failed: {type(exc).__name__}: {exc}"

    if precheck_error is None:
        callbacks: Dict[str, Callable[[CaseContext, pathlib.Path], Dict[str, Any]]] = {
            "normal_commit_control": _case_normal_commit_control,
            "commit_waits_for_lifecycle_lock": _case_commit_waits_for_lifecycle_lock,
            "recover_waits_for_lifecycle_lock": _case_recover_waits_for_lifecycle_lock,
            "stale_source_replacement_preserved": _case_stale_source_replacement_preserved,
        }
        normal_case = _run_case("normal_commit_control", callbacks["normal_commit_control"])
        cases = [normal_case]
        if normal_case.get("status") == "passed":
            cases.extend(
                _run_case(name, callbacks[name])
                for name in CASE_NAMES[1:]
            )
        else:
            reason = str(normal_case.get("error") or "normal control did not pass")
            cases.extend(
                _blocked_case(name, "normal_commit_control", reason)
                for name in CASE_NAMES[1:]
            )
    else:
        cases = [
            {
                "name": name,
                "status": "failed",
                "executed": False,
                "assertion_count": 0,
                "body_assertion_count": 0,
                "error": precheck_error,
                "details": {"classification": "hash_or_import_drift"},
                "temp_cleanup": {"removed": True, "error": None},
            }
            for name in CASE_NAMES
        ]
    hashes_after = _source_hash_snapshot()
    author_baseline_after = _author_baseline_comparison(hashes_after)
    hash_drift: Dict[str, Any] = {}
    if hashes_before != hashes_after:
        hash_drift["changed_during_run"] = {
            path: {"before": hashes_before.get(path), "after": hashes_after.get(path)}
            for path in sorted(set(hashes_before) | set(hashes_after))
            if hashes_before.get(path) != hashes_after.get(path)
        }

    report: Dict[str, Any] = {
        "schema": "oni.a09a9.confirmation-custody-verification.v1",
        "action_id": ACTION_ID,
        "generated_at": now_iso(),
        "started_at": started_at,
        "finished_at": now_iso(),
        "mode": "offline_real_import_and_subprocess_fault_injection",
        "game_launched": False,
        "bridge_process_started": False,
        "live_admission": False,
        "module_attestation": module_attestation,
        "source_hashes_before": hashes_before,
        "source_hashes_after": hashes_after,
        "author_baseline_comparison_before": author_baseline_before,
        "author_baseline_comparison_after": author_baseline_after,
        "hash_drift": hash_drift,
        "cases": cases,
    }
    report["gate"] = _gate(report)
    report["status"] = report["gate"]["status"]
    report["classification"] = None if report["status"] == "passed" else (
        "hash_drift" if hash_drift else "confirmation_custody_behavior_fail"
    )
    _write_report(report_path, report)
    for case in cases:
        print(f"[{str(case.get('status', 'failed')).upper()}] {case.get('name')}")
    print(
        f"Summary: {report['gate']['passed_count']}/{report['gate']['expected_case_count']} cases; "
        f"status={report['status']}"
    )
    print(f"Report: {report_path}")
    return 0 if report["status"] == "passed" else 1


def _worker_emit(value: Mapping[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) > MAX_CHILD_LINE:
        raise RuntimeError("worker protocol line exceeds bound")
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def _lexical_path_without_reparse(path: pathlib.Path, label: str) -> pathlib.Path:
    lexical = pathlib.Path(os.path.abspath(os.fspath(path)))
    if not lexical.is_absolute():
        raise RuntimeError(f"{label} is not absolute")
    current = pathlib.Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current = current / part
        metadata = os.lstat(current)
        if _is_reparse(metadata):
            raise RuntimeError(f"{label} contains a symlink/reparse path segment: {current}")
    return lexical


def _same_lexical_path(left: pathlib.Path, right: pathlib.Path) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def _worker_validate_args(args: argparse.Namespace) -> Tuple[pathlib.Path, Dict[str, Any]]:
    if SCRIPT_PATH != (PROJECT_ROOT / "scripts" / "verify_confirmation_custody_integration.py").resolve():
        raise RuntimeError("worker script path is not the expected workspace script")
    if not isinstance(args.root, pathlib.Path) or not args.root.is_absolute():
        raise RuntimeError("worker root must be an absolute path")
    temp_root = _lexical_path_without_reparse(pathlib.Path(tempfile.gettempdir()), "system Temp")
    temp_resolved = temp_root.resolve(strict=True)
    if not _same_lexical_path(temp_root, temp_resolved) or not temp_resolved.is_dir():
        raise RuntimeError("system Temp is not a stable directory")
    root = _lexical_path_without_reparse(args.root, "worker root")
    root_resolved = root.resolve(strict=True)
    if not _same_lexical_path(root, root_resolved):
        raise RuntimeError("worker root resolves through an alias")
    if not root_resolved.is_dir() or not _same_lexical_path(root.parent, temp_root):
        raise RuntimeError("worker root is not a direct system Temp child")
    if not root.name.startswith("oni-a09a9-"):
        raise RuntimeError("worker root is outside the owned oni-a09a9 Temp scope")
    if not isinstance(args.marker, pathlib.Path) or not args.marker.is_absolute():
        raise RuntimeError("worker marker must be an absolute path")
    marker = _lexical_path_without_reparse(args.marker, "worker marker")
    marker_resolved = marker.resolve(strict=True)
    if not _same_lexical_path(marker, marker_resolved):
        raise RuntimeError("worker marker resolves through an alias")
    marker_metadata = os.lstat(marker)
    if (
        not stat.S_ISREG(marker_metadata.st_mode)
        or int(marker_metadata.st_nlink) != 1
        or not _same_lexical_path(marker.parent, root)
        or not WORKER_MARKER_RE.fullmatch(marker.name)
    ):
        raise RuntimeError("worker marker path is outside the validated root")
    if not isinstance(args.token, str) or WORKER_TOKEN_RE.fullmatch(args.token) is None:
        raise RuntimeError("worker token is invalid")
    marker_before = os.lstat(marker)
    marker_value = _read_json(marker)
    marker_after = os.lstat(marker)
    if (
        _identity(marker_before) != _identity(marker_after)
        or _mtime_ns(marker_before) != _mtime_ns(marker_after)
        or int(marker_before.st_size) != int(marker_after.st_size)
    ):
        raise RuntimeError("worker marker changed while it was being validated")
    if set(marker_value) != {"schema", "root", "operation", "command_id", "token"}:
        raise RuntimeError("worker marker shape is invalid")
    if marker_value.get("schema") != "oni.a09a9.custody-worker-marker.v1":
        raise RuntimeError("worker marker schema is invalid")
    if marker_value.get("root") != str(root) or marker_value.get("operation") != args.worker:
        raise RuntimeError("worker marker binding is invalid")
    if marker_value.get("token") != args.token or marker.name != f".oni-a09a9-worker-{args.token}.json":
        raise RuntimeError("worker marker token binding is invalid")
    command_id = marker_value.get("command_id")
    if not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", command_id):
        raise RuntimeError("worker command id is invalid")
    if args.worker not in WORKER_OPERATIONS:
        raise RuntimeError("worker operation is invalid")
    return root, marker_value


def _worker_main(args: argparse.Namespace) -> int:
    try:
        root, marker = _worker_validate_args(args)
        child_module_attestation = _module_attestation()
        _worker_emit({"stage": "ready", "module_attestation": child_module_attestation})
        go = sys.stdin.readline(32)
        if go.strip() != "go":
            _worker_emit({"stage": "error", "type": "WorkerProtocolError", "message": "go was not received"})
            return 2
        _worker_emit({"stage": "calling", "operation": args.worker})
        command_id = str(marker["command_id"])
        if args.worker == "commit":
            document = _read_json(root / "action-outcomes.json")
            validate_action_outcomes(document)
            session_path = _safe_session_path(root, document)
            result = commit_confirmed_outcomes(
                root,
                document,
                publication_paths=(root / "action-outcomes.json", session_path),
            )
        else:
            result = recover_staged_confirmed_outcomes(root)
        if not isinstance(result, list):
            raise RuntimeError("worker API result is not a list")
        _worker_emit({"stage": "result", "command_id": command_id, "result": result})
        return 0
    except Exception as exc:
        try:
            _worker_emit(
                {
                    "stage": "error",
                    "type": type(exc).__name__,
                    "message": str(exc)[:512],
                }
            )
        except Exception:
            pass
        return 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=pathlib.Path, default=None)
    parser.add_argument("--worker", choices=sorted(WORKER_OPERATIONS), default=None)
    parser.add_argument("--root", type=pathlib.Path, default=None)
    parser.add_argument("--marker", type=pathlib.Path, default=None)
    parser.add_argument("--token", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    worker_args_present = any(value is not None for value in (args.root, args.marker, args.token))
    if args.worker is not None:
        if args.report is not None or not worker_args_present or args.root is None or args.marker is None or args.token is None:
            raise SystemExit("--worker is mutually exclusive with --report and requires --root/--marker/--token")
        return _worker_main(args)
    if worker_args_present:
        raise SystemExit("--root/--marker/--token are only valid with --worker")
    report_path = _unique_report_path(args.report)
    return run(report_path)


if __name__ == "__main__":
    raise SystemExit(main())
